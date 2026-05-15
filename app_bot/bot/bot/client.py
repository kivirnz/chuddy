import asyncio
import logging
from collections.abc import Iterable

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import RPCError
from yt_shared.db.session import get_db
from yt_shared.repositories.allowed_chat import AllowedChatRepository

from bot.core.schemas import ConfigSchema, UserSchema
from bot.core.utils import bold


class VideoBotClient(Client):
    """Extended Pyrogram's `Client` class."""

    _RUN_FOREVER_SLEEP_SECONDS: int = 86400

    def __init__(self, *args, conf: ConfigSchema, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._log = logging.getLogger(self.__class__.__name__)
        self._log.info('Initializing bot client')
        self.conf = conf

        self.allowed_users: dict[int, UserSchema] = {}
        self.admin_users: dict[int, UserSchema] = {}
        self.allowed_chats: set[int] = set()

        for user in self.conf.telegram.allowed_users:
            self.allowed_users[user.id] = user
            if user.is_admin:
                self.admin_users[user.id] = user

    async def load_allowed_chats(self) -> None:
        async for db in get_db():
            repo = AllowedChatRepository(db)
            self.allowed_chats = await repo.get_all_chat_ids()
        self._log.info('Loaded allowed chats from DB: %s', self.allowed_chats)

    async def run_forever(self) -> None:
        """Firstly, 'await bot.start()' should be called."""
        if not self.is_initialized:
            raise RuntimeError('Bot was not started (initialized).')
        while True:
            await asyncio.sleep(self._RUN_FOREVER_SLEEP_SECONDS)

    def get_startup_users(self) -> list[int]:
        user_ids: list[int] = []
        for user in self.allowed_users.values():
            if user.send_startup_message:
                user_ids.append(user.id)
        return user_ids

    async def send_startup_message(self) -> None:
        """Send welcome message after bot launch."""
        self._log.info('Sending welcome message')
        await self.send_message_to_users(
            text=(
                f'{bold((await self.get_me()).first_name)} started, '
                f'type {bold(".help")} to see all the available commands.\n'
                f'\n'
                f'<b>Powered by <a href="https://github.com/kivirnz/chuddy/tree/mommy">Chuddy</a>. Made with &lt;3 by Riley (@hicuckomori).</b>'
            ),
            user_ids=self.get_startup_users(),
        )

    async def send_message_to_users(
        self, text: str, user_ids: Iterable[int], parse_mode: ParseMode = ParseMode.HTML
    ) -> None:
        coros = []
        self._log.debug('Sending message "%s" to chat ids %s', text, user_ids)
        for user_id in user_ids:
            coros.append(self.send_message(user_id, text, parse_mode=parse_mode))
        results = await asyncio.gather(*coros, return_exceptions=True)
        for user_id, result in zip(user_ids, results, strict=False):
            if isinstance(result, RPCError):
                self._log.error('User %s did not receive message: %s', user_id, result)

    async def send_message_all(
        self, text: str, parse_mode: ParseMode = ParseMode.HTML
    ) -> None:
        """Send a message to all defined user IDs in config.json."""
        await self.send_message_to_users(
            text=text, user_ids=self.allowed_users.keys(), parse_mode=parse_mode
        )

    async def send_message_admins(
        self, text: str, parse_mode: ParseMode = ParseMode.HTML
    ) -> None:
        """Send a message to all defined user IDs in config.json."""
        await self.send_message_to_users(
            text=text, user_ids=self.admin_users.keys(), parse_mode=parse_mode
        )
