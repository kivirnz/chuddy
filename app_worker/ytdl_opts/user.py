"""User-specific `yt-dlp` download CLI options."""

from typing import Final

from worker.core.config import settings
from worker.utils import get_cookies_opts_if_not_empty

FINAL_AUDIO_FORMAT: Final[str] = 'mp3'
FINAL_THUMBNAIL_FORMAT: Final[str] = 'jpg'

type _OptsType = tuple[str, ...]

DEFAULT_YTDL_OPTS: Final[_OptsType] = (
    '--output',
    '%(uploader|Unknown)s - %(title).150B.%(ext)s',
    '--no-playlist',
    '--playlist-items',
    '1:1',
    '--concurrent-fragments',
    '10',
    '--downloader',
    'aria2c',
    '--downloader-args',
    'aria2c:-x 16 -s 16 -k 1M',
    '--ignore-errors',
    '--verbose',
    *get_cookies_opts_if_not_empty(),
)

DEFAULT_VIDEO_FORMAT_SORT_OPT: Final[_OptsType] = (
    '--format-sort',
    'res:1080,vcodec:h265,h264',
)

AUDIO_YTDL_OPTS: Final[_OptsType] = (
    '--extract-audio',
    '--audio-quality',
    '0',
    '--audio-format',
    FINAL_AUDIO_FORMAT,
)

AUDIO_FORMAT_YTDL_OPTS: Final[_OptsType] = ('--format', 'bestaudio/best')

VIDEO_YTDL_OPTS: Final[_OptsType] = (
    '--format',
    'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
    '--write-thumbnail',
    '--convert-thumbnails',
    FINAL_THUMBNAIL_FORMAT,
)
