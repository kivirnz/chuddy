"""Chuddy Telegram bot - single-process media download bot."""

import asyncio
import json
import logging
import os
import random
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ChatType, ParseMode
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import chuddy.db as db
from chuddy.config import BotConfig, UserConf, get_config, settings
from chuddy.constants import SUCCESS_EMOJI
from chuddy.enums import DownMediaType
from chuddy.media import MediaService
from chuddy.models import DownloadContext
from chuddy.stfu import is_stfu, toggle_stfu
from chuddy.uploader import Uploader, UploadContext, _progress_bar
from chuddy.utils import bold, extract_urls, filter_urls, format_bytes, preprocess_url, remove_dir, sanitize_url
from chuddy.ytdl_opts import HostConfig

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).parent
_MILK_DIR = _BASE_DIR / 'milk'
_TFD_FILE = _BASE_DIR / 'tfd.txt'
_YANDEX_DIR = _BASE_DIR / 'yandex'
_TRACKER_FILE = _BASE_DIR / 'command_tracker.json'
_NN_FILE = _BASE_DIR / 'nn.jpg'

# ── Punk mode constants ──────────────────────────────────────────────

_PUNK_SLASH_COMMANDS = {
    '/intruder_self_deprecation',
    '/intruder_heightism',
    '/yaman_phone_laptop',
    '/yaman_end_it_all',
    '/yaman_unfunny_reel',
    '/yaman_settings_page',
    '/arsen_configoored',
    '/arsen_mentioned_nix',
    '/arsen_mentioned_fp',
    '/arsen_mentioned_ai',
    '/ansi_didnt_fuck',
    '/henkka_reinstalled_os',
}

_GOOD_BOT_REPLIES = [
    'At your service, sir',
    't-thwanks s-senpaii *starts twerking*',
    'Good human',
    'thank youu!!',
]

_BAD_BOT_REPLIES = [
    'Stupid human',
    'Lord, have mercy: they do not know what they are saying 🙏🙏🙏',
    'Lick my nuts',
    'no u',
    'seethe dilate cope freetards btfo',
    'GO FUCK YOURSELF',
]

# ── Command tracker (file-based persistence) ─────────────────────────


class CommandTracker:
    """Track punk slash command invocations per chat."""

    def __init__(self):
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        if _TRACKER_FILE.exists():
            try:
                self._data = json.loads(_TRACKER_FILE.read_text(encoding='utf-8'))
            except Exception:
                self._data = {}

    def _save(self):
        try:
            _TRACKER_FILE.write_text(json.dumps(self._data, indent=2), encoding='utf-8')
        except Exception:
            pass

    def record(self, chat_id: int, command: str) -> tuple[bool, bool, int | None]:
        """Record command invocation. Returns (first_this_month, already_today, days_since_last)."""
        now = datetime.now(UTC)
        now_date = now.date()
        now_str = now.isoformat()
        key = f'{chat_id}:{command}'
        timestamps = self._data.get(key, [])

        month_ts = [
            ts for ts in timestamps
            if datetime.fromisoformat(ts).date().replace(day=1) == now_date.replace(day=1)
        ]
        first_this_month = len(month_ts) == 0

        today_ts = [
            ts for ts in timestamps
            if datetime.fromisoformat(ts).date() == now_date
        ]
        already_today = len(today_ts) > 0

        days_since_last = None
        if timestamps:
            last = datetime.fromisoformat(timestamps[-1])
            days_since_last = (now_date - last.date()).days

        timestamps.append(now_str)
        self._data[key] = timestamps
        self._save()

        return first_this_month, already_today, days_since_last


# ── Punk mode state ──────────────────────────────────────────────────


class PunkState:
    """Punk mode per-chat tracking."""

    def __init__(self):
        self._enabled: dict[int, bool] = {}

    async def load(self):
        chat_ids = await db.punk_get_all_enabled()
        self._enabled = {cid: True for cid in chat_ids}

    def is_enabled(self, chat_id: int) -> bool:
        return self._enabled.get(chat_id, False)

    async def toggle(self, chat_id: int) -> bool:
        new_state = not self._enabled.get(chat_id, False)
        self._enabled[chat_id] = new_state
        await db.punk_set(chat_id, new_state)
        return new_state


# ── Helper: get random tfd line ──────────────────────────────────────


def _get_random_tfd_line() -> str | None:
    try:
        lines = _TFD_FILE.read_text(encoding='utf-8').splitlines()
        lines = [l for l in lines if l.strip()]
        return random.choice(lines) if lines else None
    except Exception:
        return None


