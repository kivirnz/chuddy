import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from pyrogram.enums import ChatAction
from yt_shared.utils.common import Singleton
from yt_shared.utils.tasks.tasks import create_task

if TYPE_CHECKING:
    from bot.bot.client import VideoBotClient


class ChatActionManager(metaclass=Singleton):
    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._bot: 'VideoBotClient | None' = None
        self._downloading: dict[int, int] = defaultdict(int)
        self._uploading: dict[int, int] = defaultdict(int)
        self._task: asyncio.Task | None = None

    def start(self, bot: 'VideoBotClient') -> None:
        if not self._bot:
            self._bot = bot
        if not self._task:
            self._task = create_task(self._loop(), task_name='ChatActionManagerLoop', logger=self._log)

    def add_download(self, chat_id: int) -> None:
        self._downloading[chat_id] += 1

    def remove_download(self, chat_id: int) -> None:
        if self._downloading[chat_id] > 0:
            self._downloading[chat_id] -= 1

    def add_upload(self, chat_id: int) -> None:
        self._uploading[chat_id] += 1

    def remove_upload(self, chat_id: int) -> None:
        if self._uploading[chat_id] > 0:
            self._uploading[chat_id] -= 1

    async def _loop(self) -> None:
        while True:
            for chat_id, count in list(self._uploading.items()):
                if count > 0:
                    try:
                        await self._bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                    except Exception as e:
                        self._log.debug('Failed to send upload chat action to %s: %s', chat_id, e)
            
            for chat_id, count in list(self._downloading.items()):
                if count > 0 and self._uploading[chat_id] == 0:
                    try:
                        await self._bot.send_chat_action(chat_id, ChatAction.TYPING)
                    except Exception as e:
                        self._log.debug('Failed to send typing chat action to %s: %s', chat_id, e)
            
            await asyncio.sleep(4)
