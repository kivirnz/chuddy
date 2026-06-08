"""Media service: orchestrates download, post-processing, and DB persistence."""

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from chuddy.config import settings
from chuddy.downloader import MediaDownloader, DownloadError
from chuddy.enums import DownMediaType
from chuddy.models import DownMedia, DownloadContext, Video, Audio
from chuddy.ytdl_opts import HostConfig
from chuddy.utils import remove_dir, gen_random_str


class MediaService:
    """Orchestrate download and post-processing of media."""

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)
        self._downloader = MediaDownloader()

    async def process(self, ctx: DownloadContext) -> DownMedia | None:
        """Full pipeline: download → post-process → return media."""
        import chuddy.db as db

        # Check/create task
        task_id, status = await db.task_get_or_create(
            url=ctx.url,
            source=ctx.source,
            from_chat_id=ctx.from_chat_id,
            from_user_id=ctx.from_user_id,
            message_id=ctx.message_id,
        )
        ctx.task_id = task_id

        if status != 'PENDING':
            self._log.info('Task %s already in status %s, skipping', task_id, status)
            return None

        await db.task_set_status(task_id, 'PROCESSING')

        try:
            # Download
            media = await self._download(ctx)

            # Post-process
            await self._post_process(media, ctx)

            await db.task_set_status(task_id, 'DONE')
            return media

        except Exception as e:
            self._log.exception('Download pipeline failed for %s', ctx.url)
            await db.task_set_status(task_id, 'FAILED', error=str(e))
            raise

    async def _download(self, ctx: DownloadContext) -> DownMedia:
        host_conf = HostConfig(ctx.url)

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._downloader.download(host_conf, ctx)),
                timeout=settings.DOWNLOAD_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise DownloadError(f'Download timed out after {settings.DOWNLOAD_TIMEOUT_SECONDS}s')

    async def _post_process(self, media: DownMedia, ctx: DownloadContext) -> None:
        import chuddy.db as db

        coros = []

        match media.media_type:
            case DownMediaType.AUDIO:
                coros.append(self._post_process_audio(media.audio))
            case DownMediaType.VIDEO:
                coros.append(self._post_process_video(media.video, ctx))
            case DownMediaType.AUDIO_VIDEO:
                coros.append(self._post_process_audio(media.audio))
                coros.append(self._post_process_video(media.video, ctx))

        await asyncio.gather(*coros, return_exceptions=True)

        # Save file records
        if media.audio:
            file_id = await db.file_create(
                task_id=ctx.task_id,
                file_type='AUDIO',
                filename=media.audio.current_filename,
                file_size=media.audio.current_file_size(),
                duration=media.audio.duration,
                title=media.audio.title,
            )
            media.audio.orm_file_id = file_id

        if media.video:
            file_id = await db.file_create(
                task_id=ctx.task_id,
                file_type='VIDEO',
                filename=media.video.current_filename,
                file_size=media.video.current_file_size(),
                duration=media.video.duration,
                width=media.video.width,
                height=media.video.height,
                thumb_path=str(media.video.thumb_path) if media.video.thumb_path else None,
                title=media.video.title,
            )
            media.video.orm_file_id = file_id

    async def _post_process_audio(self, audio: Audio) -> None:
        pass  # Audio is ready after download

    async def _post_process_video(self, video: Video, ctx: DownloadContext) -> None:
        coros = []

        # Fill missing metadata via ffprobe
        if not all([video.duration, video.width, video.height]):
            await self._probe_video(video)

        # Generate thumbnail if missing or wrong aspect ratio
        if not video.thumb_path or self._thumb_ar_mismatch(video):
            thumb_path = video.directory_path / f'{video.current_filename}-thumb.jpg'
            coros.append(self._make_thumbnail(video.current_filepath, thumb_path, video.duration))

        # H264 encode if needed (Instagram, Facebook)
        host_conf = HostConfig(ctx.url)
        if host_conf.encode_video and host_conf.ffmpeg_video_opts:
            coros.append(self._encode_h264(video, host_conf.ffmpeg_video_opts))

        # Copy to storage if requested
        if ctx.save_to_storage:
            coros.append(self._copy_to_storage(video))

        await asyncio.gather(*coros, return_exceptions=True)

    async def _probe_video(self, video: Video) -> None:
        """Get video metadata via ffprobe."""
        try:
            result = await asyncio.create_subprocess_exec(
                'ffprobe',
                '-v', 'error',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                str(video.current_filepath),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            if result.returncode != 0:
                return

            import json
            probe = json.loads(stdout)

            video.duration = float(probe['format']['duration'])

            video_streams = [s for s in probe.get('streams', []) if s.get('codec_type') == 'video']
            if video_streams:
                video.width = video_streams[0].get('width')
                video.height = video_streams[0].get('height')
            else:
                self._log.warning('No video stream found in %s', video.current_filepath)

        except Exception:
            self._log.exception('ffprobe failed for %s', video.current_filepath)

    def _thumb_ar_mismatch(self, video: Video) -> bool:
        """Check if thumbnail aspect ratio doesn't match video."""
        if not video.thumb_path or not video.aspect_ratio:
            return False
        try:
            from PIL import Image
            with Image.open(video.thumb_path) as img:
                tw, th = img.size
            g = _gcd(tw, th)
            thumb_ar = (tw // g, th // g)
            return thumb_ar != video.aspect_ratio
        except Exception:
            return False

    async def _make_thumbnail(self, video_path: Path, thumb_path: Path, duration: float | None) -> None:
        """Extract thumbnail frame from video."""
        ts = settings.THUMBNAIL_FRAME_SECOND
        if duration and duration > 0:
            ts = min(ts, duration * 0.25)  # 25% into video

        try:
            proc = await asyncio.create_subprocess_exec(
                'ffmpeg', '-y', '-loglevel', 'error',
                '-ss', str(ts),
                '-i', str(video_path),
                '-vframes', '1',
                '-vf', 'scale=320:-1',
                str(thumb_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0 and thumb_path.exists():
                self._log.info('Thumbnail created: %s', thumb_path)
        except Exception:
            self._log.exception('Thumbnail generation failed')

    async def _encode_h264(self, video: Video, ffmpeg_cmd_tpl: str) -> None:
        """Re-encode video to H264 if needed."""
        src = video.current_filepath
        dst = src.with_name(f'{src.stem}-h264{src.suffix}')

        cmd = ffmpeg_cmd_tpl.format(filepath=str(src), output=str(dst))
        self._log.info('H264 encode: %s', cmd)

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode().strip()
            self._log.error('H264 encode failed: %s', err)
            return

        if not dst.exists():
            self._log.error('H264 output not found: %s', dst)
            return

        self._log.info('H264 encoded: %s (%s)', dst.name, _human_size(dst))
        video.mark_as_converted(dst)

    async def _copy_to_storage(self, media) -> None:
        """Copy media file to persistent storage."""
        dst = settings.STORAGE_PATH / media.current_filename
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            dst = dst.parent / f'{stem}-{gen_random_str()}{suffix}'

        settings.STORAGE_PATH.mkdir(parents=True, exist_ok=True)

        try:
            await asyncio.to_thread(shutil.copy2, media.current_filepath, dst)
            media.mark_as_saved_to_storage(dst)
            self._log.info('Copied to storage: %s', dst)
        except Exception:
            self._log.exception('Storage copy failed for %s', media.current_filepath)


def _human_size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a
