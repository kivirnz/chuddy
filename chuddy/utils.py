"""Shared utility functions."""

import logging
import os
import re
import secrets
import string
from pathlib import Path
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ── Formatting ───────────────────────────────────────────────────────


def bold(text: str) -> str:
    return f'<b>{text}</b>'


def code(text: str) -> str:
    return f'<code>{text}</code>'


def format_bytes(num: float) -> str:
    """Format bytes into human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num) < 1024.0:
            return f'{num:.1f} {unit}'
        num /= 1024.0
    return f'{num:.1f} PB'


# ── File operations ──────────────────────────────────────────────────


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def remove_dir(path: Path) -> None:
    """Recursively remove a directory, ignoring errors."""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        logger.exception('Failed to remove %s', path)


def gen_random_str(length: int = 8) -> str:
    return ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def list_files_human(path: Path) -> list[str]:
    """List files in a directory with human-readable sizes."""
    if not path.is_dir():
        return []
    result = []
    for f in sorted(path.iterdir()):
        if f.is_file():
            result.append(f'{f.name} ({format_bytes(file_size(f))})')
    return result


# ── URL helpers ──────────────────────────────────────────────────────

REMOVE_QUERY_PARAMS_HOSTS = {
    'twitter.com', 'www.twitter.com', 'x.com', 'www.x.com', 't.co', 'www.t.co',
    'instagram.com', 'www.instagram.com',
    'facebook.com', 'www.facebook.com',
}


def can_remove_url_params(url: str, matching_hosts: set[str] | None = None) -> bool:
    hosts = matching_hosts or REMOVE_QUERY_PARAMS_HOSTS
    parsed = urlparse(url)
    return parsed.netloc in hosts


def preprocess_url(url: str) -> str:
    """Strip query params from known hosts."""
    if can_remove_url_params(url):
        return urljoin(url, urlparse(url).path)
    return url


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from text."""
    return re.findall(r'https?://[^\s]+', text)


def filter_urls(urls: list[str], regexes: list[str]) -> list[str]:
    """Filter URLs against a list of regex patterns."""
    valid = []
    for url in urls:
        for regex in regexes:
            if re.match(regex, url):
                valid.append(url)
                break
    return list(dict.fromkeys(valid))


# ── yt-dlp helpers ───────────────────────────────────────────────────


def cli_to_api(opts: list[str]) -> dict:
    """Convert yt-dlp CLI options tuple to internal API dict."""
    import yt_dlp
    default = yt_dlp.parse_options([]).ydl_opts
    diff = {k: v for k, v in yt_dlp.parse_options(opts).ydl_opts.items() if default[k] != v}
    if 'postprocessors' in diff:
        diff['postprocessors'] = [
            pp for pp in diff['postprocessors'] if pp not in default['postprocessors']
        ]
    return diff


def get_cookies_file() -> Path | None:
    """Find non-empty cookies file."""
    candidates = [
        Path('/app/cookies/_cookies.txt'),
        Path('./cookies/_cookies.txt'),
        Path('./cookies/cookies.txt'),
        Path('/app/cookies/cookies.txt'),
    ]
    for p in candidates:
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None
