import sqlalchemy as sa

from yt_shared.db.session import Base


class AllowedChat(Base):
    __tablename__ = 'allowed_chat'

    id = sa.Column(sa.BigInteger, autoincrement=True, primary_key=True, nullable=False)
    chat_id = sa.Column(sa.BigInteger, nullable=False, unique=True, index=True)
