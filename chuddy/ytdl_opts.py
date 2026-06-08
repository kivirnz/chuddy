"""yt-dlp options configuration per host.

Simplified: no metaclass registry. Just host -> options mapping.
"""

from pathlib import Path

from chuddy.constants import FACEBOOK_HOSTS, INSTAGRAM_HOSTS, TIKTOK_HOSTS, TWITTER_HOSTS
from chuddy.config import settings
from chuddy.enums import DownMediaType
from chuddy.utils import cli_to_api, get_cookies_file

# ── Default options ──────────────────────────────────────────────────

FINAL_AUDIO_FORMAT = 'mp3'
FINAL_THUMBNAIL_FORMAT = 'jpg'

COOKIES_OPT: tuple[str, str] | tuple[()] = ()
_cookies = get_cookies_file()
if _cookies:
    COOKIES_OPT = ('--cookies', str(_cookies))

DEFAULT_YTDL_OPTS: tuple[str, ...] = (
    '--output', '%(title).200B.%(ext)s',
    '--no-playlist',
    '--playlist-items', '1:1',
    '--concurrent-fragments', str(settings.MAX_DOWNLOAD_THREADS),
    '--ignore-errors',
    '--verbose',
    *COOKIES_OPT,
)

DEFAULT_VIDEO_FORMAT_SORT_OPT: tuple[str, ...] = (
    '--format-sort', 'res,vcodec:h265,h264',
)

AUDIO_YTDL_OPTS: tuple[str, ...] = (
    '--extract-audio',
    '--audio-quality', '0',
    '--audio-format', FINAL_AUDIO_FORMAT,
)

AUDIO_FORMAT_YTDL_OPTS: tuple[str, ...] = ('--format', 'bestaudio/best')

VIDEO_YTDL_OPTS: tuple[str, ...] = (
    '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4',
    '--write-thumbnail',
    '--convert-thumbnails', FINAL_THUMBNAIL_FORMAT,
)

# ── Host-specific options ────────────────────────────────────────────

# Host -> (encode_video, ffmpeg_video_opts, extra_format_sort)
HOST_CONFIGS: dict[str, dict] = {}

# Facebook / Instagram: VP9 → H264 encode needed (Telegram iOS doesn't play VP9)
FFMPEG_H264 = (
    'ffmpeg -y -loglevel error -i "{filepath}" '
    '-c:v libx264 -pix_fmt yuv420p -preset slow -threads 2 -crf 22 '
    '-movflags +faststart -c:a copy "{output}"'
)

for host in INSTAGRAM_HOSTS:
    HOST_CONFIGS[host] = {
        'encode_video': settings.INSTAGRAM_ENCODE_TO_H264,
        'ffmpeg_video_opts': FFMPEG_H264,
        'format_sort': DEFAULT_VIDEO_FORMAT_SORT_OPT,
    }

for host in FACEBOOK_HOSTS:
    HOST_CONFIGS[host] = {
        'encode_video': settings.FACEBOOK_ENCODE_TO_H264,
        'ffmpeg_video_opts': FFMPEG_H264,
        'format_sort': DEFAULT_VIDEO_FORMAT_SORT_OPT,
    }

for host in TIKTOK_HOSTS:
    HOST_CONFIGS[host] = {
        'encode_video': False,
        'ffmpeg_video_opts': None,
        'format_sort': DEFAULT_VIDEO_FORMAT_SORT_OPT,
    }

for host in TWITTER_HOSTS:
    HOST_CONFIGS[host] = {
        'encode_video': False,
        'ffmpeg_video_opts': None,
        'format_sort': ('--format-sort', 'res,proto:https,vcodec:h265,h264'),
    }

# ── Config builder ───────────────────────────────────────────────────


class HostConfig:
    """Resolved yt-dlp + FFmpeg config for a URL."""

    def __init__(self, url: str):
        from urllib.parse import urlsplit
        self.url = url
        netloc = urlsplit(url).netloc
        self._host_cfg = HOST_CONFIGS.get(netloc, {
            'encode_video': False,
            'ffmpeg_video_opts': None,
            'format_sort': DEFAULT_VIDEO_FORMAT_SORT_OPT,
        })

    @property
    def encode_video(self) -> bool:
        return self._host_cfg['encode_video']

    @property
    def ffmpeg_video_opts(self) -> str | None:
        return self._host_cfg['ffmpeg_video_opts']

    @property
    def format_sort_opt(self) -> tuple[str, ...]:
        return self._host_cfg['format_sort']

    def build_ytdl_opts(self, media_type: DownMediaType, curr_tmp_dir: Path) -> dict:
        """Build yt-dlp options dict for the given media type."""
        opts = list(DEFAULT_YTDL_OPTS)

        match media_type:
            case DownMediaType.AUDIO:
                opts.extend(AUDIO_YTDL_OPTS)
                opts.extend(AUDIO_FORMAT_YTDL_OPTS)
            case DownMediaType.VIDEO:
                opts.extend(VIDEO_YTDL_OPTS)
                opts.extend(self.format_sort_opt)
            case DownMediaType.AUDIO_VIDEO:
                opts.extend(AUDIO_YTDL_OPTS)
                opts.extend(VIDEO_YTDL_OPTS)
                opts.extend(self.format_sort_opt)
                opts.append('--keep-video')

        opts_dict = cli_to_api(opts)
        # Prepend temp dir to output template
        if 'outtmpl' in opts_dict and 'default' in opts_dict['outtmpl']:
            opts_dict['outtmpl']['default'] = str(
                curr_tmp_dir / opts_dict['outtmpl']['default']
            )
        return opts_dict