# ── Bot class ────────────────────────────────────────────────────────


class ChuddyBot(Client):
    """Main bot class combining all functionality."""

    def __init__(self, config: BotConfig):
        super().__init__(
            'chuddy',
            api_id=config.telegram.api_id,
            api_hash=config.telegram.api_hash,
            bot_token=config.telegram.token,
            lang_code=config.telegram.lang_code,
        )
        self._log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self._media_service = MediaService()

        # User/chat tracking
        self.allowed_users: dict[int, UserConf] = {}
        self.admin_users: dict[int, UserConf] = {}
        self.allowed_chats: set[int] = set()

        for user in config.telegram.allowed_users:
            self.allowed_users[user.id] = user
            if user.is_admin:
                self.admin_users[user.id] = user

        # State
        self._punk = PunkState()
        self._tracker = CommandTracker()
        self._semaphore = asyncio.Semaphore(config.telegram.max_upload_tasks)

    async def load_allowed_chats(self):
        self.allowed_chats = await db.allowed_chats_get_all()
        self._log.info('Loaded %d allowed chats', len(self.allowed_chats))

    def _is_allowed(self, message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else None
        chat_id = message.chat.id
        if user_id in self.allowed_users:
            return True
        if chat_id in self.allowed_users:
            return True
        if chat_id in self.allowed_chats:
            return True
        return False

    def _get_user_conf(self, message: Message) -> UserConf:
        user_id = message.from_user.id if message.from_user else None
        if user_id and user_id in self.allowed_users:
            return self.allowed_users[user_id]
        chat_id = message.chat.id
        if chat_id in self.allowed_users:
            return self.allowed_users[chat_id]
        # Default user for unknown senders
        return UserConf(
            id=user_id or chat_id,
            is_admin=False,
            send_startup_message=False,
            download_media_type=DownMediaType.VIDEO,
            save_to_storage=False,
            use_url_regex_match=False,
        )

    # ── Handler setup ────────────────────────────────────────────────

    def _setup_handlers(self):
        # /start, /help
        self.add_handler(MessageHandler(
            self._on_start,
            filters=filters.command(['start', 'help']) & self._dm_filter(),
        ))

        # stfu chuddy
        self.add_handler(MessageHandler(
            self._on_stfu,
            filters=filters.regex(r'(?i)^stfu\s+chuddy\s*$'),
        ))

        # /debug
        self.add_handler(MessageHandler(
            self._on_debug,
            filters=filters.command(['debug']) & self._dm_filter(),
        ))

        # /add, /remove (admin only)
        self.add_handler(MessageHandler(
            self._on_add,
            filters=filters.user(list(self.admin_users.keys())) & filters.command('add'),
        ))
        self.add_handler(MessageHandler(
            self._on_remove,
            filters=filters.user(list(self.admin_users.keys())) & filters.command('remove'),
        ))

        # Media commands (.v, .a, .tr, .help, .punk, .ocr) in groups/channels
        media_re = r'(?m)^\.(?:[va](?:\s+|$)|t[rd](?:\s+|$)|help(?:\s+|$)|punk(?:\s+|$)|ocr(?:\s+|$))'
        self.add_handler(MessageHandler(
            self._on_message,
            filters=filters.regex(media_re) & self._dm_filter() & self._allowlist_filter(),
        ))

        # DM commands (no punk command in DMs)
        dm_re = r'(?m)^\.(?:[va](?:\s+|$)|t[rd](?:\s+|$)|help(?:\s+|$)|ocr(?:\s+|$))'
        self.add_handler(MessageHandler(
            self._on_dm,
            filters=filters.regex(dm_re) & filters.private & self._dm_filter(),
        ))

        # Punk mode messages
        self.add_handler(MessageHandler(
            self._on_punk,
            filters=(
                (filters.chat(list(self.allowed_users.keys())) | self._chat_filter())
                & ~filters.regex(media_re)
            ),
        ))

    def _dm_filter(self):
        allowed = set(self.allowed_users.keys())
        dm_whitelist = getattr(self.config.telegram, 'dm_whitelist', set()) or set()

        class _DmFilter(filters.Filter):
            async def __call__(self, client, message: Message):
                uid = message.from_user.id if message.from_user else None
                cid = message.chat.id
                return (
                    uid in allowed or cid in allowed
                    or cid in client.allowed_chats
                    or (uid and uid in dm_whitelist)
                )

        return _DmFilter()

    def _allowlist_filter(self):
        allowed = set(self.allowed_users.keys())

        class _AllowFilter(filters.Filter):
            async def __call__(self, client, message: Message):
                cid = message.chat.id
                return cid in allowed or cid in client.allowed_chats

        return _AllowFilter()

    def _chat_filter(self):
        allowed = set(self.allowed_users.keys())

        class _ChatFilter(filters.Filter):
            async def __call__(self, client, message: Message):
                cid = message.chat.id
                return cid in allowed or cid in client.allowed_chats

        return _ChatFilter()

    # ── Command handlers ─────────────────────────────────────────────

    async def _on_start(self, _, message: Message):
        help_text = (
            f'{bold(".v <URL>")} - Download the URL as a video\n'
            f'{bold(".a <URL>")} - Download the URL as audio\n'
            f'{bold(".tr <text>")} - Translate text to English (Google Translate)\n'
            f'{bold(".ocr")} - OCR + translate image text to English {bold("EXPERIMENTAL")}\n'
            f'{bold(".help")} - Show this menu\n'
            f'{bold("stfu chuddy")} - Toggle minimal output mode'
        )
        await message.reply(help_text, parse_mode=ParseMode.HTML)

    async def _on_stfu(self, _, message: Message):
        chat_id = message.chat.id
        toggle_stfu(chat_id)
        try:
            await message.reply('з:')
        except Exception:
            self._log.debug('Failed to reply to stfu toggle')

    async def _on_debug(self, _, message: Message):
        chat_id = message.chat.id
        if is_stfu(chat_id):
            toggle_stfu(chat_id)
            await message.reply(
                f'{bold("Debug mode:")} STFU mode disabled for this chat.',
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.reply(
                f'{bold("Debug mode:")} STFU mode is already off.',
                parse_mode=ParseMode.HTML,
            )

    @staticmethod
    async def _on_add(_, message: Message):
        text = (message.text or '').strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(f'{bold("Usage:")} /add <chat_id>', parse_mode=ParseMode.HTML)
            return
        try:
            chat_id = int(parts[1].strip())
        except ValueError:
            await message.reply(f'{bold("Invalid chat ID.")} Must be an integer.', parse_mode=ParseMode.HTML)
            return
        await db.allowed_chats_add(chat_id)
        await message.reply(f'{bold("Added")} <code>{chat_id}</code> to allowlist.', parse_mode=ParseMode.HTML)

    @staticmethod
    async def _on_remove(_, message: Message):
        text = (message.text or '').strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(f'{bold("Usage:")} /remove <chat_id>', parse_mode=ParseMode.HTML)
            return
        try:
            chat_id = int(parts[1].strip())
        except ValueError:
            await message.reply(f'{bold("Invalid chat ID.")} Must be an integer.', parse_mode=ParseMode.HTML)
            return
        await db.allowed_chats_remove(chat_id)
        await message.reply(f'{bold("Removed")} <code>{chat_id}</code> from allowlist.', parse_mode=ParseMode.HTML)

    # ── Main message handler ─────────────────────────────────────────

    async def _on_message(self, _, message: Message):
        self._log.debug('Received message: %s', message)
        await self._handle_message(message, is_dm=False)

    async def _on_dm(self, _, message: Message):
        self._log.debug('Received DM: %s', message)
        await self._handle_message(message, is_dm=True)

    async def _handle_message(self, message: Message, is_dm: bool):
        text = message.text or message.caption or ''
        if not text:
            return

        stripped = text.strip()

        # .punk toggle
        if stripped == '.punk':
            await self._handle_punk_toggle(message)
            return

        # .help
        if stripped == '.help' or stripped.startswith('.help\n') or '\n.help' in text:
            help_text = (
                f'{bold(".v <URL>")} - Download the URL as a video\n'
                f'{bold(".a <URL>")} - Download the URL as audio\n'
                f'{bold(".tr <text>")} - Translate text to English (Google Translate)\n'
                f'{bold(".ocr")} - OCR + translate image text to English {bold("EXPERIMENTAL")}\n'
                f'{bold(".help")} - Show this menu\n'
                f'{bold("stfu chuddy")} - Toggle minimal output mode'
            )
            await message.reply(help_text, parse_mode=ParseMode.HTML)
            return

        # .tr (translate)
        if stripped.startswith('.tr'):
            await self._handle_translate(message, stripped)
            return

        # .ocr
        if stripped == '.ocr':
            await self._handle_ocr(message)
            return

        # Parse .v / .a URLs
        urls = []
        url_media_types: dict[str, DownMediaType] = {}
        reply_target_id = message.id

        if stripped in ('.v', '.a') and message.reply_to_message:
            reply_text = message.reply_to_message.text or message.reply_to_message.caption or ''
            if reply_text:
                mt = DownMediaType.VIDEO if stripped == '.v' else DownMediaType.AUDIO
                for url in extract_urls(reply_text):
                    urls.append(url)
                    url_media_types[url] = mt
                reply_target_id = message.reply_to_message.id
        else:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('.v '):
                    url = line[3:].strip()
                    urls.append(url)
                    url_media_types[url] = DownMediaType.VIDEO
                elif line.startswith('.a '):
                    url = line[3:].strip()
                    urls.append(url)
                    url_media_types[url] = DownMediaType.AUDIO

        if not urls:
            return

        user_conf = self._get_user_conf(message)
        if user_conf.use_url_regex_match:
            urls = filter_urls(urls, self.config.telegram.url_validation_regexes)
            if not urls:
                self._log.debug('No URLs matched validation regexes')
                return

        # Expand any playlist URLs into their individual entries so each video
        # is uploaded separately instead of being collapsed into one.
        from chuddy import streamer
        loop = asyncio.get_running_loop()
        queue: list[tuple[str, DownMediaType]] = []
        for url in urls:
            media_type = url_media_types.get(preprocess_url(url), user_conf.download_media_type)
            try:
                entries = await loop.run_in_executor(
                    None, streamer.expand_playlist, preprocess_url(url)
                )
            except Exception:
                entries = [url]
            for sub in entries:
                queue.append((sub, media_type))

        # Send ack
        ack = await self._send_ack(message, len(queue), reply_target_id)

        # Process each URL
        for url, media_type in queue:
            if media_type == DownMediaType.VIDEO:
                await self._stream_video(
                    message=message,
                    url=url,
                    user_conf=user_conf,
                    ack_message=ack,
                    reply_target_id=reply_target_id,
                )
            else:
                await self._download_and_upload(
                    message=message,
                    url=url,
                    media_type=media_type,
                    user_conf=user_conf,
                    ack_message=ack,
                    reply_target_id=reply_target_id,
                )

    # ── Streaming video pipeline ──────────────────────────────────────

    async def _stream_video(
        self,
        message: Message,
        url: str,
        user_conf: UserConf,
        ack_message: Message | None,
        reply_target_id: int,
    ):
        """Stream a video URL straight into Telegram without a full disk download.

        Falls back to the legacy download-then-upload pipeline when the source
        can't be streamed as a single file (HLS/DASH) or its size is unknown.
        """
        from chuddy import streamer

        chat_id = message.chat.id
        stfu = is_stfu(chat_id)
        loop = asyncio.get_running_loop()

        stream_file = None
        thumb_path: Path | None = None
        progress_msg: Message | None = None
        processed = preprocess_url(url)

        try:
            if not stfu:
                try:
                    await self.send_chat_action(chat_id, action='typing')
                except Exception:
                    pass

            info = await asyncio.wait_for(
                loop.run_in_executor(None, streamer.extract_stream, processed),
                timeout=120,
            )

            if info is None or not info.streamable or not info.url:
                self._log.info('Source not streamable (%s), falling back to legacy pipeline', url)
                await self._download_and_upload(
                    message=message,
                    url=url,
                    media_type=DownMediaType.VIDEO,
                    user_conf=user_conf,
                    ack_message=ack_message,
                    reply_target_id=reply_target_id,
                )
                return

            size = info.filesize
            if not size:
                size = await loop.run_in_executor(None, streamer.resolve_size, info.url, info.http_headers)
            if not size or size <= 0:
                self._log.info('Could not determine stream size (%s), falling back', url)
                await self._download_and_upload(
                    message=message,
                    url=url,
                    media_type=DownMediaType.VIDEO,
                    user_conf=user_conf,
                    ack_message=ack_message,
                    reply_target_id=reply_target_id,
                )
                return

            thumb_path = await loop.run_in_executor(None, streamer.prepare_thumbnail, info.thumb_url)

            filename = streamer.safe_filename(info.title, info.ext)
            caption = self._stream_caption(url, info, size, filename, user_conf, stfu)

            stream_file = streamer.StreamingFile(info.url, size, headers=info.http_headers, name=filename)

            if not stfu:
                progress_msg = await self._send_stream_progress(
                    chat_id, reply_target_id, filename, size
                )

            try:
                await self.send_chat_action(chat_id, action=ChatAction.UPLOAD_VIDEO)
            except Exception:
                pass

            async with self._semaphore:
                sent = await self.send_video(
                    chat_id=chat_id,
                    video=stream_file,
                    caption=caption,
                    file_name=filename,
                    duration=int(info.duration or 0),
                    height=int(info.height or 0),
                    width=int(info.width or 0),
                    thumb=str(thumb_path) if thumb_path else None,
                    supports_streaming=True,
                    parse_mode=ParseMode.DISABLED,
                    progress=self._stream_progress,
                    progress_args=(chat_id, progress_msg, filename, size, stream_file),
                    reply_to_message_id=reply_target_id,
                )

            if stfu and reply_target_id:
                try:
                    await self.send_reaction(chat_id=chat_id, message_id=reply_target_id, emoji='🌭')
                except Exception:
                    self._log.debug('Failed to react after stream upload')

            if sent and sent.video:
                try:
                    await self._record_stream_file(processed, info, size, sent, message)
                except Exception:
                    self._log.debug('Failed to record streamed file')

        except Exception as e:
            self._log.exception('Streaming upload failed for %s', url)
            await self._handle_error(message, url, e, stfu)
        finally:
            if stream_file is not None:
                try:
                    stream_file.close()
                except Exception:
                    pass
            if thumb_path is not None:
                try:
                    thumb_path.unlink(missing_ok=True)
                except Exception:
                    pass
            if progress_msg is not None:
                try:
                    await self.delete_messages(chat_id, [progress_msg.id])
                except Exception:
                    pass
            if ack_message is not None:
                try:
                    await self.delete_messages(chat_id, [ack_message.id])
                except Exception:
                    pass

    @staticmethod
    def _stream_caption(
        url: str, info, size: int, filename: str, user_conf: UserConf, stfu: bool
    ) -> str:
        link = sanitize_url(url)
        if stfu:
            return link
        items: list[str] = []
        cap = user_conf.upload.video_caption
        if cap.include_title and info.title:
            items.append(info.title)
        if cap.include_filename:
            items.append(filename)
        if cap.include_link:
            items.append(link)
        if cap.include_size:
            items.append(format_bytes(size))
        return '\n'.join(items)[:settings.TG_MAX_CAPTION_SIZE]

    async def _send_stream_progress(
        self, chat_id: int, reply_to: int, filename: str, size: int
    ) -> Message | None:
        text = (
            f'⬆️ {bold("Streaming")} {filename}\n'
            f'📏 {bold("Size")} {format_bytes(size)}\n'
            f'{_progress_bar(0)} 0.0%'
        )
        try:
            return await self.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to
            )
        except Exception:
            return None

    async def _stream_progress(
        self, current: int, total: int, chat_id: int, progress_msg: Message | None,
        filename: str, size: int, stream_file,
    ) -> None:
        if is_stfu(chat_id) or progress_msg is None:
            return
        now = asyncio.get_event_loop().time()
        last = getattr(progress_msg, '_chuddy_last', 0.0)
        if now - last < 1.5 and current < total:
            return
        setattr(progress_msg, '_chuddy_last', now)

        pct = (current / total * 100) if total else 0
        fetched = getattr(stream_file, 'fetched', 0) or 0
        dl_pct = (fetched / total * 100) if total else 0
        text = (
            f'⬆️ {bold("Streaming")} {filename}\n'
            f'📏 {bold("Size")} {format_bytes(size)}\n'
            f'{_progress_bar(pct)} {pct:.1f}% uploaded  |  ⬇️ {dl_pct:.1f}% fetched'
        )
        try:
            await self.edit_message_text(chat_id, progress_msg.id, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    async def _record_stream_file(self, url: str, info, size: int, sent: Message, message: Message):
        """Persist a lightweight task/file record for streamed uploads."""
        try:
            task_id, _ = await db.task_get_or_create(
                url=url,
                source='BOT',
                from_chat_id=message.chat.id,
                from_user_id=message.from_user.id if message.from_user else None,
                message_id=sent.id,
            )
            await db.task_set_status(task_id, 'DONE')
            file = sent.video
            await db.file_create(
                task_id=task_id,
                file_type='VIDEO',
                filename=getattr(file, 'file_name', None) or info.title,
                file_size=getattr(file, 'file_size', None) or size,
                duration=info.duration,
                width=info.width,
                height=info.height,
                title=info.title,
            )
        except Exception:
            self._log.debug('Stream file record failed', exc_info=True)

    # ── Download & Upload pipeline ───────────────────────────────────

    async def _download_and_upload(
        self,
        message: Message,
        url: str,
        media_type: DownMediaType,
        user_conf: UserConf,
        ack_message: Message | None,
        reply_target_id: int,
    ):
        chat_id = message.chat.id
        processed = preprocess_url(url)
        stfu = is_stfu(chat_id)

        ctx = DownloadContext(
            url=processed,
            original_url=url,
            from_chat_id=chat_id,
            from_chat_type=message.chat.type.value,
            from_user_id=message.from_user.id if message.from_user else None,
            message_id=reply_target_id,
            ack_message_id=ack_message.id if ack_message else None,
            save_to_storage=user_conf.save_to_storage,
            download_media_type=media_type,
        )

        try:
            # Show typing during download
            if not stfu:
                try:
                    await self.send_chat_action(chat_id, action='typing')
                except Exception:
                    pass

            media = await self._media_service.process(ctx)
            if not media:
                self._log.info('Media already being processed, skipping %s', url)
                return

            # Build upload context
            upload_ctx = UploadContext(
                media=media.video if media.video else media.audio,
                url=url,
                from_chat_id=chat_id,
                message_id=reply_target_id,
                ack_message_id=ack_message.id if ack_message else None,
                # Upload to the chat where the request was made, NOT the user's personal ID
                target_chat_id=chat_id,
                forward_group_id=user_conf.upload.forward_group_id if user_conf.upload.forward_to_group else None,
                include_title=user_conf.upload.video_caption.include_title,
                include_filename=user_conf.upload.video_caption.include_filename,
                include_link=user_conf.upload.video_caption.include_link,
                include_size=user_conf.upload.video_caption.include_size,
                max_file_size=user_conf.upload.upload_video_max_file_size,
                silent=user_conf.upload.silent,
                stfu=stfu,
            )

            async with self._semaphore:
                progress_msgs = await Uploader(self, upload_ctx).run()

            # Delete the upload progress messages after completion
            for msg in progress_msgs:
                try:
                    await self.delete_messages(chat_id, [msg.id])
                except Exception:
                    pass

        except Exception as e:
            self._log.exception('Download/upload failed for %s', url)
            await self._handle_error(message, url, e, stfu)
        finally:
            # Delete ack message
            if ack_message:
                try:
                    await self.delete_messages(chat_id, [ack_message.id])
                except Exception:
                    pass
            # Clean up temp files
            try:
                root = settings.TMP_DOWNLOAD_ROOT_PATH / 'downloaded'
                if root.exists():
                    for d in root.iterdir():
                        if d.is_dir():
                            remove_dir(d)
            except Exception:
                pass

    async def _handle_error(self, message: Message, url: str, error: Exception, stfu: bool):
        chat_id = message.chat.id
        if stfu:
            if settings.STFU_STICKER_ID:
                try:
                    await message.reply_sticker(settings.STFU_STICKER_ID, reply_to_message_id=message.id)
                except Exception:
                    self._log.debug('Failed to send error sticker')
        else:
            error_text = f'🛑 {bold("Failed to download")} {url}\n{bold("Error:")} {type(error).__name__}'
            await message.reply(error_text, parse_mode=ParseMode.HTML, reply_to_message_id=message.id)

    # ── Helpers ──────────────────────────────────────────────────────

    async def _send_ack(self, message: Message, url_count: int, reply_to: int) -> Message | None:
        chat_id = message.chat.id
        if is_stfu(chat_id):
            try:
                await message.react('🌭')
            except Exception:
                pass
            return None
        plural = 's' if url_count != 1 else ''
        text = f'{SUCCESS_EMOJI} {bold(f"{url_count} URL{plural} sent for download")}'
        try:
            return await message.reply(text, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to)
        except Exception:
            return None

    # ── Translate ────────────────────────────────────────────────────

    async def _handle_translate(self, message: Message, stripped: str):
        translate_text = None
        reply_target = message

        if stripped == '.tr' and message.reply_to_message:
            reply_msg = message.reply_to_message
            translate_text = reply_msg.text or reply_msg.caption
            reply_target = reply_msg
        elif stripped.startswith('.tr '):
            translate_text = stripped[4:].strip()

        if not translate_text:
            return

        translated = await self._translate_text(translate_text)
        if translated:
            await reply_target.reply(translated, reply_to_message_id=reply_target.id)
        else:
            await message.reply('Failed to translate text.')

    @staticmethod
    async def _translate_text(text: str) -> str | None:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    'https://translate.googleapis.com/translate_a/single',
                    params={'client': 'g', 'sl': 'auto', 'tl': 'en', 'dt': 't', 'q': text},
                ) as resp:
                    data = await resp.json()
            result = ''.join(item[0] for item in data[0] if item[0])
            return result if result else None
        except Exception:
            logger.exception('Translation failed')
            return None

    # ── OCR ──────────────────────────────────────────────────────────

    async def _handle_ocr(self, message: Message):
        photo_message = None
        if message.photo:
            photo_message = message
        elif message.reply_to_message and message.reply_to_message.photo:
            photo_message = message.reply_to_message

        if not photo_message:
            await message.reply(
                f'{bold("Usage:")} Send an image with caption `.ocr` or reply `.ocr` to an image.',
                parse_mode=ParseMode.HTML,
            )
            return

        chat_id = message.chat.id
        stfu = is_stfu(chat_id)
        if stfu:
            try:
                await message.react('🌭')
            except Exception:
                pass
            status_msg = None
        else:
            status_msg = await message.reply('Processing image...', parse_mode=ParseMode.HTML)

        input_fd, input_path = tempfile.mkstemp(suffix='.jpg')
        output_fd, output_path = tempfile.mkstemp(suffix='.jpg')
        os.close(input_fd)
        os.close(output_fd)

        actual_input = None
        try:
            actual_input = await self.download_media(photo_message, file_name=input_path)
            if not actual_input:
                self._log.error('download_media returned None for OCR photo')
                if status_msg:
                    await status_msg.edit_text('Failed to process image.')
                return

            script_path = _YANDEX_DIR / 'yandex-trans.py'
            if not script_path.exists():
                if status_msg:
                    await status_msg.edit_text('OCR script not found.')
                return

            proc = await asyncio.create_subprocess_exec(
                'python3', str(script_path), actual_input, output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(_YANDEX_DIR),
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                self._log.error('yandex-trans.py failed (rc=%d): %s', proc.returncode, stderr.decode().strip())
                if status_msg:
                    await status_msg.edit_text('Failed to process image.')
                return

            if not os.path.getsize(output_path):
                if status_msg:
                    await status_msg.edit_text('Failed to process image.')
                return

            await message.reply_photo(photo=output_path, reply_to_message_id=photo_message.id)
            if status_msg:
                await status_msg.delete()
        except Exception:
            self._log.exception('OCR processing failed')
            if status_msg:
                await status_msg.edit_text('Failed to process image.')
        finally:
            cleanup_paths = {input_path, output_path}
            if actual_input:
                cleanup_paths.add(actual_input)
            for path in cleanup_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ── Punk mode ────────────────────────────────────────────────────

    async def _handle_punk_toggle(self, message: Message):
        if message.chat.type == ChatType.PRIVATE:
            return
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        if user_id not in self.admin_users:
            return

        new_state = await self._punk.toggle(chat_id)
        if new_state:
            await message.reply('Enabled nigga yhurr')
        else:
            await message.reply('Goodbye :c')

    @staticmethod
    def _has_ycombinator_link(message: Message) -> bool:
        """True if the message references ycombinator.com anywhere.

        Covers the raw body text, hyperlinks whose visible text hides the URL
        (Telegram ``text_link`` entities), and standalone link previews.
        """
        candidates: list[str] = []

        body = message.text or message.caption or ''
        if body:
            candidates.append(body.lower())

        for ent in (message.entities or []) + (message.caption_entities or []):
            url = getattr(ent, 'url', None)
            if url:
                candidates.append(url.lower())

        preview = getattr(message, 'link_preview_options', None)
        if preview and getattr(preview, 'url', None):
            candidates.append(preview.url.lower())

        return any('ycombinator.com' in c for c in candidates)

    async def _on_punk(self, _, message: Message):
        chat_id = message.chat.id
        if not self._punk.is_enabled(chat_id):
            return

        # Hacker News / Y Combinator -> reply with nn.jpg. Matches the URL
        # whether it shows as plain text, a hyperlink whose display text hides
        # the actual URL, or a standalone link preview.
        if _NN_FILE.exists() and self._has_ycombinator_link(message):
            try:
                await message.reply_photo(str(_NN_FILE), reply_to_message_id=message.id)
            except Exception:
                self._log.debug('Failed to reply with nn.jpg')
            return

        text = message.text or message.caption or ''
        if not text:
            # Check for document (PDF)
            if message.document and message.document.mime_type == 'application/pdf':
                await self._handle_pdf(message)
            return

        text_lower = text.lower()

        # Slash commands
        command = text.strip().split()[0].lower().split('@')[0]
        if command in _PUNK_SLASH_COMMANDS:
            first, already, days = self._tracker.record(chat_id, command)
            if first:
                await message.reply('First one this month.')
            elif already:
                await message.reply('Not the first time today.')
            else:
                await message.reply(f'Last time it happened was {days} days ago.')
            return

        # "furry" / "furries"
        if re.search(r'\bfurr(?:y|ies?)\b', text_lower):
            line = _get_random_tfd_line()
            if line:
                await message.reply(line)
                return

        # "I'm X" → "Hi X, I'm Chuddy"
        im_match = re.match(r"^i'm\s+(\S+)\s*$", text_lower)
        if im_match:
            word = im_match.group(1)
            filler = {
                'a', 'an', 'the', 'i', "i'm", 'me', 'my', 'we', 'us', 'he', 'she',
                'it', 'so', 'ok', 'not', 'just', 'back', 'home', 'here',
                'there', 'now', 'only', 'also', 'going', 'goin', 'gonna',
                'fucking', 'doing', 'trying', 'getting', 'being',
            }
            if len(word) >= 3 and word not in filler:
                await message.reply(f"Hi {word}, I'm Chuddy")
                return

        # good/bad bot
        if re.search(r'\bbad bot\b', text_lower):
            await message.reply(random.choice(_BAD_BOT_REPLIES))
            return
        if re.search(r'\bgood bot\b', text_lower):
            await message.reply(random.choice(_GOOD_BOT_REPLIES))
            return

        # milk
        if re.search(r'\bmilk\b', text_lower):
            await self._send_milk(message)
            return

        # prolly
        if re.search(r'\bprolly\b', text_lower):
            await message.reply(f'{random.randint(0, 100)}%')
            return

        # laptop
        if re.search(r'\blaptop\b', text_lower):
            if random.random() < 0.5:
                brand = random.choice(['Lenovo', 'IBM'])
                series = random.choice(['X', 'W', 'T'])
                num = random.randint(100, 999)
                await message.reply(f'{brand} Thinkpad {series}{num}')
            return

        # PDF
        if message.document and message.document.mime_type == 'application/pdf':
            await self._handle_pdf(message)

    async def _send_milk(self, message: Message):
        try:
            images = [
                f for f in _MILK_DIR.iterdir()
                if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp')
            ]
            if images:
                await message.reply_photo(str(random.choice(images)))
        except Exception:
            pass

    async def _handle_pdf(self, message: Message):
        try:
            file_path = await self.download_media(message, file_name='/tmp/')
            with open(file_path, 'rb') as f:
                content = f.read()
            if b'/JavaScript' in content or b'/JS' in content:
                await message.reply('This file DOES CONTAIN Javascript.')
            else:
                await message.reply('This file DOES NOT CONTAIN any Javascript.')
            os.remove(file_path)
        except Exception:
            self._log.exception('PDF scan failed')
            await message.reply('Failed to scan PDF file.')

    # ── Startup ──────────────────────────────────────────────────────

    async def start_bot(self):
        await db.init_schema()
        await self.load_allowed_chats()
        await self._punk.load()
        self._setup_handlers()
        await self.start()
        self._log.info('Chuddy bot starting')
        await self._send_startup_message()

        # Periodic DB cleanup
        asyncio.create_task(self._db_cleanup_loop())

    async def _send_startup_message(self):
        user_ids = [
            u.id for u in self.allowed_users.values() if u.send_startup_message
        ]
        if user_ids:
            try:
                await self.send_message_to_users('Chuddy online', user_ids)
            except Exception:
                pass

    async def send_message_to_users(self, text: str, user_ids: list[int]):
        coros = []
        for uid in user_ids:
            coros.append(self.send_message(uid, text, parse_mode=ParseMode.HTML))
        results = await asyncio.gather(*coros, return_exceptions=True)
        for uid, r in zip(user_ids, results):
            if isinstance(r, Exception):
                self._log.debug('Failed to send to %d: %s', uid, r)

    async def _db_cleanup_loop(self):
        """Periodically clean up old tasks from the database."""
        while True:
            await asyncio.sleep(86400)  # Once per day
            try:
                deleted = await db.cleanup_old_tasks(days=30)
                if deleted:
                    self._log.info('Cleaned up %d old tasks', deleted)
            except Exception:
                self._log.exception('DB cleanup failed')
