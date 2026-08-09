"""Streaming media pipeline.

Instead of downloading the entire video to disk with yt-dlp and then uploading
it to Telegram, we:

1. Use yt-dlp (extract-only, no download) to resolve a single muxed/progressive
   direct media URL plus metadata (title, duration, width/height, thumbnail).
2. Stream that URL straight into Pyrogram's upload via a seekable file-like
   object backed by a background download thread. Download and upload now
   overlap, so the bot responds with the video almost instantly.

Audio (``.a``) and sources that cannot be streamed as a single file
(HLS/DASH manifests, unknown size) keep using the legacy download pipeline.
"""

import io
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yt_dlp

from chuddy.utils import get_cookies_file

logger = logging.getLogger(__name__)

# Format selector: prefer a single muxed mp4 served over plain HTTP(S).
# Avoid HLS (m3u8*) and MPEG-DASH (http_dash_segments) which are fragmented.
STREAM_FORMAT_SELECTOR = (
    'best[ext=mp4][protocol^=http][protocol!=http_dash_segments][protocol!=m3u8_native]'
    '/best[protocol^=http][protocol!=http_dash_segments][protocol!=m3u8_native]'
    '/best[ext=mp4]/best'
)

_FRAGMENTED_PROTOCOLS = {'m3u8', 'm3u8_native', 'http_dash_segments'}

# Bound the in-memory prefetch so big videos don't blow up RAM, while keeping
# enough buffered that Pyrogram's synchronous reads almost never stall.
_MAX_BUFFER_BYTES = 64 * 1024 * 1024
_FETCH_CHUNK = 512 * 1024

# Telegram thumbnail limits
_THUMB_MAX_DIM = 320
_THUMB_MAX_BYTES = 200 * 1024


@dataclass
class StreamInfo:
    """Resolved metadata + direct URL for a streamable video."""

    url: str
    title: str
    ext: str
    duration: float | None
    width: int | None
    height: int | None
    thumb_url: str | None
    filesize: int | None
    http_headers: dict[str, str]
    streamable: bool


def _resolve_entry(info: dict[str, Any]) -> dict[str, Any]:
    if info.get('_type') == 'playlist':
        entries = info.get('entries') or []
        if entries:
            return entries[0] or {}
        return {}
    return info


