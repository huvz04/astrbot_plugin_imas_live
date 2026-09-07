"""The verified anonymous CMS directory adapter; no token is persisted or logged."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://cmsapi-frontend.idolmaster-official.jp/sitern/api/"
OFFICIAL = "https://idolmaster-official.jp"


class SourceUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class CmsArticle:
    cms_id: str
    title: str
    url: str | None
    brands: list[str]
    event_display: str | None
    venue: str | None
    updated: str | None
    raw: dict[str, Any]


class OfficialCmsClient:
    def __init__(self, timeout: float = 25, min_interval: float = 1.0):
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "ImasLiveAstrBot/0.1 (+local official-source reader)"},
        )
        self.min_interval = min_interval
        self._next_request = 0.0
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any], retry_token: bool = True) -> dict[str, Any]:
        await self._wait_slot()
        for attempt in range(3):
            try:
                response = await self.client.get(BASE + path, params=params)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt == 2:
                        raise SourceUnavailable(f"官网临时不可用（HTTP {response.status_code}）")
                    await asyncio.sleep(min(8, 2 ** attempt))
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("statusCode") != 200:
                    if retry_token and self._token and attempt == 0:
                        self._token = None
                        params = {**params, "token": await self.token()}
                        continue
                    raise SourceUnavailable("官网 CMS 返回业务错误；已保留上次成功数据。")
                return payload
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SourceUnavailable("官网网络请求失败；已保留上次成功数据。") from exc
                await asyncio.sleep(min(8, 2 ** attempt))
        raise SourceUnavailable("官网请求失败")

    async def _wait_slot(self) -> None:
        async with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay + random.uniform(0, 0.15))
            self._next_request = time.monotonic() + self.min_interval

    async def fetch_html(self, url: str) -> str:
        """Fetch a direct official page using the same serial rate limiter and retries."""
        await self._wait_slot()
        for attempt in range(3):
            try:
                response = await self.client.get(url)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt == 2:
                        raise SourceUnavailable(f"官网临时不可用（HTTP {response.status_code}）")
                    await asyncio.sleep(min(8, 2 ** attempt))
                    continue
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SourceUnavailable("官网网络请求失败；已保留上次成功数据。") from exc
                await asyncio.sleep(min(8, 2 ** attempt))
        raise SourceUnavailable("官网请求失败")

    async def token(self) -> str:
        if self._token:
            return self._token
        payload = await self._get("cmsbase/Token/get", {}, retry_token=False)
        token = payload.get("data", {}).get("token")
        if not isinstance(token, str) or not token:
            raise SourceUnavailable("官网未返回匿名令牌。")
        self._token = token
        return token

    async def live_articles(self, max_pages: int = 30, page_size: int = 12) -> list[CmsArticle]:
        token = await self.token()
        articles: list[CmsArticle] = []
        total: int | None = None
        for start in range(0, max_pages * page_size, page_size):
            data = json.dumps({"category": ["LIVE-EVENT"], "article_type": ["url_link", "detail_page"]}, ensure_ascii=False)
            payload = await self._get("idolmaster/Article/list", {
                "site": "jp", "ip": "idolmaster", "token": token, "start": start,
                "limit": page_size, "data": data,
            })
            body = payload.get("data", {})
            total = int(body.get("total_count", 0)) if total is None else total
            batch = body.get("article_list", [])
            if not isinstance(batch, list) or not batch:
                break
            articles.extend(self._article(item) for item in batch if isinstance(item, dict))
            if start + len(batch) >= total or len(batch) < page_size:
                break
        return articles

    @staticmethod
    def _article(item: dict[str, Any]) -> CmsArticle:
        brands = [str(x.get("code")) for x in item.get("brand", []) if isinstance(x, dict) and x.get("code")]
        url = item.get("event_url") if item.get("article_type") == "url_link" else None
        # Routes are confirmed from the site's front-end; a CMS content-detail endpoint is not assumed.
        if not url and item.get("article_type") == "detail_page" and item.get("url_name"):
            url = f"{OFFICIAL}/live_events/{item['url_name']}"
        if not url and item.get("article_type") == "lp_detail" and item.get("path"):
            url = f"{OFFICIAL}/lp/{item['path']}"
        return CmsArticle(
            str(item.get("_id", "")), str(item.get("title", "")), str(url) if url else None,
            brands, item.get("event_dspdate"), item.get("event_place"), item.get("updated"), item,
        )
