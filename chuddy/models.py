"""Pydantic data models for media and download context."""

import uuid
from abc import ABC
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, DirectoryPath, Field, FilePath, model_validator

from chuddy.enums import DownMediaType, MediaFileType
from chuddy.utils import format_bytes, file_size


class BaseMedia(BaseModel, ABC):
    """Abstract downloaded media."""

    model_config = ConfigDict(frozen=False)

    file_type: MediaFileType
    title: str
    original_filename: str
    directory_path: Path
    file_size: int
    duration: float | None = None

    saved_to_storage: bool = False
    storage_path: Path | None = None
    is_converted: bool = False
    converted_filename: str | None = None
    converted_file_size: int | None = None
    custom_filename: str | None = None
    orm_file_id: str | None = None

    def file_size_human(self) -> str:
        return format_bytes(self.current_file_size())

    def current_file_size(self) -> int:
        if self.converted_file_size is not None:
            return self.converted_file_size
        return self.file_size

    @property
    def current_filename(self) -> str:
        if self.custom_filename:
            return self.custom_filename
        if self.is_converted:
            return self.converted_filename
        return self.original_filename

    @property
    def current_filepath(self) -> Path:
        return self.directory_path / self.current_filename

    def mark_as_saved_to_storage(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.saved_to_storage = True

    def mark_as_converted(self, filepath: Path) -> None:
        self.converted_filename = filepath.name
        self.converted_file_size = file_size(filepath)
        self.is_converted = True


class Audio(BaseMedia):
    file_type: Literal[MediaFileType.AUDIO] = MediaFileType.AUDIO


class Video(BaseMedia):
    file_type: Literal[MediaFileType.VIDEO] = MediaFileType.VIDEO
    thumb_name: str | None = None
    width: int | float | None = None
    height: int | float | None = None
    thumb_path: FilePath | None = None

    @model_validator(mode='after')
    def set_thumb_name(self) -> 'Video':
        if not self.thumb_name:
            self.thumb_name = f'{self.current_filename}-thumb.jpg'
        return self

    @property
    def aspect_ratio(self) -> tuple[int, int] | None:
        if self.width and self.height:
            w, h = int(self.width), int(self.height)
            g = _gcd(w, h)
            return (w // g, h // g)
        return None


class DownMedia(BaseModel):
    """Container for downloaded media."""

    audio: Audio | None
    video: Video | None
    media_type: DownMediaType
    root_path: Path
    meta: dict

    def get_media_objects(self) -> tuple[Audio | Video, ...]:
        return tuple(filter(None, (self.audio, self.video)))


class DownloadContext(BaseModel):
    """Context passed through the download pipeline."""

    url: str
    original_url: str
    from_chat_id: int | None
    from_chat_type: str | None
    from_user_id: int | None
    message_id: int | None
    ack_message_id: int | None
    save_to_storage: bool
    download_media_type: DownMediaType
    source: str = 'BOT'
    task_id: str | None = None


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a
