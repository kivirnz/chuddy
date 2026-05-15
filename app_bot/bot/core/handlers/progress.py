import logging
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageIdInvalid, MessageNotModified
from yt_shared.schemas.progress import ProgressPayload
from yt_shared.utils.common import format_bytes

from bot.bot.client import VideoBotClient
from bot.core.utils import bold


def _progress_bar(percent: float, width: int = 12) -> str:
    filled = int(width * min(percent, 100) / 100)
    return f'[{("█" * filled).ljust(width)}]'


def _clean_filename(filename: str | None) -> str:
    if not filename:
        return 'media...'
    name = Path(filename).name
    if name.endswith('.part'):
        name = name[:-5]
    return name


class ProgressDownloadHandler:
    """Handle download progress payloads from RabbitMQ."""

    def __init__(self, bot: VideoBotClient, body: ProgressPayload) -> None:
        self._bot = bot
        self._body = body
        self._log = logging.getLogger(self.__class__.__name__)

    async def handle(self) -> None:
        if not self._body.from_chat_id or not self._body.ack_message_id:
            self._log.debug(
                'Skipping progress update: missing chat_id=%s or ack_msg_id=%s',
                self._body.from_chat_id,
                self._body.ack_message_id,
            )
            return

        filename = _clean_filename(self._body.filename)
        text = f'⬇️ {bold("Downloading")} {filename}\n'

        if self._body.progress_percent:
            try:
                pct = float(self._body.progress_percent.strip().replace('%', ''))
                bar = _progress_bar(pct)
                text += f'{bar} {self._body.progress_percent.strip()}'
            except (ValueError, AttributeError):
                pass

        if self._body.downloaded_bytes is not None and self._body.total_bytes:
            dl = format_bytes(self._body.downloaded_bytes)
            total = format_bytes(self._body.total_bytes)
            text += f'\n📦 {bold("Size:")} {dl} / {total}'
        elif self._body.downloaded_bytes is not None:
            text += f'\n📦 {bold("Downloaded:")} {format_bytes(self._body.downloaded_bytes)}'

        details = []
        if self._body.speed:
            details.append(f'🚀 {bold("Speed:")} {self._body.speed}')
        if self._body.eta:
            details.append(f'⏱ {bold("ETA:")} {self._body.eta}')
        if self._body.elapsed is not None and self._body.elapsed > 0:
            elapsed_str = f'{int(self._body.elapsed // 60)}m {int(self._body.elapsed % 60)}s' if self._body.elapsed >= 60 else f'{self._body.elapsed:.1f}s'
            details.append(f'⏳ {bold("Elapsed:")} {elapsed_str}')
        if details:
            text += '\n' + '  |  '.join(details)

        try:
            await self._bot.edit_message_text(
                chat_id=self._body.from_chat_id,
                message_id=self._body.ack_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except MessageNotModified:
            pass
        except MessageIdInvalid:
            self._log.debug(
                'Message %s in chat %s not found for progress update',
                self._body.ack_message_id,
                self._body.from_chat_id,
            )
        except Exception as err:
            self._log.error('Failed to update progress message: %s', err)
