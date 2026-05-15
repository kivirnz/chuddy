import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from itertools import chain
from typing import TYPE_CHECKING

from pydantic import ConfigDict, FilePath
from pyrogram.enums import ChatAction, MessageMediaType, ParseMode
from pyrogram.types import Animation, Message
from pyrogram.types import Audio as _Audio
from pyrogram.types import Video as _Video
from tenacity import retry, stop_after_attempt, wait_fixed
from yt_shared.db.session import get_db
from yt_shared.repositories.task import TaskRepository
from yt_shared.schemas.base import RealBaseModel
from yt_shared.schemas.cache import CacheSchema
from yt_shared.schemas.media import BaseMedia, Video
from yt_shared.schemas.success import SuccessDownloadPayload
from yt_shared.utils.tasks.abstract import AbstractTask
from yt_shared.utils.tasks.tasks import create_task
from yt_shared.utils.common import format_bytes

from bot.core.config.config import get_main_config, settings
from bot.core.schemas import AnonymousUserSchema, UserSchema, VideoCaptionSchema
from bot.core.utils import bold, is_user_upload_silent

if TYPE_CHECKING:
    from bot.bot.client import VideoBotClient


def _progress_bar(percent: float, width: int = 12) -> str:
    filled = int(width * min(percent, 100) / 100)
    return f'[{("█" * filled).ljust(width)}]'


class BaseUploadContext(RealBaseModel):
    model_config = ConfigDict(**RealBaseModel.model_config, strict=True)
    caption: str
    filename: str
    filepath: FilePath | str
    duration: float
    type: MessageMediaType
    is_cached: bool = False


class VideoUploadContext(BaseUploadContext):
    height: int | float
    width: int | float
    thumb: FilePath | str | None = None


class AudioUploadContext(BaseUploadContext):
    pass


