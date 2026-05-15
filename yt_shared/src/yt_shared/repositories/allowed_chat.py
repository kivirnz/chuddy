import logging

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from yt_shared.models import AllowedChat


class AllowedChatRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._db = db

    async def get_all_chat_ids(self) -> set[int]:
        result = await self._db.execute(select(AllowedChat.chat_id))
        return set(result.scalars().all())

    async def add_chat(self, chat_id: int) -> None:
        stmt = insert(AllowedChat).values(chat_id=chat_id)
        stmt = stmt.on_conflict_do_nothing(index_elements=['chat_id'])
        await self._db.execute(stmt)
        await self._db.commit()

    async def remove_chat(self, chat_id: int) -> bool:
        stmt = delete(AllowedChat).where(AllowedChat.chat_id == chat_id).returning(AllowedChat.id)
        result = await self._db.execute(stmt)
        await self._db.commit()
        return result.first() is not None
