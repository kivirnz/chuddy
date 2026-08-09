"""Upload tasks: video/audio upload to Telegram with progress, caching, compression."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from pyrogram.enums import ChatAction, MessageMediaType, ParseMode
from pyrogram.types import Message

from chuddy.config import BotConfig, settings
from chuddy.models import Audio, Video, BaseMedia
from chuddy.stfu import is_stfu
from chuddy.utils import bold, format_bytes, remove_dir, sanitize_url

if False:  # type-only import
    from chuddy.bot import ChuddyBot


def _progress_bar(percent: float, width: int = 12) -> str:
    filled = int(width * min(percent, 100) / 100)
    return f'[{"█" * filled}{"." * (width - filled)}]'


class UploadContext:
    """Data needed for an upload operation."""

    def __init__(
        self,
        media: Audio | Video,
        url: str,
        from_chat_id: int | None,
        message_id: int | None,
        ack_message_id: int | None,
        target_chat_id: int,
        forward_group_id: int | None,
        include_title: bool,
        include_filename: bool,
        include_link: bool,
        include_size: bool,
        max_file_size: int,
        silent: bool,
        stfu: bool = False,
        source: str = 'BOT',
    ):
        self.media = media
        self.url = url
        self.from_chat_id = from_chat_id
        self.message_id = message_id
        self.ack_message_id = ack_message_id
        self.target_chat_id = target_chat_id
        self.forward_group_id = forward_group_id
        self.include_title = include_title
        self.include_filename = include_filename
        self.include_link = include_link
        self.include_size = include_size
        self.max_file_size = max_file_size
        self.silent = silent
        self.stfu = stfu
        self.source = source


class Uploader:
    """Handle upload with progress, caching, and compression."""

    def __init__(self, bot, ctx: UploadContext):
        self._bot = bot
        self._ctx = ctx
        self._log = logging.getLogger(self.__class__.__name__)
        self._upload_messages: dict[int, Message] = {}
        self._upload_start_times: dict[int, float] = {}
        self._last_update_times: dict[int, float] = {}

    async def run(self) -> list[Message]:
        media = self._ctx.media
        self._log.info(
            'Uploading "%s" [%s] to %d chat(s)',
            media.current_filename,
            media.file_size_human(),
            len(self._all_chat_ids()),
        )

        try:
            # Compress if oversized (video only)
            if isinstance(media, Video):
                await self._maybe_compress(media)

            # Upload to all target chats
            chat_ids = self._all_chat_ids()
            for chat_id in chat_ids:
                await self._upload_to_chat(chat_id)

            # STFU: react to original message
            if self._ctx.stfu and self._ctx.message_id:
                try:
                    await self._bot.send_reaction(
                        chat_id=self._ctx.from_chat_id,
                        message_id=self._ctx.message_id,
                        emoji='🌭',
                    )
                except Exception:
                    self._log.debug('Failed to react to original message')

        except Exception:
            self._log.exception('Upload failed for %s', media.current_filename)
            raise

        # Return progress messages so caller can delete them
        return list(self._upload_messages.values())

    def _all_chat_ids(self) -> list[int]:
        ids = [self._ctx.target_chat_id]
        if self._ctx.forward_group_id:
            ids.append(self._ctx.forward_group_id)
        return list(dict.fromkeys(ids))

    async def _maybe_compress(self, video: Video) -> None:
        if video.current_file_size() <= self._ctx.max_file_size:
            return

        self._log.info(
            'Video %s (%s) exceeds limit (%s), compressing...',
            video.current_filename,
            video.file_size_human(),
            format_bytes(self._ctx.max_file_size),
        )

        for attempt in range(3):
            crf = 28 + attempt * 4
            await self._compress(video, crf)
            if video.current_file_size() <= self._ctx.max_file_size:
                self._log.info(
                    'Compressed to %s (CRF %d)', video.file_size_human(), crf
                )
                return
            self._log.warning(
                'Still too large after attempt %d (CRF %d)', attempt + 1, crf
            )

        raise RuntimeError(
            f'Failed to compress "{video.current_filename}" below '
            f'{format_bytes(self._ctx.max_file_size)} after 3 attempts'
        )

    @staticmethod
    async def _compress(video: Video, crf: int) -> None:
        src = video.current_filepath
        dst = src.with_name(f'{src.stem}-compressed{src.suffix}')

        cmd = (
            f'ffmpeg -y -loglevel error -i "{src}" '
            f'-c:v libx264 -preset medium -crf {crf} '
            f'-pix_fmt yuv420p -movflags +faststart '
            f'-c:a aac -b:a 128k "{dst}"'
        )

        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f'FFmpeg failed (CRF {crf}): {stderr.decode().strip()}')
        if not dst.exists():
            raise RuntimeError(f'Compressed file not found: {dst}')

        video.mark_as_converted(dst)

    async def _upload_to_chat(self, chat_id: int) -> Message | None:
        # Send single upload text message (skip in STFU)
        if not self._ctx.stfu:
            await self._send_upload_text(chat_id)

        # Upload
        self._upload_start_times[chat_id] = time.time()
        try:
            await self._bot.send_chat_action(chat_id, action=self._upload_action())
        except Exception:
            pass

        message = await self._send_with_retry(chat_id)
        if not message:
            return None

        # Save cache for future dedup
        if isinstance(self._ctx.media, Video):
            file = message.video or message.animation
        else:
            file = message.audio

        if file:
            await self._save_cache(file)

    async def _send_upload_text(self, chat_id: int) -> None:
        media = self._ctx.media
        text = (
            f'⬆️ {bold("Uploading")} {media.current_filename}\n'
            f'📏 {bold("Size")} {media.file_size_human()}\n'
            f'{_progress_bar(0)} 0.0%'
        )
        kwargs: dict = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': ParseMode.HTML,
        }
        if self._ctx.message_id:
            kwargs['reply_to_message_id'] = self._ctx.message_id

        try:
            msg = await self._bot.send_message(**kwargs)
            self._upload_messages[chat_id] = msg
        except Exception as e:
            self._log.debug('Failed to send upload text to %d: %s', chat_id, e)

    async def _progress_callback(self, current: int, total: int, chat_id: int) -> None:
        if is_stfu(chat_id):
            return

        now = time.time()
        last = self._last_update_times.get(chat_id, 0)
        if now - last < 1.5 and current < total:
            return
        self._last_update_times[chat_id] = now

        pct = (current / total) * 100 if total else 0
        speed = ''
        elapsed = now - self._upload_start_times.get(chat_id, now)
        if elapsed > 0 and current > 0:
            speed = f'  |  🚀 {bold("Speed:")} {format_bytes(int(current / elapsed))}/s'

        text = (
            f'⬆️ {bold("Uploading")} {self._ctx.media.current_filename}\n'
            f'📏 {bold("Size")} {self._ctx.media.file_size_human()}\n'
            f'{_progress_bar(pct)} {pct:.1f}% ({format_bytes(current)} / {format_bytes(total)})'
            f'{speed}'
        )

        # Edit upload progress message
        msg = self._upload_messages.get(chat_id)
        if msg:
            try:
                await self._bot.edit_message_text(chat_id, msg.id, text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    async def _send_with_retry(self, chat_id: int) -> Message | None:
        """Try upload up to 3 times."""
        for attempt in range(3):
            try:
                return await self._send(chat_id)
            except Exception as e:
                self._log.error(
                    'Upload attempt %d failed for chat %d: %s',
                    attempt + 1, chat_id, e,
                )
                if attempt < 2:
                    await asyncio.sleep(3)
        return None

    async def _send(self, chat_id: int) -> Message | None:
        """Send media to a specific chat."""
        media = self._ctx.media
        progress_args = (chat_id,)
        caption = self._generate_caption()

        if isinstance(media, Video):
            kwargs = {
                'chat_id': chat_id,
                'video': str(media.current_filepath),
                'caption': caption,
                'file_name': media.current_filename,
                'duration': int(media.duration or 0),
                'height': int(media.height or 0),
                'width': int(media.width or 0),
                'supports_streaming': True,
                'parse_mode': ParseMode.DISABLED,
                'progress': self._progress_callback,
                'progress_args': progress_args,
            }
            if media.thumb_path:
                kwargs['thumb'] = str(media.thumb_path)
            return await self._bot.send_video(**kwargs)

        elif isinstance(media, Audio):
            kwargs = {
                'chat_id': chat_id,
                'audio': str(media.current_filepath),
                'caption': caption,
                'file_name': media.current_filename,
                'duration': int(media.duration or 0),
                'progress': self._progress_callback,
                'progress_args': progress_args,
            }
            return await self._bot.send_audio(**kwargs)

        return None

    def _generate_caption(self) -> str:
        items = []

        # STFU mode: URL only
        if self._ctx.from_chat_id and is_stfu(self._ctx.from_chat_id):
            return sanitize_url(self._ctx.url)

        c = self._ctx
        if c.include_title and isinstance(self._ctx.media, (Video, Audio)):
            items.append(self._ctx.media.title)
        if c.include_filename:
            items.append(self._ctx.media.current_filename)
        if c.include_link:
            items.append(sanitize_url(self._ctx.url))
        if c.include_size:
            items.append(self._ctx.media.file_size_human())

        return '\n'.join(items)[:settings.TG_MAX_CAPTION_SIZE]

    def _upload_action(self) -> ChatAction:
        if isinstance(self._ctx.media, Video):
            return ChatAction.UPLOAD_VIDEO
        return ChatAction.UPLOAD_AUDIO

    async def _save_cache(self, file) -> None:
        """Save Telegram file_id to DB cache for future dedup."""
        import chuddy.db as db

        try:
            if self._ctx.media.orm_file_id:
                await db.cache_save(
                    file_id=self._ctx.media.orm_file_id,
                    cache_id=file.file_id,
                    cache_unique_id=file.file_unique_id,
                    file_size=file.file_size,
                    date_timestamp=file.date or 0,
                )
                self._log.info('Cache saved: %s', file.file_unique_id)
        except Exception:
            self._log.debug('Failed to save cache', exc_info=True)