class AbstractUploadTask(AbstractTask, ABC):
    _UPLOAD_ACTION: ChatAction

    def __init__(
        self,
        media_object: BaseMedia,
        users: list[AnonymousUserSchema | UserSchema],
        bot: 'VideoBotClient',
        semaphore: asyncio.Semaphore,
        context: SuccessDownloadPayload,
    ) -> None:
        super().__init__()
        self._config = get_main_config()
        self._media_object = media_object

        self._filename = media_object.current_filename
        self._filepath = media_object.current_filepath

        self._bot = bot
        self._users = users
        self._semaphore = semaphore
        self._ctx = context
        self._media_ctx = self._create_media_context()

        self._forward_chat_ids = self._get_forward_chat_ids()
        self._cached_message: Message | None = None

        self._ack_chat_id: int | None = context.from_chat_id
        self._ack_message_id: int | None = context.context.ack_message_id

    async def run(self) -> None:
        async with self._semaphore:
            self._log.debug('Semaphore for "%s" acquired', self._filename)
            await self._run()
        self._log.debug('Semaphore for "%s" released', self._filename)

    async def _run(self) -> None:
        try:
            self._upload_start_times = {}
            await self._send_upload_text()
            await self._upload_file()
        except Exception:
            self._log.exception('Exception in upload task for "%s"', self._filename)
            raise

    @abstractmethod
    def _generate_caption_items(self) -> list[str]:
        pass

    def _generate_file_caption(self) -> str:
        return '\n'.join(self._generate_caption_items())[: settings.TG_MAX_CAPTION_SIZE]

    async def _send_upload_text(self) -> None:
        text = (
            f'⬆️ {bold("Uploading")} {self._filename}\n'
            f'📏 {bold("Size")} {self._media_object.file_size_human()}\n'
            f'{_progress_bar(0)} 0.0%'
        )
        coros = []
        user_ids = []
        for user in self._users:
            if not is_user_upload_silent(user=user, conf=self._bot.conf):
                kwargs = {
                    'chat_id': user.id,
                    'text': text,
                    'parse_mode': ParseMode.HTML,
                }
                if self._ctx.message_id:
                    kwargs['reply_to_message_id'] = self._ctx.message_id
                coros.append(self._bot.send_message(**kwargs))
                user_ids.append(user.id)
                
        self._upload_messages = {}
        if coros:
            results = await asyncio.gather(*coros, return_exceptions=True)
            for uid, res in zip(user_ids, results):
                if isinstance(res, Message):
                    self._upload_messages[uid] = res
                else:
                    self._log.error('Failed to send upload text to %s: %s', uid, res)

    def _get_forward_chat_ids(self) -> list[int]:
        forward_chat_ids = []
        for user in self._users:
            if (
                isinstance(user, UserSchema)
                and user.upload.forward_to_group
                and user.upload.forward_group_id
            ):
                forward_chat_ids.append(user.upload.forward_group_id)
        return forward_chat_ids

    async def _progress_callback(self, current: int, total: int, chat_id: int) -> None:
        now = time.time()
        if not hasattr(self, '_last_update_times'):
            self._last_update_times = {}
            
        last_time = self._last_update_times.get(chat_id, 0)
        if now - last_time < 1.5 and current < total:
            return
            
        self._last_update_times[chat_id] = now

        percentage = (current / total) * 100 if total else 0
        speed = ''
        if hasattr(self, '_upload_start_times'):
            elapsed = now - self._upload_start_times.get(chat_id, now)
            if elapsed > 0 and current > 0:
                speed = f'  |  🚀 {bold("Speed:")} {format_bytes(int(current / elapsed))}/s'
        text = (
            f'⬆️ {bold("Uploading")} {self._filename}\n'
            f'📏 {bold("Size")} {self._media_object.file_size_human()}\n'
            f'{_progress_bar(percentage)} {percentage:.1f}% ({format_bytes(current)} / {format_bytes(total)})'
            f'{speed}'
        )
        
        message = getattr(self, '_upload_messages', {}).get(chat_id)
        if message:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message.id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                self._log.debug('Failed to edit progress message: %s', e)

        if self._ack_chat_id and self._ack_message_id:
            try:
                await self._bot.edit_message_text(
                    chat_id=self._ack_chat_id,
                    message_id=self._ack_message_id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                self._log.debug('Failed to edit ack progress message: %s', e)

    @retry(wait=wait_fixed(3), stop=stop_after_attempt(3), reraise=True)
    async def __upload(self, chat_id: int) -> Message | None:
        self._log.debug('Uploading to "%d" with context: %s', chat_id, self._media_ctx)
        return await self._generate_send_media_coroutine(chat_id)

    @abstractmethod
    def _generate_send_media_coroutine(self, chat_id: int) -> Coroutine:
        pass

    @abstractmethod
    def _create_media_context(self) -> AudioUploadContext | VideoUploadContext:
        pass

    async def _upload_file(self) -> None:
        chat_ids = dict.fromkeys(chain((u.id for u in self._users), self._forward_chat_ids))
        from bot.core.chat_action import ChatActionManager
        for chat_id in chat_ids:
            try:
                ChatActionManager().add_upload(chat_id)
                self._log.info(
                    'Uploading "%s" [%s] [cached: %s] to chat id "%d"',
                    self._filename,
                    self._media_object.file_size_human(),
                    self._media_ctx.is_cached,
                    chat_id,
                )
                if hasattr(self, '_upload_start_times'):
                    self._upload_start_times[chat_id] = time.time()
                try:
                    await self._bot.send_chat_action(chat_id, action=self._UPLOAD_ACTION)
                except Exception as e:
                    self._log.debug('Failed to send chat action: %s', e)
                
                try:
                    message = await self.__upload(chat_id=chat_id)
                except Exception as e:
                    self._log.error(
                        'Failed to upload "%s" to "%d": %s', self._media_ctx.filepath, chat_id, e
                    )
                    continue
            finally:
                ChatActionManager().remove_upload(chat_id)

            self._log.debug('Telegram response message: %s', message)
            if not self._cached_message and message:
                self._cache_data(message)

    def _create_cache_task(self, cache_object: _Audio | _Video | Animation) -> None:
        self._log.debug('Creating cache task for %s', cache_object)
        db_cache_task_name = 'Save cache to DB'
        create_task(
            self._save_cache_to_db(cache_object),
            task_name=db_cache_task_name,
            logger=self._log,
            exception_message='Task "%s" raised an exception',
            exception_message_args=(db_cache_task_name,),
        )

    @abstractmethod
    def _cache_data(self, message: Message) -> None:
        pass

    async def _save_cache_to_db(self, file: _Audio | _Video | Animation) -> None:
        cache = CacheSchema(
            cache_id=file.file_id,
            cache_unique_id=file.file_unique_id,
            file_size=file.file_size,
            date_timestamp=file.date,
        )

        async for db in get_db():
            await TaskRepository(db=db).save_file_cache(
                cache=cache, file_id=self._media_object.orm_file_id
            )


class AudioUploadTask(AbstractUploadTask):
    _UPLOAD_ACTION = ChatAction.UPLOAD_AUDIO
    _media_ctx: AudioUploadContext

    def _generate_send_media_coroutine(self, chat_id: int) -> Coroutine:
        kwargs = {
            'chat_id': chat_id,
            'audio': self._media_ctx.filepath,
            'caption': self._media_ctx.caption,
            'file_name': self._media_ctx.filename,
            'duration': int(self._media_ctx.duration),
            'progress': self._progress_callback,
            'progress_args': (chat_id,),
        }
        return self._bot.send_audio(**kwargs)

    def _create_media_context(self) -> AudioUploadContext:
        return AudioUploadContext(
            caption=self._generate_file_caption(),
            filename=self._filename,
            filepath=self._filepath,
            duration=self._media_object.duration or 0.0,
            type=MessageMediaType.AUDIO,
        )

    def _cache_data(self, message: Message) -> None:
        self._log.info('Saving Telegram file cache')
        audio = message.audio
        if not audio:
            err_msg = 'Telegram message response does not contain audio'
            self._log.error('%s: %s', err_msg, message)
            raise RuntimeError(err_msg)

        self._media_ctx.type = message.media
        self._media_ctx.filepath = audio.file_id
        self._media_ctx.duration = audio.duration
        self._media_ctx.is_cached = True
        self._cached_message = message

        self._create_cache_task(cache_object=audio)

    def _generate_caption_items(self) -> list[str]:
        return [
            f'{bold("Title:")} {self._media_object.title}',
            f'{bold("URL:")} {self._ctx.context.url}',
        ]


class VideoUploadTask(AbstractUploadTask):
    _UPLOAD_ACTION = ChatAction.UPLOAD_VIDEO
    _media_ctx: VideoUploadContext
    _media_object: Video

    def _create_media_context(self) -> VideoUploadContext:
        return VideoUploadContext(
            caption=self._generate_file_caption(),
            filename=self._filename,
            filepath=self._filepath,
            duration=self._media_object.duration or 0.0,
            height=self._media_object.height or 0,
            width=self._media_object.width or 0,
            thumb=self._media_object.thumb_path,
            type=MessageMediaType.VIDEO,
        )

    def _get_caption_conf(self) -> VideoCaptionSchema:
        if isinstance(self._users[0], AnonymousUserSchema):
            return self._bot.conf.telegram.api.video_caption
        return self._users[0].upload.video_caption

    def _generate_caption_items(self) -> list[str]:
        caption_items = []
        caption_conf = self._get_caption_conf()

        if caption_conf.include_title:
            caption_items.append(self._media_object.title)
        if caption_conf.include_filename:
            caption_items.append(self._filename)
        if caption_conf.include_link:
            caption_items.append(self._ctx.context.url)
        if caption_conf.include_size:
            caption_items.append(self._media_object.file_size_human())
        return caption_items

    def _generate_send_media_coroutine(self, chat_id: int) -> Coroutine:
        kwargs = {
            'chat_id': chat_id,
            'caption': self._media_ctx.caption,
            'file_name': self._media_ctx.filename,
            'duration': int(self._media_ctx.duration),
            'height': int(self._media_ctx.height),
            'width': int(self._media_ctx.width),
            'parse_mode': ParseMode.DISABLED,
            'progress': self._progress_callback,
            'progress_args': (chat_id,),
        }

        if self._media_ctx.thumb:
            kwargs['thumb'] = self._media_ctx.thumb
        if self._media_ctx.type is MessageMediaType.ANIMATION:
            kwargs['animation'] = self._media_ctx.filepath
            return self._bot.send_animation(**kwargs)

        kwargs['video'] = self._media_ctx.filepath
        kwargs['supports_streaming'] = True
        return self._bot.send_video(**kwargs)

    def _cache_data(self, message: Message) -> None:
        self._log.info('Saving Telegram file cache')
        video = message.video or message.animation
        if not video:
            err_msg = 'Telegram message response does not contain video or animation'
            self._log.error('%s: %s', err_msg, message)
            raise RuntimeError(err_msg)

        self._media_ctx.type = message.media
        self._media_ctx.filepath = video.file_id
        try:
            self._media_ctx.thumb = video.thumbs[0].file_id
        except TypeError:
            # video.thumbs is None when no thumbnail
            self._log.warning('No thumbnail found for caching object')
        self._media_ctx.is_cached = True
        self._cached_message = message

        self._create_cache_task(cache_object=video)
