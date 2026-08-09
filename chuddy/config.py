"""Configuration loading: YAML for bot settings, env vars for runtime paths."""

import logging
import os
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, Field, PositiveInt

from chuddy.enums import DownMediaType

logger = logging.getLogger(__name__)

# ── YAML config schemas ──────────────────────────────────────────────

class VideoCaptionConf(BaseModel):
    include_title: bool = True
    include_filename: bool = False
    include_link: bool = True
    include_size: bool = True


class UploadConf(BaseModel):
    upload_video_file: bool = True
    upload_video_max_file_size: PositiveInt = 2_147_483_648
    forward_to_group: bool = False
    forward_group_id: int | None = None
    silent: bool = True
    video_caption: VideoCaptionConf = Field(default_factory=VideoCaptionConf)


class UserConf(BaseModel):
    id: int
    is_admin: bool = False
    send_startup_message: bool = False
    download_media_type: DownMediaType = DownMediaType.VIDEO
    save_to_storage: bool = False
    use_url_regex_match: bool = True
    upload: UploadConf = Field(default_factory=UploadConf)
    save_to_database: bool = True


class ApiConf(BaseModel):
    upload_video_file: bool = False
    upload_video_max_file_size: PositiveInt = 2_147_483_648
    upload_to_chat_ids: list[int] = Field(default_factory=list)
    silent: bool = False
    video_caption: VideoCaptionConf = Field(default_factory=VideoCaptionConf)


class TelegramConf(BaseModel):
    api_id: int
    api_hash: str
    token: str
    lang_code: str = 'en'
    max_upload_tasks: PositiveInt = 5
    url_validation_regexes: list[str] = Field(default_factory=lambda: [r'^https?://.+$'])
    allowed_users: list[UserConf] = Field(default_factory=list)
    api: ApiConf = Field(default_factory=ApiConf)


class YtdlpConf(BaseModel):
    version_check_enabled: bool = True
    version_check_interval: int = 86400
    notify_users_on_new_version: bool = True
    release_channel: str = 'NIGHTLY'


class BotConfig(BaseModel):
    telegram: TelegramConf
    ytdlp: YtdlpConf = Field(default_factory=YtdlpConf)


# ── Env-var settings (paths, limits) ─────────────────────────────────

class Settings:
    """Runtime settings from environment variables with sensible defaults."""

    # Telegram limits
    TG_MAX_MSG_SIZE: Final[int] = int(os.getenv('TG_MAX_MSG_SIZE', '4096'))
    TG_MAX_CAPTION_SIZE: Final[int] = int(os.getenv('TG_MAX_CAPTION_SIZE', '1024'))

    # Download
    MAX_DOWNLOAD_THREADS: Final[int] = int(os.getenv('MAX_DOWNLOAD_THREADS', '4'))
    DOWNLOAD_TIMEOUT_SECONDS: Final[int] = int(os.getenv('DOWNLOAD_TIMEOUT_SECONDS', '300'))

    # Post-processing
    THUMBNAIL_FRAME_SECOND: Final[float] = float(os.getenv('THUMBNAIL_FRAME_SECOND', '1.0'))
    INSTAGRAM_ENCODE_TO_H264: Final[bool] = os.getenv('INSTAGRAM_ENCODE_TO_H264', 'true').lower() == 'true'
    FACEBOOK_ENCODE_TO_H264: Final[bool] = os.getenv('FACEBOOK_ENCODE_TO_H264', 'true').lower() == 'true'

    # Paths
    TMP_DOWNLOAD_ROOT_PATH: Final[Path] = Path(os.getenv('TMP_DOWNLOAD_ROOT_PATH', '/tmp'))
    STORAGE_PATH: Final[Path] = Path(os.getenv('STORAGE_PATH', '/filestorage'))
    DATA_DIR: Final[Path] = Path(os.getenv('DATA_DIR', '/data'))

    # Logging
    LOG_LEVEL: Final[str] = os.getenv('LOG_LEVEL', 'INFO')

    # STFU
    STFU_STICKER_ID: str | None = os.getenv('STFU_STICKER_ID')

    # yt-dlp cookies
    COOKIES_FILE: Path | None = None

    def __post_init__(self) -> None:
        pass  # Not using dataclass


settings = Settings()


def resolve_cookies() -> Path | None:
    """Find yt-dlp cookies file."""
    candidates = [
        Path('/app/cookies/_cookies.txt'),
        Path('./cookies/_cookies.txt'),
        Path('./cookies/cookies.txt'),
        Path('/app/cookies/cookies.txt'),
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            settings.COOKIES_FILE = p
            return p
    return None


# ── Config loader ────────────────────────────────────────────────────

_DEFAULT_USER = UserConf(
    id=0,
    is_admin=False,
    send_startup_message=False,
    download_media_type=DownMediaType.VIDEO,
    save_to_storage=False,
    use_url_regex_match=True,
    upload=UploadConf(
        upload_video_file=True,
        upload_video_max_file_size=2_147_483_648,
        forward_to_group=False,
        forward_group_id=None,
        silent=True,
        video_caption=VideoCaptionConf(
            include_title=True, include_filename=False, include_link=True, include_size=True,
        ),
    ),
    save_to_database=True,
)


def load_config(config_path: str | Path | None = None) -> BotConfig:
    """Load bot config from YAML, merging user_ids.txt if present."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / 'config.yml'
    config_path = Path(config_path)

    if not config_path.is_file():
        logger.error('Config file not found: %s', config_path)
        raise FileNotFoundError(f'Config file not found: {config_path}')

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # Merge chuddy/user_ids.txt — one Telegram user ID per line. Users listed
    # here get direct DM access to all of the bot's features. This path is used
    # because the Dockerfile copies the chuddy/ package into the image.
    user_ids_file = Path(__file__).parent / 'user_ids.txt'
    if user_ids_file.is_file():
        raw = _merge_user_ids(raw, user_ids_file)

    config = BotConfig(**raw)
    logger.info('Loaded config: %d user(s)/chat(s)', len(config.telegram.allowed_users))
    return config


def _merge_user_ids(raw: dict, user_ids_file: Path) -> dict:
    existing_ids = {u.get('id') for u in raw.get('telegram', {}).get('allowed_users', [])}
    new_users = []
    with open(user_ids_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    uid = int(line)
                    if uid not in existing_ids:
                        new_users.append({'id': uid})
                except ValueError:
                    pass
    if new_users:
        raw.setdefault('telegram', {}).setdefault('allowed_users', []).extend(new_users)
        logger.info('Added %d user(s) from %s', len(new_users), user_ids_file)
    return raw


# ── Global config instance ───────────────────────────────────────────

_config: BotConfig | None = None


def get_config(config_path: str | Path | None = None) -> BotConfig:
    global _config
    if _config is None:
        _config = load_config(config_path)
    return _config
