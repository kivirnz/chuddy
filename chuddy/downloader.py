"""Media downloader using yt-dlp with progress tracking."""

import asyncio
import glob
import logging
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import yt_dlp

from chuddy.config import settings
from chuddy.enums import DownMediaType
from chuddy.models import Audio, DownMedia, DownloadContext, Video
from chuddy.utils import file_size, gen_random_str, remove_dir
from chuddy.ytdl_opts import FINAL_AUDIO_FORMAT, FINAL_THUMBNAIL_FORMAT, HostConfig


class DownloadError(Exception):
    pass


class MediaDownloader:
    """Download media via yt-dlp, handling progress and file resolution."""

    def __init__(self):
        self._log = logging.getLogger(self.__class__.__name__)

    def download(self, host_conf: HostConfig, ctx: DownloadContext) -> DownMedia:
        """Blocking download call. Run in executor for async usage."""
        self._log.info('Downloading %s (type=%s)', ctx.url, ctx.download_media_type)

        tmp_root = settings.TMP_DOWNLOAD_ROOT_PATH / 'downloading'
        tmp_root.mkdir(parents=True, exist_ok=True)

        with TemporaryDirectory(prefix='tmp_media-', dir=tmp_root) as tmp_dir:
            curr_tmp = Path(tmp_dir)
            ytdl_opts = host_conf.build_ytdl_opts(ctx.download_media_type, curr_tmp)
            ytdl_opts = dict(ytdl_opts)
            ytdl_opts['noprogress'] = False

            # Progress hook - edit messages in-place
            last_update = {'time': 0.0}
            progress_data = {'filename': None, 'percent': None, 'speed': None}

            def _progress_hook(d: dict) -> None:
                if d['status'] != 'downloading':
                    return
                now = time.time()
                if now - last_update['time'] < 1.5:
                    return
                last_update['time'] = now

                raw_fn = d.get('filename', '')
                clean_name = Path(raw_fn).name if raw_fn else None
                if clean_name and clean_name.endswith('.part'):
                    clean_name = clean_name[:-5]

                progress_data['filename'] = clean_name
                progress_data['percent'] = d.get('_percent_str')
                progress_data['speed'] = d.get('_speed_str')
                progress_data['eta'] = d.get('_eta_str')

                self._log.info('Progress: %s %s', clean_name, d.get('_percent_str', '?'))

            ytdl_opts['progress_hooks'] = [_progress_hook]

            with yt_dlp.YoutubeDL(ytdl_opts) as ytdl:
                self._log.info('Downloading "%s" → "%s"', host_conf.url, curr_tmp)

                # Actual download (single call — avoids double-extract issues on some sites)
                meta = ytdl.extract_info(host_conf.url, download=True)
                if not meta:
                    raise DownloadError(
                        'Download returned empty metadata. This usually means '
                        'the URL requires login, is geo-blocked, or is no longer available.'
                    )

                # Reject timer videos
                title = (meta.get('title') or '').lower()
                if 'timer' in title:
                    raise DownloadError(
                        f'Download rejected: video title contains "timer" — "{meta.get("title")}"'
                    )

                current_files = list(curr_tmp.iterdir())
                if not current_files:
                    raise DownloadError('Nothing downloaded. Is the URL valid?')

                meta = ytdl.sanitize_info(meta)

            self._log.info('Download complete: %s', host_conf.url)
            self._log.info('Files in %s: %s', curr_tmp, [f.name for f in curr_tmp.iterdir()])

            # Move to destination
            dest_dir = settings.TMP_DOWNLOAD_ROOT_PATH / 'downloaded' / gen_random_str(4)
            dest_dir.mkdir(parents=True, exist_ok=True)

            audio, video = self._resolve_media(
                media_type=ctx.download_media_type,
                meta=meta,
                curr_tmp=curr_tmp,
                dest_dir=dest_dir,
            )

            # Clean up temp dir (leftover files like original video when audio-extracted)
            remove_dir(curr_tmp)

            return DownMedia(
                media_type=ctx.download_media_type,
                audio=audio,
                video=video,
                meta=meta,
                root_path=dest_dir,
            )

    def _resolve_media(
        self,
        media_type: DownMediaType,
        meta: dict,
        curr_tmp: Path,
        dest_dir: Path,
    ) -> tuple[Audio | None, Video | None]:
        match media_type:
            case DownMediaType.AUDIO:
                return self._create_audio(meta, curr_tmp, dest_dir), None
            case DownMediaType.VIDEO:
                return None, self._create_video(meta, curr_tmp, dest_dir)
            case DownMediaType.AUDIO_VIDEO:
                return (
                    self._create_audio(meta, curr_tmp, dest_dir),
                    self._create_video(meta, curr_tmp, dest_dir),
                )
        raise RuntimeError(f'Unknown media type: {media_type}')

    def _create_audio(self, meta: dict, curr_tmp: Path, dest_dir: Path) -> Audio:
        filename = self._find_file(curr_tmp, FINAL_AUDIO_FORMAT)
        if not filename:
            raise DownloadError('Audio file not found after download')

        src = curr_tmp / filename
        dst = dest_dir / filename
        shutil.move(src, dst)

        return Audio(
            title=meta['title'],
            original_filename=filename,
            directory_path=dest_dir,
            file_size=file_size(dst),
            duration=self._to_float(meta.get('duration')),
        )

    def _create_video(self, meta: dict, curr_tmp: Path, dest_dir: Path) -> Video:
        filename = self._get_video_filename(meta)
        src = curr_tmp / filename
        dst = dest_dir / filename
        shutil.move(src, dst)

        thumb_name = self._find_file(curr_tmp, FINAL_THUMBNAIL_FORMAT)
        thumb_path: Path | None = None
        if thumb_name:
            shutil.move(curr_tmp / thumb_name, dest_dir)
            thumb_path = dest_dir / thumb_name

        duration, width, height = self._get_video_ctx(meta)

        return Video(
            title=meta['title'],
            original_filename=filename,
            directory_path=dest_dir,
            file_size=file_size(dst),
            duration=duration,
            width=width,
            height=height,
            thumb_path=thumb_path,
            thumb_name=thumb_name,
        )

    @staticmethod
    def _find_file(root: Path, ext: str) -> str | None:
        for fn in glob.glob(f'*.{ext}', root_dir=root):
            return fn
        return None

    def _get_video_filename(self, meta: dict) -> str:
        dl = self._get_requested_download(meta)
        try:
            filepath = dl['filepath']
        except (AttributeError, KeyError):
            filepath = dl.get('filename', dl.get('_filename', ''))
            if not filepath:
                raise DownloadError('Video filepath not found in metadata')
        return filepath.rsplit('/', maxsplit=1)[-1]

    def _get_requested_download(self, meta: dict) -> dict:
        if meta.get('_type') == 'playlist':
            if not meta.get('entries'):
                raise DownloadError('Playlist has no entries')
            downloads = meta['entries'][0].get('requested_downloads', [])
        else:
            downloads = meta.get('requested_downloads', [])

        for dl in downloads:
            if dl.get('ext', '') != FINAL_AUDIO_FORMAT:
                return dl

        # Fallback: video was converted to audio but original kept
        for dl in downloads:
            if dl.get('ext') != dl.get('_filename', '').rsplit('.', 1)[-1]:
                return dl

        raise DownloadError('No video download found in metadata')

    @staticmethod
    def _get_video_ctx(meta: dict) -> tuple[float | None, int | None, int | None]:
        if meta.get('_type') == 'playlist' and meta.get('entries'):
            entry = meta['entries'][0]
            dl = entry.get('requested_downloads', [{}])[0] if entry.get('requested_downloads') else {}
            return (
                _try_float(entry.get('duration')),
                dl.get('width'),
                dl.get('height'),
            )
        dl = meta.get('requested_downloads', [{}])[0] if meta.get('requested_downloads') else {}
        return (
            _try_float(meta.get('duration')),
            dl.get('width'),
            dl.get('height'),
        )

    def _to_float(self, val) -> float | None:
        return _try_float(val)


def _try_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
