from yt_shared.rabbit.rabbit_config import PROGRESS_QUEUE
from yt_shared.schemas.progress import ProgressPayload
from yt_shared.utils.tasks.tasks import create_task

from bot.core.handlers.progress import ProgressDownloadHandler
from bot.core.workers.abstract import AbstractDownloadResultWorker, RabbitWorkerType


class ProgressDownloadResultWorker(AbstractDownloadResultWorker):
    """RabbitMQ worker that processes download progress events."""

    TYPE = RabbitWorkerType.PROGRESS
    QUEUE_TYPE = PROGRESS_QUEUE
    SCHEMA_CLS = (ProgressPayload,)
    HANDLER_CLS = ProgressDownloadHandler

    async def _process_body(self, body: ProgressPayload) -> None:
        self._log.info(
            'Received download progress for chat %s: %s %s',
            body.from_chat_id,
            body.filename,
            body.progress_percent,
        )
        self._spawn_handler_task(body)

    def _spawn_handler_task(self, body: ProgressPayload) -> None:
        task_name = self.HANDLER_CLS.__class__.__name__
        create_task(
            self.HANDLER_CLS(body=body, bot=self._bot).handle(),
            task_name=task_name,
            logger=self._log,
            exception_message='Task "%s" raised an exception',
            exception_message_args=(task_name,),
        )