def _pick_format(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the best single muxed HTTP format from the resolved entry.

    Guarantees the chosen format has both video and audio (so we don't ship a
    silent video-only DASH stream) and a plain http(s) protocol.
    """
    candidates: list[dict[str, Any]] = list(entry.get('formats') or [])
    if entry.get('url'):
        candidates.append(entry)

    best: dict[str, Any] | None = None

    def score(f: dict[str, Any]) -> tuple[Any, ...]:
        proto = (f.get('protocol') or '').lower()
        ext = (f.get('ext') or '').lower()
        vcodec = (f.get('vcodec') or '').lower()
        acodec = (f.get('acodec') or '').lower()
        has_video = bool(vcodec) and vcodec != 'none'
        has_audio = bool(acodec) and acodec != 'none'
        muxed = has_video and has_audio
        plain_http = proto.startswith('http') and proto not in _FRAGMENTED_PROTOCOLS
        height = f.get('height') or 0
        return (
            plain_http,
            muxed,
            ext == 'mp4',
            -1 if f.get('filesize') else 0,
            height,
        )

    for f in candidates:
        if not f.get('url'):
            continue
        proto = (f.get('protocol') or '').lower()
        if not proto.startswith('http') or proto in _FRAGMENTED_PROTOCOLS:
            continue
        vcodec = (f.get('vcodec') or '').lower()
        if not vcodec or vcodec == 'none':
            continue
        if best is None or score(f) > score(best):
            best = f

    return best


def extract_stream(url: str) -> StreamInfo | None:
    """Resolve a streamable direct URL + metadata via yt-dlp (no download)."""
    opts: dict[str, Any] = {
        'format': STREAM_FORMAT_SELECTOR,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'allow_playlist_files': False,
        'noprogress': True,
    }
    cookies = get_cookies_file()
    if cookies:
        opts['cookiefile'] = str(cookies)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        logger.exception('yt-dlp extract failed for %s', url)
        return None

    if not info:
        return None

    entry = _resolve_entry(info)
    if not entry:
        return None

    fmt = _pick_format(entry)

    title = entry.get('title') or 'video'
    thumb_url = entry.get('thumbnail')
    http_headers = dict(entry.get('http_headers') or {})

    if not fmt:
        return StreamInfo(
            url='',
            title=title,
            ext=(entry.get('ext') or 'mp4'),
            duration=_to_float(entry.get('duration')),
            width=entry.get('width'),
            height=entry.get('height'),
            thumb_url=thumb_url,
            filesize=_to_int(entry.get('filesize') or entry.get('filesize_approx')),
            http_headers=http_headers,
            streamable=False,
        )

    filesize = _to_int(fmt.get('filesize') or fmt.get('filesize_approx'))
    return StreamInfo(
        url=fmt['url'],
        title=title,
        ext=(fmt.get('ext') or 'mp4'),
        duration=_to_float(fmt.get('duration') or entry.get('duration')),
        width=fmt.get('width') or entry.get('width'),
        height=fmt.get('height') or entry.get('height'),
        thumb_url=thumb_url,
        filesize=filesize,
        http_headers=http_headers,
        streamable=True,
    )


def resolve_size(url: str, headers: dict[str, str] | None = None) -> int | None:
    """Determine the exact byte size of a remote media URL.

    Tries a HEAD first, then a single-byte Range request which exposes the
    total size via the ``Content-Range`` header on CDNs that don't answer HEAD.
    """
    hdrs = dict(headers or {})
    try:
        with requests.head(url, headers=hdrs, allow_redirects=True, timeout=15) as r:
            cl = r.headers.get('Content-Length')
            if r.ok and cl:
                return _to_int(cl)
    except Exception:
        logger.debug('HEAD size probe failed for %s', url, exc_info=True)

    range_hdrs = dict(hdrs)
    range_hdrs['Range'] = 'bytes=0-0'
    try:
        with requests.get(url, headers=range_hdrs, stream=True, allow_redirects=True, timeout=15) as r:
            if r.status_code in (206, 200):
                cr = r.headers.get('Content-Range')
                if cr and '/' in cr:
                    total = cr.rsplit('/', 1)[-1]
                    return _to_int(total)
                cl = r.headers.get('Content-Length')
                if cl:
                    return _to_int(cl)
    except Exception:
        logger.debug('Range size probe failed for %s', url, exc_info=True)

    return None


def prepare_thumbnail(thumb_url: str | None) -> Path | None:
    """Download a thumbnail URL and normalize it to Telegram's requirements.

    Returns a path to a JPEG <= 200KB and <= 320px, or None on failure.
    """
    if not thumb_url:
        return None

    try:
        resp = requests.get(thumb_url, timeout=15)
        resp.raise_for_status()
        raw = resp.content
    except Exception:
        logger.debug('Thumbnail download failed for %s', thumb_url, exc_info=True)
        return None

    if not raw:
        return None

    try:
        from PIL import Image

        src_fd, src_path = tempfile.mkstemp(suffix='.jpg')
        dst_fd, dst_path = tempfile.mkstemp(suffix='.jpg')
        os.close(src_fd)
        os.close(dst_fd)
        with open(src_path, 'wb') as f:
            f.write(raw)

        with Image.open(src_path) as img:
            img = img.convert('RGB')
            img.thumbnail((_THUMB_MAX_DIM, _THUMB_MAX_DIM))
            for quality in (90, 80, 70, 60, 50):
                img.save(dst_path, 'JPEG', quality=quality)
                if os.path.getsize(dst_path) <= _THUMB_MAX_BYTES:
                    break

        try:
            os.remove(src_path)
        except OSError:
            pass
        return Path(dst_path)
    except Exception:
        logger.debug('Thumbnail normalization failed', exc_info=True)
        return None


def safe_filename(title: str, ext: str) -> str:
    """Turn a video title into a filesystem/Telegram-friendly filename."""
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', ' ', title).strip()
    name = re.sub(r'\s+', ' ', name)
    if not name:
        name = 'video'
    name = name[:120]
    ext = (ext or 'mp4').lower().strip('.')
    return f'{name}.{ext}'


class StreamingFile(io.IOBase):
    """Seekable file-like object that streams a remote URL on demand.

    Pyrogram's upload routine probes the size via ``seek(SEEK_END)``/``tell()``
    and then reads the body sequentially in 512KiB parts. We satisfy that
    interface with a logical position over a known total size, while a daemon
    thread pulls bytes from the source into a bounded in-memory buffer.

    Rewinding (used by Pyrogram on rare ``FilePartMissing`` retries for big
    files) is supported by re-opening the source with an HTTP ``Range`` header.
    """

    def __init__(
        self,
        url: str,
        size: int,
        headers: dict[str, str] | None = None,
        name: str = 'video.mp4',
    ):
        self._url = url
        self._size = int(size)
        self._headers = dict(headers or {})
        self.name = name

        self._buf = bytearray()
        self._buf_start = 0  # file offset of buf[0]
        self._pos = 0  # logical read cursor
        self._fetched = 0  # high-water mark of bytes fetched from source

        self._eof = False
        self._err: BaseException | None = None
        self._closed = False

        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None

    @property
    def fetched(self) -> int:
        return self._fetched

    # ── io.IOBase interface ───────────────────────────────────────────

    def readable(self) -> bool:
        return not self._closed

    def seekable(self) -> bool:
        return not self._closed

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_END:
            self._pos = self._size + offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = offset
        if self._pos < 0:
            self._pos = 0
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b''

        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        if size == 0:
            return b''

        with self._cond:
            if self._pos < self._buf_start and self._thread is not None:
                self._rewind_locked(self._pos)

        self._ensure_started()

        out = bytearray()
        remaining = size
        while remaining > 0:
            with self._cond:
                while not self._buf and not self._eof and self._err is None:
                    self._cond.wait()
                if self._err is not None:
                    raise self._err
                if not self._buf:
                    break
                chunk = bytes(self._buf[:remaining])
                del self._buf[: len(chunk)]
                self._buf_start += len(chunk)
                self._pos += len(chunk)
                remaining -= len(chunk)
            out.extend(chunk)

        return bytes(out)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._buf.clear()
            self._cond.notify_all()
        super().close()

    # ── internals ─────────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        with self._cond:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._run, args=(self._buf_start,), daemon=True, name='chuddy-stream'
            )
            self._thread.start()

    def _rewind_locked(self, new_start: int) -> None:
        """Restart the source from new_start using a Range request.

        Caller holds self._cond.
        """
        self._buf.clear()
        self._buf_start = new_start
        self._fetched = new_start
        self._eof = False
        self._err = None
        self._thread = threading.Thread(
            target=self._run, args=(new_start,), daemon=True, name='chuddy-stream-rw'
        )
        self._thread.start()

    def _run(self, start_offset: int) -> None:
        """Background download loop filling the buffer."""
        try:
            headers = dict(self._headers)
            range_start = start_offset
            if range_start > 0:
                headers['Range'] = f'bytes={range_start}-'
            with requests.get(
                self._url, headers=headers, stream=True, timeout=(15, 60), allow_redirects=True
            ) as r:
                r.raise_for_status()
                if range_start > 0:
                    content_range = r.headers.get('Content-Range', '')
                    if not content_range.startswith(f'bytes {range_start}-'):
                        with self._cond:
                            if self._thread is threading.current_thread():
                                self._err = OSError('Source does not support HTTP Range requests')
                                self._cond.notify_all()
                        return

                for chunk in r.iter_content(chunk_size=_FETCH_CHUNK):
                    if self._closed:
                        return
                    if not chunk:
                        continue
                    with self._cond:
                        if self._thread is not threading.current_thread():
                            return
                        while len(self._buf) >= _MAX_BUFFER_BYTES and not self._closed:
                            self._cond.wait()
                            if self._thread is not threading.current_thread():
                                return
                        self._buf.extend(chunk)
                        end = self._buf_start + len(self._buf)
                        if end > self._fetched:
                            self._fetched = end
                        self._cond.notify_all()
        except Exception as e:
            with self._cond:
                if self._thread is threading.current_thread():
                    self._err = e
                self._cond.notify_all()
        finally:
            with self._cond:
                if self._thread is threading.current_thread():
                    self._eof = True
                self._cond.notify_all()


def _to_float(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
