import sqlalchemy as sa

from yt_shared.db.session import Base


class PunkChat(Base):
    __tablename__ = 'punk_chat'

    id = sa.Column(sa.BigInteger, autoincrement=True, primary_key=True, nullable=False)
    chat_id = sa.Column(sa.BigInteger, nullable=False, unique=True, index=True)
    enabled = sa.Column(sa.Boolean, nullable=False, default=False)
