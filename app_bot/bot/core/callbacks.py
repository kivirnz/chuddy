import asyncio
import json
import logging
import os
import random
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import Message
from yt_shared.db.session import get_db
from yt_shared.emoji import SUCCESS_EMOJI
from yt_shared.repositories.allowed_chat import AllowedChatRepository
from yt_shared.repositories.punk_chat import PunkChatRepository

from bot.bot.client import VideoBotClient
from bot.core.service import UrlParser, UrlService
from bot.core.utils import bold, get_user_id

_BASE_DIR = Path(__file__).parent.parent.parent
_MILK_DIR = _BASE_DIR / 'milk'
_TFD_FILE = _BASE_DIR / 'tfd.txt'
_TRACKER_FILE = _BASE_DIR / 'command_tracker.json'

_PUNK_SLASH_COMMANDS = {
    '/intruder_self_deprecation',
    '/yaman_phone_laptop',
    '/arsen_configoored',
    '/arsen_didnt_fuck',
    '/arsen_mentioned_nix',
    '/arsen_vs_ansi',
}

_GOOD_BOT_REPLIES = [
    'At your service, sir',
    't-thwanks s-senpaii *starts twerking*',
    'Good human',
]


class TelegramCallback:
    _MSG_SEND_OK: str = (
        f'{SUCCESS_EMOJI} {bold("{count}URL{plural} sent for download")}'
    )
    _MSG_SEND_FAIL: str = f'🛑 {bold("Failed to send URL for download")}'

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._url_parser = UrlParser()
        self._url_service = UrlService()
        self._punk_mode: dict[int, bool] = {}
        self._command_tracker: dict[str, list[str]] = {}
        self._load_tracker()
        self._load_punk_mode()

    def _load_punk_mode(self) -> None:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        async def _load() -> None:
            async for db in get_db():
                repo = PunkChatRepository(db)
                chat_ids = await repo.get_all_enabled_chat_ids()
                self._punk_mode = {cid: True for cid in chat_ids}
                self._log.info('Loaded punk mode from DB: %s', self._punk_mode)

        if loop and loop.is_running():
            asyncio.ensure_future(_load())
        else:
            asyncio.run(_load())

    def _is_punk_enabled(self, chat_id: int) -> bool:
        return self._punk_mode.get(chat_id, False)

    def _load_tracker(self) -> None:
        try:
            if _TRACKER_FILE.exists():
                self._command_tracker = json.loads(_TRACKER_FILE.read_text(encoding='utf-8'))
        except Exception:
            self._log.exception('Failed to load command tracker')
            self._command_tracker = {}

    def _save_tracker(self) -> None:
        try:
            _TRACKER_FILE.write_text(json.dumps(self._command_tracker, indent=2), encoding='utf-8')
        except Exception:
            self._log.exception('Failed to save command tracker')

    def _record_command(self, chat_id: int, command: str) -> tuple[bool, bool, int | None]:
        now = datetime.now(UTC)
        now_date = now.date()
        now_str = now.isoformat()
        key = f'{chat_id}:{command}'
        timestamps = self._command_tracker.get(key, [])

        month_timestamps = [
            ts for ts in timestamps
            if datetime.fromisoformat(ts).date().replace(day=1) == now_date.replace(day=1)
        ]
        first_this_month = len(month_timestamps) == 0

        today_timestamps = [
            ts for ts in timestamps
            if datetime.fromisoformat(ts).date() == now_date
        ]
        already_today = len(today_timestamps) > 0

        days_since_last = None
        if timestamps:
            last = datetime.fromisoformat(timestamps[-1])
            days_since_last = (now_date - last.date()).days

        timestamps.append(now_str)
        self._command_tracker[key] = timestamps
        self._save_tracker()

        return first_this_month, already_today, days_since_last

    @staticmethod
    async def on_start(client: VideoBotClient, message: Message) -> None:  # noqa: ARG004
        await message.reply(
            bold('Send video URL to start processing'),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )

    @staticmethod
    async def on_add(client: VideoBotClient, message: Message) -> None:
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
        async for db in get_db():
            repo = AllowedChatRepository(db)
            await repo.add_chat(chat_id)
        client.allowed_chats.add(chat_id)
        await message.reply(f'{bold("Added")} <code>{chat_id}</code> to allowlist.', parse_mode=ParseMode.HTML)

    @staticmethod
    async def on_remove(client: VideoBotClient, message: Message) -> None:
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
        async for db in get_db():
            repo = AllowedChatRepository(db)
            removed = await repo.remove_chat(chat_id)
        client.allowed_chats.discard(chat_id)
        if removed:
            await message.reply(f'{bold("Removed")} <code>{chat_id}</code> from allowlist.', parse_mode=ParseMode.HTML)
        else:
            await message.reply(f'<code>{chat_id}</code> was not in the allowlist.', parse_mode=ParseMode.HTML)

    async def on_message(self, client: VideoBotClient, message: Message) -> None:
        self._log.debug('Received Telegram Message: %s', message)
        text = message.text or message.caption or ''
        if not text:
            self._log.debug('Forwarded message, skipping')
            return

        stripped = text.strip()

        if stripped == '.punk':
            await self._handle_punk_toggle(client, message)
            return

        if stripped == '.help' or stripped.startswith('.help\n') or '\n.help' in text:
            help_msg = (
                f'{bold(".v <URL>")} - Download the URL as a video (playable in Telegram)\n'
                f'{bold(".a <URL>")} - Download the URL as audio (playable in Telegram)\n'
                f'{bold(".tr <text>")} - Translates text from any language into English (powered by Google Translate API)\n'
                f'{bold(".ocr")} - OCR + translate text on an image to English (reply to image or caption an image, powered by Yandex Translate API) {bold("EXPERIMENTAL")}\n'
                f'{bold(".help")} - Show this menu again\n'
                f'\n'
                f'Powered by <a href=\"https://github.com/kivirnz/chuddy/tree/mommy\">{bold("Chuddy")}</a>. Made with &lt;3 by Riley (@hicuckomori).'
            )
            await message.reply(help_msg, parse_mode=ParseMode.HTML)
            return

        if stripped.startswith('.tr'):
            from bot.core.translator import TranslatorService

            translate_text = None
            reply_target = message

            if stripped == '.tr' and message.reply_to_message:
                reply_msg = message.reply_to_message
                translate_text = reply_msg.text or reply_msg.caption
                reply_target = reply_msg
            elif stripped.startswith('.tr '):
                translate_text = stripped[4:].strip()

            if translate_text:
                translator = TranslatorService()
                translated = await translator.translate(translate_text)
                if translated:
                    await reply_target.reply(translated, reply_to_message_id=reply_target.id)
                else:
                    await message.reply("Failed to translate text.")
            return

        if stripped == '.ocr':
            await self._handle_ocr(client, message)
            return

        from yt_shared.enums import DownMediaType

        url_media_types: dict[str, DownMediaType] = {}
        urls = []
        reply_target_id = message.id

        if stripped in ('.v', '.a') and message.reply_to_message:
            reply_msg = message.reply_to_message
            reply_text = reply_msg.text or reply_msg.caption or ''
            if reply_text:
                media_type = DownMediaType.VIDEO if stripped == '.v' else DownMediaType.AUDIO
                extracted_urls = re.findall(r'https?://[^\s]+', reply_text)
                for url in extracted_urls:
                    urls.append(url)
                    url_media_types[url] = media_type
                reply_target_id = reply_msg.id
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

        user = client.allowed_users[get_user_id(message)]
        if user.use_url_regex_match:
            urls = self._url_parser.filter_urls(
                urls=urls, regexes=client.conf.telegram.url_validation_regexes
            )
            if not urls:
                self._log.debug('No urls to download, skipping message')
                return

        ack_message = await self._send_acknowledge_message(
            message=message, url_count=len(urls), reply_to_message_id=reply_target_id
        )
        context = {
            'message': message,
            'user': user,
            'ack_message': ack_message,
            'reply_target_id': reply_target_id,
        }
        url_objects = self._url_parser.parse_urls(urls=urls, context=context, url_media_types=url_media_types)
        await self._url_service.process_urls(url_objects)

    async def _handle_punk_toggle(self, client: VideoBotClient, message: Message) -> None:
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None
        if user_id not in client.admin_users:
            return
        current = self._is_punk_enabled(chat_id)
        new_state = not current
        self._punk_mode[chat_id] = new_state
        async for db in get_db():
            repo = PunkChatRepository(db)
            await repo.set_punk_mode(chat_id, new_state)
        if new_state:
            await message.reply('Enabled nigga yhurr')
        else:
            await message.reply('Goodbye :c')

    async def on_punk_message(self, client: VideoBotClient, message: Message) -> None:
        chat_id = message.chat.id
        if not self._is_punk_enabled(chat_id):
            return

        text = message.text or message.caption or ''

        if text:
            command = text.strip().split()[0].lower().split('@')[0]
            if command in _PUNK_SLASH_COMMANDS:
                first_this_month, already_today, days_since_last = self._record_command(chat_id, command)
                if first_this_month:
                    await message.reply("First one this month.")
                elif already_today:
                    await message.reply("Not the first time today.")
                else:
                    await message.reply(f"Last time it happened was {days_since_last} days ago.")
                return

        text_lower = text.lower()

        if text_lower:
            if re.search(r'\bfurries?\b', text_lower):
                line = self._get_random_tfd_line()
                if line:
                    await message.reply(line)
                    return

            im_match = re.match(r"^i'm\s+(\S+)\s*$", text_lower)
            if im_match:
                word = im_match.group(1)
                filler_words = {
                    'a', 'an', 'the', 'i', "i'm", 'me', 'my', 'we', 'us', 'he', 'she',
                    'it', 'so', 'ok', 'not', 'just', 'back', 'home', 'here',
                    'there', 'now', 'still', 'only', 'also', 'going', 'goin',
                    'gonna', 'fucking', 'doing', 'trying', 'getting', 'being',
                }
                if len(word) >= 3 and word not in filler_words:
                    await message.reply(f"Hi {word}, I'm Chuddy")
                    return

            if re.search(r'\bbad bot\b', text_lower):
                await message.reply("Stupid human")
                return

            if re.search(r'\bgood bot\b', text_lower):
                await message.reply(random.choice(_GOOD_BOT_REPLIES))
                return

            if re.search(r'\bmilk\b', text_lower):
                await self._send_milk_image(message)
                return

            if re.search(r'\bprolly\b', text_lower):
                await message.reply(f"{random.randint(0, 100)}%")
                return

            if re.search(r'\blaptop\b', text_lower):
                await message.reply("Lenovo Thinkpad X62")
                return

        if message.document and message.document.mime_type == 'application/pdf':
            await self._handle_pdf_scan(message)

    def _get_random_tfd_line(self) -> str | None:
        try:
            lines = _TFD_FILE.read_text(encoding='utf-8').splitlines()
            lines = [l for l in lines if l.strip()]
            if lines:
                return random.choice(lines)
        except FileNotFoundError:
            self._log.warning('tfd.txt not found at %s', _TFD_FILE)
        return None

    async def _send_milk_image(self, message: Message) -> None:
        try:
            images = [
                f for f in _MILK_DIR.iterdir()
                if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.gif', '.webp')
            ]
            if images:
                await message.reply_photo(str(random.choice(images)))
            else:
                self._log.warning('No images found in milk directory %s', _MILK_DIR)
        except FileNotFoundError:
            self._log.warning('milk directory not found at %s', _MILK_DIR)

    async def _handle_pdf_scan(self, message: Message) -> None:
        try:
            file_path = await message.download(file_name='/tmp/')
            with open(file_path, 'rb') as f:
                content = f.read()
            if b'/JavaScript' in content or b'/JS' in content:
                await message.reply("This file DOES CONTAIN Javascript.")
            else:
                await message.reply("This file DOES NOT CONTAIN any Javascript.")
            os.remove(file_path)
        except Exception:
            self._log.exception('Failed to scan PDF')
            await message.reply("Failed to scan PDF file.")

    async def _handle_ocr(self, client: VideoBotClient, message: Message) -> None:
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

        status_msg = await message.reply('Processing image...', parse_mode=ParseMode.HTML)

        input_fd, input_path = tempfile.mkstemp(suffix='.jpg')
        output_fd, output_path = tempfile.mkstemp(suffix='.jpg')
        os.close(input_fd)
        os.close(output_fd)

        try:
            await client.download_media(photo_message, file_name=input_path)

            yandex_dir = Path(__file__).resolve().parent.parent.parent / 'yandex'
            script_path = str(yandex_dir / 'yandex-trans.py')
            proc = await asyncio.create_subprocess_exec(
                'python3', script_path, input_path, output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(yandex_dir),
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                self._log.error('yandex-trans.py failed (rc=%d): %s', proc.returncode, stderr.decode().strip())
                await status_msg.edit_text('Failed to process image. See logs for details.')
                return

            if not os.path.getsize(output_path):
                await status_msg.edit_text('Failed to process image.')
                return

            await message.reply_photo(
                output_path,
                reply_to_message_id=photo_message.id,
            )
            await status_msg.delete()
        except Exception:
            self._log.exception('OCR processing failed')
            await status_msg.edit_text('Failed to process image. See logs for details.')
        finally:
            for path in (input_path, output_path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    async def _send_acknowledge_message(
        self, message: Message, url_count: int, reply_to_message_id: int | None = None
    ) -> Message:
        return await message.reply(
            text=self._format_acknowledge_text(url_count),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id or message.id,
        )

    def _format_acknowledge_text(self, url_count: int) -> str:
        is_multiple = url_count > 1
        return self._MSG_SEND_OK.format(
            count=f'{url_count} ' if is_multiple else '',
            plural='s' if is_multiple else '',
        )
