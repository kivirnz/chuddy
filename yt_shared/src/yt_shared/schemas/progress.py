from typing import Optional
from yt_shared.schemas.base import RealBaseModel

class ProgressPayload(RealBaseModel):
    task_id: str | None = None
    message_id: int | None = None
    ack_message_id: int | None = None
    from_chat_id: int | None = None
    status: str
    progress_percent: str | None = None
    speed: str | None = None
    eta: str | None = None
    filename: str | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    elapsed: float | None = None
