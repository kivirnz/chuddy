import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from yt_shared.models import PunkChat


class PunkChatRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._db = db

    async def get_all_enabled_chat_ids(self) -> set[int]:
        result = await self._db.execute(
            select(PunkChat.chat_id).where(PunkChat.enabled.is_(True))
        )
        return set(result.scalars().all())

    async def set_punk_mode(self, chat_id: int, enabled: bool) -> None:
        stmt = insert(PunkChat).values(chat_id=chat_id, enabled=enabled)
        stmt = stmt.on_conflict_do_update(
            index_elements=['chat_id'],
            set_={'enabled': enabled},
        )
        await self._db.execute(stmt)
        await self._db.commit()
