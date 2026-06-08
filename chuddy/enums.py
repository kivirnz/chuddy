from enum import StrEnum, unique


@unique
class TaskStatus(StrEnum):
    PENDING = 'PENDING'
    PROCESSING = 'PROCESSING'
    FAILED = 'FAILED'
    DONE = 'DONE'


@unique
class DownMediaType(StrEnum):
    AUDIO = 'AUDIO'
    VIDEO = 'VIDEO'
    AUDIO_VIDEO = 'AUDIO_VIDEO'


@unique
class MediaFileType(StrEnum):
    AUDIO = 'AUDIO'
    VIDEO = 'VIDEO'
