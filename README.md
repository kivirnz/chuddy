# Chuddy - Media Download Telegram Bot

Self-hosted Telegram bot for downloading audio and video from [yt-dlp](https://github.com/yt-dlp/yt-dlp)-supported sites, with OCR and translation features built in.

**Single container, zero bloat.** No Postgres, no RabbitMQ, no Redis. Just SQLite and direct async calls.

## Features

- Download audio and video from 1000+ sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- Upload downloaded media directly back to Telegram
- Use in private chats or groups
- OCR + translate text from images (powered by Yandex)
- Text translation (powered by Google Translate)
- Cookie-based authentication for protected sites
- Automatic H264 re-encoding for Instagram/Facebook videos (VP9 → iOS compatible)
- Video compression for oversized files
- STFU mode — minimal output with reactions only
- File caching (Telegram file_id dedup via SQLite)
- Punk mode with custom chat interactions

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Telegram API credentials — [get them here](https://my.telegram.org/apps)
- Your Telegram user ID — [find it here](https://stackoverflow.com/questions/32683992/find-out-my-own-user-id-for-sending-a-message-with-telegram-api)

## Quick Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/chuddy.git
cd chuddy
```

### 2. Configure the bot

```bash
cp config-example.yml config.yml
```

Edit `config.yml` and fill in:

| Field | Description |
|-------|-------------|
| `api_id` | Your Telegram API ID |
| `api_hash` | Your Telegram API hash |
| `token` | Your bot token from BotFather |
| `allowed_users` → `id` | Your Telegram user or group ID |

### 3. Build and run

```bash
./start.sh
```

Or manually:

```bash
docker compose up --build -d
docker compose logs -f
```

Your bot will send a "Chuddy online" message once ready.

## Bot Commands

| Command | Description |
|---------|-------------|
| `.v <url>` | Download as video (playable in Telegram) |
| `.a <url>` | Download as audio (playable in Telegram) |
| `.tr <text>` | Translate text to English (Google Translate) |
| `.tr` (reply to message) | Translate replied text to English |
| `.ocr` (on/reply to image) | OCR + translate image text to English |
| `.help` | Show help menu |
| `.punk` | Toggle punk mode (admin only, groups) |
| `stfu chuddy` | Toggle minimal output mode (reactions only) |
| `/add <chat_id>` | Add chat to allow list (admin) |
| `/remove <chat_id>` | Remove chat from allow list (admin) |

## Configuration

### `config.yml`

The main configuration file. Key sections:

**Per-user settings** (`allowed_users`):
- `download_media_type`: `VIDEO`, `AUDIO`, or `AUDIO_VIDEO`
- `save_to_storage`: Save files to disk as well as uploading to Telegram
- `upload_video_file`: Upload the downloaded file back to Telegram chat
- `upload_video_max_file_size`: Max upload size in bytes (default 2GB)
- `forward_to_group` / `forward_group_id`: Forward uploads to a group/channel
- `video_caption`: Control what appears in the upload caption

**yt-dlp settings** (`ytdlp`):
- `version_check_enabled`: Check for new yt-dlp versions
- `release_channel`: `STABLE`, `NIGHTLY`, or `MASTER`

### `.env`

Runtime settings (all optional with defaults):

| Variable | Description | Default |
|----------|-------------|---------|
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `MAX_DOWNLOAD_THREADS` | Concurrent fragments per download | `4` |
| `DOWNLOAD_TIMEOUT_SECONDS` | Download timeout | `300` |
| `THUMBNAIL_FRAME_SECOND` | Frame position for thumbnail extraction | `1.0` |
| `INSTAGRAM_ENCODE_TO_H264` | Re-encode Instagram VP9 to H264 | `true` |
| `FACEBOOK_ENCODE_TO_H264` | Re-encode Facebook VP9 to H264 | `true` |
| `TG_MAX_MSG_SIZE` | Telegram max message size | `4096` |
| `TG_MAX_CAPTION_SIZE` | Telegram max caption size | `1024` |

### Cookies for authenticated sites

Place your Netscape-format cookies in:

```
cookies/cookies.txt
```

### Custom yt-dlp options

Edit `chuddy/ytdl_opts.py` to change default download options or add per-host configurations.

### Adding extra users

Edit `user_ids.txt` — one user ID per line. Lines starting with `#` are ignored. Users from this file get default configuration.

## Punk Mode

When enabled in a group chat with `.punk` (admin only), the bot responds to casual messages:

| Trigger | Response |
|---------|----------|
| `good bot` | Compliment reply |
| `bad bot` | Insult reply |
| `I'm <word>` | "Hi \<word\>, I'm Chuddy" |
| `milk` | Random milk image |
| `furry` / `furries` | Random line from `tfd.txt` |
| `prolly` | Random percentage |
| `laptop` | "Lenovo Thinkpad X62" |
| Slash commands (`/arsen_*`, etc.) | Tracks first occurrence per month/day |
| PDF attachment | Scans for embedded JavaScript |

## STFU Mode

Toggle with `stfu chuddy`. In this mode:
- Hot dog (🌭) reaction on the request message instead of "✅ URL sent for download"
- No progress messages during upload
- Video/audio uploads silently with URL-only caption
- Hot dog (🌭) reaction on completion instead of success text
- Error sticker instead of error text

## Architecture (v2)

Chuddy v2 is a **single Python process** in a single container:

```
User → Telegram → Pyrogram Bot → yt-dlp → Upload
                         ↓
                     SQLite (aiosqlite)
```

| Before (v1) | After (v2) |
|---|---|
| 6 containers | **1 container** |
| Postgres (asyncpg + SQLAlchemy + Alembic) | **SQLite** (aiosqlite) |
| RabbitMQ (4 queues, 4 exchanges) | **Direct function calls** |
| Redis cache | **Removed** |
| FastAPI REST service | **Removed** |
| Separate worker process | **Merged into bot** |
| ~80+ files across 4 packages | **14 Python files** |

## Disclaimer

This tool is intended for use only with content that you have the right to download, such as Creative Commons licensed media.

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
