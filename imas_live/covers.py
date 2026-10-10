"""Explicit official covers only; bounded downloads and verified local files."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image

MAX_COVER_BYTES = 4 * 1024 * 1024
FORWARD_BUDGET = 12 * 1024 * 1024
IMAGE_API = 'https://cmsapi-frontend.idolmaster-official.jp/sitern/api/idolmaster/Image/get'


def official_image_url(value: str) -> bool:
    parts = urlsplit(value)
    if parts.scheme != 'https' or parts.username or parts.password:
        return False
    if parts.netloc == 'cmsapi-frontend.idolmaster-official.jp':
        return parts.path == '/sitern/api/idolmaster/Image/get' and any(
            p.startswith('/idolmaster/jp/') for p in parse_qs(parts.query).get('path', []))
    return parts.netloc == 'idolmaster-official.jp'


def _not_generic(value: str) -> bool:
    path = urlsplit(value).path.casefold()
    if urlsplit(value).netloc == 'cmsapi-frontend.idolmaster-official.jp':
        path = ' '.join(parse_qs(urlsplit(value).query).get('path', [])).casefold()
    return not re.search(r'logo|sponsor|background|(?:^|[/_.-])(?:bg|btn|icon|button|common|default)(?:[/_.-]|$)', path)


def cms_thumbnail_url(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if not parts.scheme and not parts.netloc and parts.path.startswith('/idolmaster/jp/'):
        # CMS uses Image/get?path=, NOT the public site's root. The trailing
        # cache-buster belongs outside path, as on the official frontend.
        query = {'path': parts.path}
        value = IMAGE_API + '?' + urlencode(query)
    return value if official_image_url(value) and _not_generic(value) else None


def og_cover_url(html: str, page_url: str) -> str | None:
    from .cms import event_root
    root = event_root(page_url)
    if not root:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    meta = soup.find('meta', attrs={'property': 'og:image'})
    if not meta or not isinstance(meta.get('content'), str):
        return None
    base_url = root if urlsplit(page_url).path.rstrip('/') == urlsplit(root).path.rstrip('/') else page_url
    value = urljoin(base_url, meta['content'].strip())
    if not official_image_url(value) or not _not_generic(value):
        return None
    # A site's shared/default OG logo is not evidence for this event. Static
    # files must belong to the actual event root; CMS og:image uses Image/get.
    if urlsplit(value).netloc == 'idolmaster-official.jp' and not value.startswith(root):
        return None
    return value


def validate_cover(content: bytes) -> tuple[str, int, int]:
    if not 64 < len(content) <= MAX_COVER_BYTES:
        raise ValueError('cover byte limit')
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
        suffix = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}.get(image.format)
        if not suffix or getattr(image, 'n_frames', 1) != 1 or not (
                320 <= width <= 8192 and 180 <= height <= 8192 and width*height <= 20_000_000
                and .25 <= width/height <= 4):
            raise ValueError('not a supported event cover')
        image.verify()
    with Image.open(BytesIO(content)) as image:
        image.load()
    return suffix, width, height


def local_cover(row: dict | None, root: Path, budget: int = MAX_COVER_BYTES) -> tuple[str, int] | None:
    if not row or not row.get('cached_path') or not row.get('content_hash'):
        return None
    if row.get('source_kind') == 'cms_thumbnail' and row.get('thumbnail_url') != row.get('image_url'):
        return None  # a changed city/article cover must not send the old image
    try:
        path = Path(row['cached_path']).resolve()
        path.relative_to(root.resolve())
        size = path.stat().st_size
        if size > min(MAX_COVER_BYTES, budget):
            return None
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row['content_hash']:
            return None
        validate_cover(content)
        return str(path), size
    except Exception:
        return None  # missing/corrupt/cache-path escapes never break text details


def store_cover(root: Path, event_id: str, content: bytes) -> tuple[str, str]:
    suffix, _, _ = validate_cover(content)
    digest = hashlib.sha256(content).hexdigest()
    directory = root / hashlib.sha256(event_id.encode()).hexdigest()[:24]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + suffix)
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.cover-', delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return str(target), digest


async def download_cover(client, url: str, headers: dict | None = None) -> tuple[bytes | None, dict]:
    """Serial limiter + timeout + manual trusted redirects + streaming byte cap."""
    async def fetch():
        current = url
        for _ in range(4):
            if not official_image_url(current):
                raise ValueError('untrusted cover URL/redirect')
            await client._wait_slot()
            async with client.client.stream('GET', current, headers=headers or {},
                    timeout=15, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    current = urljoin(current, response.headers.get('location', ''))
                    continue
                if response.status_code == 304:
                    return None, dict(response.headers)
                response.raise_for_status()
                if response.headers.get('content-type', '').split(';')[0].lower() not in (
                        'image/jpeg', 'image/png', 'image/webp'):
                    raise ValueError('non-image cover response')
                length = response.headers.get('content-length')
                if length and int(length) > MAX_COVER_BYTES:
                    raise ValueError('cover content length limit')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_COVER_BYTES:
                        raise ValueError('cover streamed byte limit')
                await asyncio.to_thread(validate_cover, bytes(content))
                return bytes(content), dict(response.headers)
        raise ValueError('too many cover redirects')
    return await asyncio.wait_for(fetch(), timeout=20)
