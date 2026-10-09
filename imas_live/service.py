"""Async orchestration layer: fetch, parse, store, query and conservative reminders."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib
import json
import logging
import re
from io import BytesIO
from datetime import datetime, timedelta, timezone
from dataclasses import asdict as vars_for_slots
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from .cms import CmsArticle, OfficialCmsClient, SourceUnavailable, event_root
from .database import Database
from .parsing import parse_shiny_information, parse_day_cast, official_roster_image_urls, parse_ticket_page, parse_ticket_news, parse_venue, parse_information, schedule_performances, clean
from .models import ParsedPage


BRAND_COMMANDS = {
    "imas": "IDOLMASTER", "765": "IDOLMASTER", "as": "IDOLMASTER",
    "cg": "CINDERELLAGIRLS", "ml": "MILLIONLIVE", "sidem": "SIDEM",
    "sm": "SIDEM", "sc": "SHINYCOLORS", "gk": "GAKUEN",
}
REMINDER_WINDOW_MINUTES = 6  # five-minute worker plus modest scheduler jitter
logger = logging.getLogger(__name__)

# The CMS catalogue has omitted this verified official event.  Keep it as a
# controlled source, not a guessed URL scan.  It is deliberately not treated
# as a newly discovered event when added to an existing installation, so old
# ticket rounds do not trigger a historical group announcement.
VERIFIED_EVENT_SOURCES = (
    CmsArticle("IUOAFA", "765 PRODUCTION × 961 PRODUCTION IDOL ULTIMATE ONCE AND FOR ALL",
               "https://idolmaster-official.jp/live_event/IUOAFA/", ["IDOLMASTER"],
               "2027年7月24日(土)・25日(日)", "京王アリーナ TOKYO", "2026-09-06", {"controlled_source": True}),
)


def _event_type_codes(value: Any) -> set[str]:
    """CMS event_type occurs as strings, arrays, and code-bearing objects."""
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, dict):
        return set().union(*(_event_type_codes(value.get(key)) for key in ("code", "value", "event_type", "type")))
    if isinstance(value, list):
        return set().union(*(_event_type_codes(item) for item in value))
    return set()


def _canonical_event_url(value: str | None) -> str:
    parts = urlsplit(value or "")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/').casefold()}" if parts.scheme == "https" else ""


class ImasLiveService:
    def __init__(self, data_dir: Path, config: dict[str, Any] | None = None):
        self.config = config if config is not None else {}
        self.data_dir = data_dir
        self.db = Database(data_dir / "imas_live.sqlite3")
        self.db.recover_deliveries()
        # The old whitelist described ticket reminders only.  It is intentionally
        # never copied to the newly introduced LIVE reminder table.
        self.db.migrate_legacy_ticket_subscriptions(self.config.get("white_umos", []))
        self.client = OfficialCmsClient(float(self.config.get("request_timeout_seconds", 25)))
        self._sync_lock = asyncio.Lock()
        self.directory_ready = asyncio.Event()
        self.directory_attempted = asyncio.Event()

    async def close(self) -> None:
        await self.client.close()

    def group_enabled(self, umo: str) -> bool:
        return self.db.group_enabled(umo, bool(self.config.get("enabled", True)))

    def set_group_enabled(self, umo: str, value: bool) -> None:
        self.db.set_group_enabled(umo, value)

    def subscription_enabled(self, kind: str, umo: str) -> bool:
        return self.db.subscription_enabled(kind, umo)

    def set_subscription(self, kind: str, umo: str, value: bool) -> None:
        self.db.set_subscription(kind, umo, value)

    async def sync(self, full_directory: bool = True) -> dict[str, Any]:
        """Fetch only verified CMS and direct public special pages. Never deletes old data."""
        if self._sync_lock.locked():
            return {"status": "already_running"}
        async with self._sync_lock:
            self.db.set_meta('last_sync_attempt', datetime.now(timezone.utc).isoformat(timespec='seconds'))
            directory_error = None
            try:
                if full_directory:
                    articles = await self.client.live_articles(int(self.config.get("max_pages", 30)))
                    controlled_by_url = {_canonical_event_url(source.url): source for source in VERIFIED_EVENT_SOURCES}
                    discovered = {_canonical_event_url(row['official_url']): row for row in self.db.list_events(limit=10000)
                                  if row['id'].startswith('news:')}
                    normalized = []
                    known_urls = set()
                    for article in articles:
                        url_key = _canonical_event_url(article.url)
                        controlled = controlled_by_url.get(url_key)
                        if controlled:
                            # Keep the established public number if the CMS later
                            # gives this controlled URL a different numeric ID.
                            article = CmsArticle(controlled.cms_id, article.title, controlled.url,
                                                 article.brands or controlled.brands, article.event_display,
                                                 article.venue or controlled.venue, article.updated,
                                                 {**article.raw, "controlled_source": True})
                        elif url_key in discovered:
                            article.cms_id = discovered[url_key]['id']
                        if controlled and url_key in known_urls:
                            continue
                        normalized.append(article)
                        if url_key:
                            known_urls.add(url_key)
                    normalized.extend(source for source in VERIFIED_EVENT_SOURCES
                                      if _canonical_event_url(source.url) not in known_urls)
                    articles = normalized
                else:
                    known = await asyncio.to_thread(self.db.fetchable_events, int(self.config.get("max_special_pages", 12)))
                    articles = [CmsArticle(row["id"], row["title"], row["official_url"], json.loads(row["brands_json"]), row["event_display"], row["venue"], row["source_updated"], {}) for row in known]
            except SourceUnavailable as exc:
                self.db.set_meta("last_error", str(exc))
                if full_directory:
                    self.directory_attempted.set()
                directory_error = str(exc)
                full_directory = False
                known = await asyncio.to_thread(self.db.fetchable_events, int(self.config.get('max_special_pages', 12)))
                articles = [CmsArticle(row['id'], row['title'], row['official_url'], json.loads(row['brands_json']),
                                       row['event_display'], row['venue'], row['source_updated'], {}) for row in known]
            directory_was_complete = self.db.meta("baseline_complete") is not None
            for article in articles:
                # Only a newly discovered entry in a later *complete* directory
                # can prove that the event itself is newly announced.  Partial
                # special-page coverage is deliberately not enough.
                title = article.title.lower()
                is_live = (any(word in title for word in ('live', 'st@ge', 'stage', 'ライブ', 'musical', 'concert', 'orchestra', '演奏会'))
                           or "mr_event" in _event_type_codes(article.raw.get("event_type"))
                           or bool(article.raw.get("controlled_source")))
                excluded = any(word in title for word in ('museum', 'ホテル', '脱出', '物販', '上映', '発売記念', 'popup'))
                await asyncio.to_thread(self.db.upsert_event, self._event_record(article),
                                        full_directory and directory_was_complete and is_live and not excluded
                                        and not article.raw.get("controlled_source"))
                if full_directory:
                    # These are date entries from the official event field, never range expansion.
                    dates = schedule_performances(article.event_display or '', article.url or f'https://idolmaster-official.jp/live_event#event-{article.cms_id}', article.venue, True) if is_live and not excluded else []
                    await asyncio.to_thread(self.db.save_directory_dates, article.cms_id, dates)
            if full_directory:
                self.db.set_meta('last_directory_sync', datetime.now(timezone.utc).isoformat(timespec='seconds'))
                self.directory_ready.set()
                self.directory_attempted.set()
                await self._discover_news_sources()
                known = await asyncio.to_thread(self.db.fetchable_events, max(1, int(self.config.get('max_special_pages', 12))))
                articles = [CmsArticle(row['id'], row['title'], row['official_url'], json.loads(row['brands_json']), row['event_display'], row['venue'], row['source_updated'], {}) for row in known]
            changed, failed = 0, 0
            for article in articles:
                if not self._fetchable_special_page(article.url):
                    continue
                try:
                    changed += int(await self._refresh_article(article))
                except SourceUnavailable as exc:
                    failed += 1
                    logger.warning("IM@S source unavailable: event=%s source=%s reason=%s", article.cms_id, article.url, exc)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, article.url, str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record source error: event=%s source=%s", article.cms_id, article.url)
                except Exception as exc:
                    # One malformed official page (including a database identity
                    # conflict) must not abort the remaining rotating sources.
                    # source_error persists the event and URL for WebUI/log review.
                    failed += 1
                    logger.exception("IM@S source refresh failed: event=%s source=%s", article.cms_id, article.url)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, article.url, str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record source error: event=%s source=%s", article.cms_id, article.url)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if len(articles) > failed:
                self.db.set_meta("last_successful_sync", stamp)
            with self.db._connect() as db:
                unverified = db.execute("SELECT COUNT(*) FROM sources WHERE quality='stale'").fetchone()[0]
            self.db.set_meta('last_error', directory_error or (f'{unverified} 个专题待核验' if unverified else ''))
            first = self.db.meta("baseline_complete") is None
            if full_directory:
                self.db.set_meta("baseline_complete", "1")
            return {"status": "failed" if directory_error else "ok", "error": directory_error,
                    "events": len(articles), "changed_pages": changed, "failed_pages": failed, "baseline": first,
                    "diagnostics": self.db.sync_diagnostics(int(self.config.get('freshness_hours', 12)))}

    async def _discover_news_sources(self) -> None:
        """Discover event roots only from actual links in a bounded official news feed."""
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        baseline = self._ticket_moment(self.db.meta('news_discovery_baseline'))
        try:
            news = await self.client.recent_news()
            existing = {_canonical_event_url(row['official_url']): row for row in self.db.list_events(limit=10000)}
            attempted = 0
            errors = []
            for item in news:
                title, page = str(item.get('title', '')), str(item.get('path') or '')
                if not page or not re.fullmatch(r'[A-Za-z0-9_-]+', page) or not any(
                        term in title.casefold() for term in ('live', 'ライブ', '公演', 'チケット', '先行', '抽選', 'concert')):
                    continue
                marker = 'news_seen:' + str(item.get('_id') or page)
                revision = str(item.get('updated') or '')
                ticket_marker = 'news_ticket_seen:' + str(item.get('_id') or page)
                if self.db.meta(marker) == revision and self.db.meta(ticket_marker) == revision:
                    continue
                if attempted >= 32:
                    break
                attempted += 1
                try:
                    html = await self.client.article_content(page)
                except SourceUnavailable as exc:
                    errors.append(f'{page}: {exc}')
                    continue  # Leave the revision unacknowledged for a later retry.
                soup = BeautifulSoup(html, 'html.parser')
                roots = {event_root(urljoin('https://idolmaster-official.jp/news/' + page + '.html', anchor['href']))
                         for anchor in soup.select('a[href]')}
                roots.discard(None)
                for anchor in soup.select('a[href]'):
                    root = event_root(urljoin('https://idolmaster-official.jp/news/' + page + '.html', anchor['href']))
                    if not root:
                        continue
                    key = _canonical_event_url(root)
                    row = existing.get(key)
                    if row is None:
                        brands = [str(b['code']) for b in item.get('brand', []) if isinstance(b, dict) and b.get('code')]
                        article = CmsArticle('news:' + hashlib.sha256(key.encode()).hexdigest()[:16], title,
                                             root, brands, None, None, item.get('updated'), {})
                        try:
                            news_updated = datetime.fromtimestamp(float(item.get('updated')), timezone.utc)
                        except (ValueError, TypeError, OSError, OverflowError):
                            news_updated = None
                        self.db.upsert_event(self._event_record(article), bool(
                            baseline and news_updated and news_updated > baseline))
                        row = {'id': article.cms_id, 'official_url': root}
                        existing[key] = row
                    self.db.set_meta('refresh_hint:' + row['id'], stamp)
                if len(roots) == 1:
                    root = next(iter(roots))
                    event = existing[_canonical_event_url(root)]
                    news_url = 'https://idolmaster-official.jp/news/' + page + '.html'
                    rounds = parse_ticket_news(html, news_url)
                    known = {t['id']: t for t in self.db.ticket_query_rows() if t['event_id'] == event['id']}
                    selected = [t for t in rounds if event['id'] + ':' + t.stable_key not in known or
                                not self._source_is_fresh(known[event['id'] + ':' + t.stable_key], datetime.now(timezone.utc)) or
                                known[event['id'] + ':' + t.stable_key].get('verified_source_url') == news_url]
                    if selected:
                        digest = hashlib.sha256(json.dumps([t.record() for t in selected], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                        try:
                            updated = datetime.fromtimestamp(float(item.get('updated')), timezone.utc)
                        except (ValueError, TypeError, OSError, OverflowError):
                            updated = None
                        self.db.save_parsed(event['id'], news_url, digest, 'official-news-v1', selected, [], [], [],
                                            ticket_only=True, announce_initial=bool(baseline and updated and updated > baseline))
                self.db.set_meta(ticket_marker, revision)
                self.db.set_meta(marker, revision)
            self.db.set_meta('last_news_discovery', stamp)
            self.db.set_meta('last_news_error', '; '.join(errors)[:1000])
            if baseline is None:
                self.db.set_meta('news_discovery_baseline', stamp)
        except SourceUnavailable as exc:
            self.db.set_meta('last_news_error', str(exc))
            logger.warning('IM@S news discovery unavailable: %s', exc)

    async def _refresh_article(self, article: CmsArticle) -> bool:
        """Refresh one already-known official event without broad directory work."""
        parsed = await self._collect_special(article)
        if not (parsed.ticket_rounds or parsed.performances):
            raise SourceUnavailable('专题未能解析，保留已知记录并暂停该来源提醒')
        digest = hashlib.sha256(json.dumps({
            'tickets': [row.record() for row in parsed.ticket_rounds],
            'performances': [vars_for_slots(row) for row in parsed.performances],
            'cast': [vars_for_slots(row) for row in parsed.cast],
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        changed = await asyncio.to_thread(self.db.save_parsed, article.cms_id, article.url, digest, "special-page-v3", parsed.ticket_rounds, parsed.performances, parsed.cast, parsed.review_notes)
        assets = await self._cache_roster_assets(article.cms_id, article.url, parsed.cast_asset_urls)
        if assets:
            await asyncio.to_thread(self.db.save_cast_assets, article.cms_id, article.url, assets)
        return changed

    async def refresh_open_ticket_sources(self, current: datetime | None = None) -> dict[str, int]:
        """Fair bounded recheck, including future LIVE events whose old round ended."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        ids = await self._ticket_recheck_ids(current, manual=True)
        if not ids or self._sync_lock.locked():
            return {"refreshed": 0, "failed": 0}
        raw = await asyncio.to_thread(self.db.events_by_ids, ids[:max(1, int(self.config.get("ticket_query_refresh_max", 4)))])
        articles = [CmsArticle(row["id"], row["title"], row["official_url"], json.loads(row["brands_json"]), row["event_display"], row["venue"], row["source_updated"], {}) for row in raw]
        refreshed = failed = 0
        async with self._sync_lock:
            for article in articles:
                if not self._fetchable_special_page(article.url):
                    continue
                try:
                    await self._refresh_article(article)
                    refreshed += 1
                except SourceUnavailable as exc:
                    failed += 1
                    logger.warning("IM@S on-demand source unavailable: event=%s source=%s reason=%s", article.cms_id, article.url, exc)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, str(article.url), str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record on-demand source error: event=%s", article.cms_id)
                except Exception as exc:
                    failed += 1
                    logger.exception("IM@S on-demand source refresh failed: event=%s source=%s", article.cms_id, article.url)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, str(article.url), str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record on-demand source error: event=%s", article.cms_id)
        return {"refreshed": refreshed, "failed": failed}

    async def refresh_due_ticket_sources(self, current: datetime | None = None) -> dict[str, int]:
        """Recheck stale official sources that can affect an upcoming alert."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        ids = await self._ticket_recheck_ids(current, manual=False)
        if not ids or self._sync_lock.locked():
            return {"refreshed": 0, "failed": 0}
        rows = await asyncio.to_thread(self.db.events_by_ids, ids)
        refreshed = failed = 0
        async with self._sync_lock:
            for row in rows:
                article = CmsArticle(row["id"], row["title"], row["official_url"], json.loads(row["brands_json"]), row["event_display"], row["venue"], row["source_updated"], {})
                if not self._fetchable_special_page(article.url):
                    continue
                try:
                    await self._refresh_article(article)
                    refreshed += 1
                except SourceUnavailable as exc:
                    failed += 1
                    logger.warning("IM@S due-ticket source unavailable: event=%s source=%s reason=%s", article.cms_id, article.url, exc)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, str(article.url), str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record due-ticket source error: event=%s", article.cms_id)
                except Exception as exc:
                    failed += 1
                    logger.exception("IM@S due-ticket recheck failed: event=%s source=%s", article.cms_id, article.url)
                    try:
                        await asyncio.to_thread(self.db.source_error, article.cms_id, str(article.url), str(exc))
                    except Exception:
                        logger.exception("IM@S failed to record due-ticket source error: event=%s", article.cms_id)
        if refreshed or failed:
            logger.info("IM@S due-ticket recheck: refreshed=%s failed=%s", refreshed, failed)
        return {"refreshed": refreshed, "failed": failed}

    async def _ticket_recheck_ids(self, current: datetime, manual: bool) -> list[str]:
        zone = ZoneInfo(str(self.config.get('display_timezone', 'Asia/Shanghai')))
        by_event: dict[str, list[dict[str, Any]]] = {}
        for row in await asyncio.to_thread(self.db.ticket_performances):
            by_event.setdefault(row['event_id'], []).append(row)
        open_ids, stale_ids = set(), set()
        for row in await asyncio.to_thread(self.db.ticket_query_rows):
            start, end = self._ticket_moment(row['application_start']), self._ticket_moment(row['application_end'])
            if start and end and start <= current < end:
                open_ids.add(row['event_id'])
                if not self._source_is_fresh(row, current):
                    stale_ids.add(row['event_id'])
        selected = []
        candidates = await asyncio.to_thread(self.db.source_candidates, current)
        for row in sorted(candidates, key=lambda r: (r['attempted_at'] or '', r['id'])):
            attempted = self._ticket_moment(row['attempted_at'])
            if attempted and timedelta(0) <= current - attempted < timedelta(minutes=5):
                continue
            event_performances = by_event.get(row['id'], [])
            if not event_performances and row.get('event_display'):
                event_performances = [vars_for_slots(p) for p in schedule_performances(row['event_display'], row['official_url'])]
            future_live = bool(event_performances) and self._ticket_event_window(event_performances, current, zone)[0]
            if not future_live and row['id'] not in open_ids:
                continue
            fetched = self._ticket_moment(row['fetched_at'])
            interval = timedelta(minutes=5 if manual and row['id'] not in open_ids else max(15, int(self.config.get('sync_interval_minutes', 60))))
            if row['source_quality'] != 'verified' or not fetched or current - fetched >= interval or row['id'] in stale_ids:
                selected.append(row['id'])
            if len(selected) >= max(1, int(self.config.get('ticket_query_refresh_max', 4))):
                break
        return selected

    async def _collect_special(self, article: CmsArticle) -> ParsedPage:
        """Follow actual links under this event only, including HTML meta redirects."""
        root = event_root(article.url)
        if not root:
            raise SourceUnavailable('不支持的官方专题地址')
        roots = {root}
        queue, visited = [article.url], set()
        selected_stop = None
        result = ParsedPage()
        ticket_rows = {}
        ticket_cities: dict[str, set[str]] = {}
        information_seen = False
        city_performances: dict[str, list[str]] = {}
        cast_pages: list[ParsedPage] = []
        while queue and len(visited) < 8:
            url = urldefrag(queue.pop(0))[0]
            if url in visited:
                continue
            visited.add(url)
            html = await self.client.event_html(url)
            soup = BeautifulSoup(html, 'html.parser')
            # The Shirube home links several cities. Bind the city before following ticket redirects.
            if 'gkmas_livetour_shirube' in root and selected_stop is None:
                for a in soup.select('a[href]'):
                    label = clean(a.get_text(' ') + ' '.join(i.get('alt', '') for i in a.select('img')))
                    destination = urljoin(url, a['href'])
                    if '/information/' in destination and destination.endswith('.php') and label and any(
                        word in label and word in article.title for word in ('福井', '福岡', '岩手', 'ファイナル')):
                        selected_stop = destination.rsplit('/', 1)[-1]
                        queue.insert(0, destination)
                        break
            parsed = parse_ticket_page(html, url)
            if not parsed.ticket_rounds and any(
                    clean(dt.get_text(' ')) in {'受付期間', '申込期間', '販売期間'} for dt in soup.select('dt')):
                raise SourceUnavailable('官网有申请字段但未能解析票务轮次，保留旧记录')
            result.review_notes.extend(parsed.review_notes)
            for row in parsed.ticket_rounds:
                ticket_rows[row.stable_key] = row
                ticket_cities.setdefault(row.stable_key, set()).add(urlsplit(url).path.rsplit('/', 1)[-1])
            result.cast_asset_urls.extend(official_roster_image_urls(html, url))
            performances = parse_information(html, url)
            if performances and (not result.performances or '/information' in url):
                if '/information' in url:
                    if not information_seen:
                        result.performances = []
                        information_seen = True
                    merged = {(p.date, p.session_label, p.venue): p for p in result.performances}
                    for performance in performances:
                        merged.setdefault((performance.date, performance.session_label, performance.venue), performance)
                    result.performances = list(merged.values())
                    city = urlsplit(url).path.rsplit('/', 1)[-1]
                    if city.endswith('.php'):
                        city_performances[city] = [merged[(p.date, p.session_label, p.venue)].stable_key for p in performances]
                else:
                    result.performances = performances
            if '283production_msp' in root:
                cast_data = parse_shiny_information(html, url)
                if cast_data.cast:
                    for row in cast_data.performances:
                        row.venue = parse_venue(html) or article.venue
                    cast_pages.append(cast_data)
            elif 'cast' in url.lower() or '出演者' in clean(soup.get_text(' ')):
                cast_data = parse_day_cast(html, url)
                if cast_data.cast:
                    cast_pages.append(cast_data)
            destinations = []
            for meta in soup.select('meta[http-equiv]'):
                if meta.get('http-equiv', '').lower() == 'refresh':
                    content = meta.get('content', '')
                    if 'url=' in content.lower():
                        destinations.append(urljoin(url, content.split('=', 1)[1].strip(' \"\'')))
            for a in soup.select('a[href]'):
                destination = urldefrag(urljoin(url, a['href']))[0]
                if any(part in destination[len(root):].lower() for part in ('ticket', 'information', 'cast')):
                    destinations.append(destination)
                target_root = event_root(destination)
                if '/live_events/' in root and target_root and '/live_event/' in target_root and any(
                        term in clean(a.get_text(' ')) for term in ('公式', '特設', 'チケット')):
                    roots.add(target_root)
                    destinations.append(destination)
            for destination in dict.fromkeys(destinations):
                if not any(destination.startswith(allowed) for allowed in roots) or destination in visited or destination in queue:
                    continue
                if selected_stop and destination.endswith('.php') and destination.rsplit('/', 1)[-1] != selected_stop:
                    continue
                if 'gkmas_livetour_shirube' in root and not selected_stop and destination != article.url:
                    continue
                queue.append(destination)
        if queue:
            raise SourceUnavailable('专题链接超过单轮8页上限，未完成核验，保留旧记录')
        for cast_data in cast_pages:
            # A day-level CAST heading cannot replace precise information-page
            # clocks or discard other cities. Bind its roster to known sessions.
            if not result.performances:
                result.performances = cast_data.performances
            cast_days = {p.stable_key: p.date for p in cast_data.performances}
            for appearance in cast_data.cast:
                day = cast_days.get(appearance.performance_key)
                sessions = [p for p in result.performances if p.date == day] if day else []
                if not sessions:
                    sessions = [p for p in cast_data.performances if p.stable_key == appearance.performance_key]
                    result.performances.extend(sessions)
                result.cast.extend(replace(appearance, performance_key=p.stable_key) for p in sessions)
        result.ticket_rounds = list(ticket_rows.values())
        for ticket in result.ticket_rounds:
            ticket.performance_keys = list(dict.fromkeys(key for city in sorted(ticket_cities[ticket.stable_key])
                                                        for key in city_performances.get(city, [])))
        result.cast_asset_urls = list(dict.fromkeys(result.cast_asset_urls))
        return result

    async def _cache_roster_assets(self, event_id: str, source_url: str, urls: list[str]) -> list[tuple[str, str]]:
        """Cache a small validated official roster asset; never proxy it elsewhere."""
        saved: list[tuple[str, str]] = []
        target = self.data_dir / "cast-assets" / event_id
        for index, url in enumerate(urls[:4]):
            try:
                await self.client._wait_slot()
                response = await self.client.client.get(url)
                response.raise_for_status()
                content = response.content
                if not 64 < len(content) <= 8 * 1024 * 1024:
                    continue
                with Image.open(BytesIO(content)) as image:
                    image.verify()
                suffix = ".webp" if "webp" in response.headers.get("content-type", "").lower() else ".png"
                path = target / f"{index}-{hashlib.sha256(url.encode()).hexdigest()[:12]}{suffix}"
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_bytes, content)
                saved.append((url, str(path)))
            except (httpx.HTTPError, OSError, ValueError):
                continue
        return saved

    @staticmethod
    def _event_record(article: CmsArticle) -> dict[str, Any]:
        return {"id": article.cms_id, "title": article.title, "brands": article.brands, "url": article.url,
                "event_display": article.event_display, "venue": article.venue, "updated": article.updated}

    @staticmethod
    def _fetchable_special_page(url: str | None) -> bool:
        return event_root(url) is not None

    async def events(self, query: str = "") -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.list_events, query)

    async def tickets(self, query: str = "") -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.tickets, query)

    async def detail(self, query: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.db.detail, query)

    async def stats(self, year: str = "", brand: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self.db.stats, year, brand)

    async def review(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.db.review)

    async def resolve_review(self, item_id: int) -> bool:
        return await asyncio.to_thread(self.db.resolve_review, item_id)

    async def birthday_cast(self, query: str) -> dict[str, Any]:
        """Optional read-only adapter. Missing/unloaded birthday plugin is not an error."""
        if not self.config.get("birthday_integration", False):
            return {"status": "disabled"}
        try:
            module = importlib.import_module("data.plugins.astrbot_plugin_imas_birthday.main")
            api = getattr(module, "call_imasbd_api")
            profile = await api("profile", query=query, render_card=False)
        except (ImportError, AttributeError, RuntimeError, TypeError) as exc:
            return {"status": "unavailable", "reason": type(exc).__name__}
        cv = profile.get('profile', {}).get('cv') if isinstance(profile, dict) and profile.get('ok') else None
        if not cv:
            return {"status": "no_cv", "profile": profile}
        # Current CV is a lookup hint only; historical appearances remain source-authoritative.
        rows = await asyncio.to_thread(self._cast_lookup, str(cv))
        return {"status": "ok", "profile": profile, "matches": rows, "note": "匹配为候选；历史出演以活动官网当场资料为准。"}

    def _cast_lookup(self, query: str) -> list[dict[str, Any]]:
        # Use detail-level fields via a narrow local query without fuzzy auto-linking.
        with self.db._connect() as db:
            return [dict(x) for x in db.execute("SELECT c.*,e.title FROM cast_appearances c JOIN events e ON e.id=c.event_id WHERE c.person_name LIKE ? LIMIT 30", (f"%{query}%",))]

    def _brand_allowed(self, brands_json: str) -> bool:
        wanted = {str(item) for item in self.config.get("brands", []) if str(item)}
        if not wanted:
            return True
        try:
            actual = set(json.loads(brands_json))
        except (TypeError, json.JSONDecodeError):
            return False
        return bool(wanted & actual)

    @staticmethod
    def _display_deadline(value: str, zone: ZoneInfo) -> tuple[str, datetime]:
        deadline = datetime.fromisoformat(value)
        local = deadline.astimezone(zone)
        name = '北京时间' if zone.key == 'Asia/Shanghai' else zone.key
        jst = deadline.astimezone(ZoneInfo('Asia/Tokyo'))
        return f"截止：{local:%Y/%m/%d %H:%M} {name} / {jst:%m/%d %H:%M} JST", local

    @staticmethod
    def _month_window(current: datetime, month: int | None) -> tuple[datetime, datetime, str]:
        """Return a left-closed, right-open local query window and its image title."""
        if month is None:
            start = current.replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=30), "IM@S LIVE! · Next 30 Days"
        year = current.year if month >= current.month else current.year + 1
        start = current.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if month == 12 else start.replace(month=month + 1)
        return start, end, f"IM@S LIVE! · {start.strftime('%B')} {year}"

    def _status(self, current: datetime, zone: ZoneInfo) -> str:
        last = self.db.meta("last_directory_sync") or self.db.meta('last_successful_sync')
        if not last:
            return "尚未同步"
        try:
            is_stale = current.astimezone(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=int(self.config.get("freshness_hours", 12)))
            local_stamp = datetime.fromisoformat(last).astimezone(zone)
            status = ("缓存陈旧｜" if is_stale else "目录更新 ") + local_stamp.strftime('%Y/%m/%d %H:%M')
        except ValueError:
            status = "核验时间格式异常"
        if self.db.meta('last_error'):
            status += '｜部分来源待核验'
        return status

    async def calendar_entries(self, current: datetime | None = None, month: int | None = None) -> tuple[list[dict[str, Any]], datetime, datetime, str, str]:
        """Return only performances in either the default 30-day or requested-month window."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        start, end, title = self._month_window(current, month)
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        entries: list[dict[str, Any]] = []
        for row in performances:
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                display_day = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if start.date() <= display_day < end.date():
                venue = clean(row["venue"] or row["event_venue"] or "场馆待核验")
                session = row["session_label"] or "场次待核验"
                entries.append({"kind": "performance", "display_date": row["date"], "title": row["title"],
                                "subtitle": f"{session}｜{venue}", "brands": json.loads(row["brands_json"]), "url": row["source_url"],
                                "public_number": row.get("public_number"), "source_fetched_at": row.get("source_fetched_at")})
        entries.sort(key=lambda item: (item["display_date"], item["title"], item["subtitle"]))
        return entries, start, end, self._status(current, zone), title

    @staticmethod
    def _ticket_time(value: str, label: str, zone: ZoneInfo) -> str:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError("票务时间缺少时区")
        local = moment.astimezone(zone)
        jst = moment.astimezone(ZoneInfo("Asia/Tokyo"))
        zone_name = "北京时间" if zone.key == "Asia/Shanghai" else zone.key
        return f"{label}：{local:%Y/%m/%d %H:%M} {zone_name} / {jst:%Y/%m/%d %H:%M} JST"

    def _source_is_fresh(self, row: dict[str, Any], current: datetime) -> bool:
        try:
            fetched = datetime.fromisoformat(row["source_fetched_at"])
            return row["source_quality"] == "verified" and fetched.tzinfo is not None and current.astimezone(timezone.utc) - fetched.astimezone(timezone.utc) <= timedelta(hours=max(1, int(self.config.get("freshness_hours", 12))))
        except (TypeError, ValueError):
            return False

    async def ticket_entries(self, current: datetime | None = None) -> tuple[list[dict[str, Any]], datetime, datetime, str]:
        """Keep onsite lottery/resale rounds until the final LIVE session starts."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        start, end, _ = self._month_window(current, None)
        rows = await asyncio.to_thread(self.db.ticket_query_rows)
        performances = await asyncio.to_thread(self.db.ticket_performances)
        by_event: dict[str, list[dict[str, Any]]] = {}
        for performance in performances:
            by_event.setdefault(performance['event_id'], []).append(performance)
        entries: list[dict[str, Any]] = []
        for row in rows:
            if not self._brand_allowed(row["brands_json"]):
                continue
            event_performances = by_event.get(row['event_id'], [])
            if not event_performances and row.get('event_display'):
                event_performances = [vars_for_slots(p) for p in schedule_performances(
                    row['event_display'], row['source_url'], row.get('event_venue'))]
            retained, live_start = self._ticket_event_window(event_performances, current, zone)
            if not retained:
                continue
            bound_keys = json.loads(row.get('performance_keys_json') or '[]')
            bound_ids = set(bound_keys) | {row['event_id'] + ':' + key for key in bound_keys}
            bound = [p for p in event_performances if p.get('id') in bound_ids]
            if bound_keys and bound and not self._ticket_event_window(bound, current, zone)[0]:
                continue
            application_start = self._ticket_moment(row.get("application_start"))
            deadline = self._ticket_moment(row.get("application_end"))
            ticket_status, status_label = self._ticket_display_status(row, current)
            details = [f"轮次：{row['name']}"]
            if application_start:
                details.append(self._ticket_time(row["application_start"], "开始", zone))
            else:
                details.append("开始：待核验")
            if deadline:
                details.append(self._ticket_time(row["application_end"], "截止", zone))
            else:
                details.append("截止：待核验")
            shown_performances = bound or event_performances
            dates = sorted({p['date'] for p in shown_performances if p.get('date')})
            date_text = '～'.join(dict.fromkeys([dates[0], dates[-1]])).replace('-', '/') if dates else ''
            venues = list(dict.fromkeys(clean(p['venue']) for p in shown_performances if p.get('venue')))
            if date_text or venues:
                details.append("演出：" + "｜".join(part for part in (
                    date_text, '／'.join(venues),
                ) if part))
            entries.append({"kind": "ticket", "title": row["title"], "subtitle": "\n".join(details),
                            "brands": json.loads(row["brands_json"]), "url": row["url"] or row["source_url"],
                            "ticket_status": ticket_status, "status_label": status_label,
                            "event_id": row["event_id"], "public_number": row.get("public_number"),
                            "live_start": live_start,
                            "sort_time": deadline or datetime.max.replace(tzinfo=timezone.utc),
                            "source_fetched_at": row.get("source_fetched_at")})
        priority = {"urgent": 0, "open": 1, "upcoming": 2, "stale": 3, "unknown": 3, "ended": 4}
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            groups.setdefault(entry["event_id"], []).append(entry)
        for group in groups.values():
            group.sort(key=lambda item: (priority[item["ticket_status"]], item["sort_time"], item["subtitle"]))
        ordered = sorted(groups.values(), key=lambda group: (
            group[0]['live_start'], group[0]["title"], group[0]["event_id"]))
        return [item for group in ordered for item in group], start, end, self._status(current, zone)

    def _ticket_event_window(self, performances: list[dict[str, Any]], current: datetime,
                             zone: ZoneInfo) -> tuple[bool, datetime]:
        """Keep a tour until every known session has started; unknown dates stay visible."""
        moments = []
        retained = not performances
        for performance in performances:
            moment, precise = self._performance_moment(performance, zone)
            if moment is None:
                retained = True
                continue
            moments.append(moment)
            if precise:
                retained |= current < moment
            else:
                # Date-only schedules are official Japanese dates. Midnight is
                # used for ordering only, never as a claimed start time.
                retained |= current.astimezone(ZoneInfo('Asia/Tokyo')).date() <= moment.astimezone(ZoneInfo('Asia/Tokyo')).date()
        return bool(retained), min(moments, default=datetime.max.replace(tzinfo=timezone.utc))

    @staticmethod
    def _ticket_moment(value: str | None) -> datetime | None:
        try:
            stamp = datetime.fromisoformat(value)
            return stamp if stamp.tzinfo else None
        except (ValueError, TypeError):
            return None

    def _ticket_display_status(self, row: dict[str, Any], current: datetime) -> tuple[str, str]:
        start = self._ticket_moment(row.get("application_start"))
        end = self._ticket_moment(row.get("application_end"))
        if end and current >= end and (not start or end > start):
            status, label = "ended", "已结束"
        elif not start or not end or end <= start:
            status, label = "unknown", "时间待核验"
        elif current < start:
            status, label = "upcoming", "未开始"
        elif end - current <= timedelta(hours=24):
            status, label = "urgent", "24小时内截止"
        else:
            status, label = "open", "抽选中"
        if row.get('sale_method') == 'resale':
            label = '转售 · ' + ('受理中' if status == 'open' else label)
        if not self._source_is_fresh(row, current):
            return ("ended" if status == "ended" else "stale"), label + " · 待核验"
        return status, label

    @staticmethod
    def _performance_moment(row: dict[str, Any], zone: ZoneInfo) -> tuple[datetime | None, bool]:
        """Return an absolute JST moment only when the official page gave a clock."""
        try:
            day = datetime.strptime(str(row["date"]), "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            return None, False
        clock = re.search(r"(?:开演\s*)?(\d{1,2}:\d{2})\s*JST", str(row.get("session_label") or ""), re.I)
        if not clock:
            return datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Tokyo")).astimezone(zone), False
        hour, minute = map(int, clock.group(1).split(":"))
        try:
            return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo("Asia/Tokyo")).astimezone(zone), True
        except ValueError:
            return None, False

    @staticmethod
    def normalize_brand_argument(value: str) -> str | None:
        return BRAND_COMMANDS.get(value.strip().casefold())

    async def next_entry(self, brand_argument: str = "", current: datetime | None = None) -> dict[str, Any] | None:
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        wanted = self.normalize_brand_argument(brand_argument) if brand_argument else None
        if brand_argument and not wanted:
            raise ValueError("未知企划缩写")
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        candidates: list[tuple[datetime, bool, dict[str, Any]]] = []
        for row in performances:
            brands = json.loads(row["brands_json"])
            if wanted and wanted not in brands:
                continue
            moment, precise = self._performance_moment(row, zone)
            if not moment:
                continue
            # Date-only dates remain candidates until the date has passed; they
            # never fabricate a start time or become automatic reminders.
            if precise and moment <= current:
                continue
            if not precise and moment.date() < current.date():
                continue
            candidates.append((moment, precise, row))
        if not candidates:
            return None
        moment, precise, row = sorted(candidates, key=lambda item: (item[0], not item[1], item[2]["id"]))[0]
        detail = await asyncio.to_thread(self.db.detail, row["event_id"])
        cast = []
        if detail:
            cast = [item for item in detail["cast"] if item.get("performance_id") == row["id"]]
            # An event-level list is relevant only when no day-specific list exists.
            if not cast:
                cast = [item for item in detail["cast"] if not item.get("performance_id")]
        assets = await asyncio.to_thread(self.db.cast_asset_rows, row["event_id"])
        day_match = re.search(r"DAY\s*(\d+)", str(row.get("session_label") or ""), re.I)
        day_number = day_match.group(1) if day_match else None
        asset_paths = []
        for asset in assets:
            # Do not send Million's DAY2 roster for a DAY1 query.  Assets that
            # do not identify a day are only used when the performance itself
            # has no day label.
            image_day = re.search(r"bnr_day(\d+)\.webp", asset["image_url"], re.I)
            if day_number and image_day and image_day.group(1) != day_number:
                continue
            if day_number and not image_day:
                continue
            if Path(asset["cached_path"]).is_file():
                asset_paths.append(asset["cached_path"])
        venue = clean(row.get("venue") or row.get("event_venue") or "场馆待核验")
        time_text = f"{moment:%Y/%m/%d %H:%M} {'北京时间' if zone.key == 'Asia/Shanghai' else zone.key}" if precise else f"{moment:%Y/%m/%d}｜开演时间待公布/待核验"
        return {"kind": "performance", "display_date": row["date"], "title": row["title"],
                "subtitle": f"{row.get('session_label') or '场次待核验'}｜{time_text}｜{venue}",
                "brands": json.loads(row["brands_json"]), "url": row.get("source_url") or "",
                "public_number": row.get("public_number"), "cast": cast, "precise": precise,
                "source_fetched_at": row.get("source_fetched_at"),
                "official_url": detail["event"].get("official_url") if detail else row.get("source_url"),
                "cast_assets": asset_paths}

    async def ticket_detail(self, number: int, current: datetime | None = None) -> dict[str, Any] | None:
        """Return an event even when all lottery rounds have ended."""
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        detail = await asyncio.to_thread(self.db.detail_by_public_number, number)
        if not detail:
            return None
        event = detail["event"]
        tickets = [row for row in detail["tickets"] if row.get("ticket_scope") == "onsite"]
        evidence_rows = {row['id']: row for row in await asyncio.to_thread(self.db.ticket_query_rows)}
        result: list[dict[str, Any]] = []
        for row in tickets:
            evidence_row = evidence_rows.get(row['id'], {**row,
                'source_fetched_at': event.get('source_fetched_at'), 'source_quality': event.get('source_quality')})
            ticket_status, status = self._ticket_display_status(evidence_row, current)
            if row["sale_method"] == "first_come":
                status, ticket_status = "一般销售/先到先得", "unknown"
            details = [f"{status}｜{row['name']}"]
            if row.get("application_end"):
                try: details.append(self._ticket_time(row["application_end"], "截止", zone))
                except ValueError: details.append("截止时间待核验")
            result.append({"kind": "ticket", "title": event["title"], "subtitle": "\n".join(details),
                           "brands": json.loads(event["brands_json"]), "url": row.get("url") or row.get("source_url"),
                           "ticket_status": ticket_status, "status_label": status,
                           "public_number": event["public_number"], "sort_time": row.get("application_end") or "", "source_fetched_at": evidence_row.get("source_fetched_at")})
        if not result:
            result.append({"kind": "ticket", "title": event["title"], "subtitle": "尚未公布可核验的现场票务轮次。",
                           "brands": json.loads(event["brands_json"]), "url": event.get("official_url") or "",
                           "ticket_status": "unknown", "status_label": "尚未公布", "public_number": event["public_number"], "sort_time": "", "source_fetched_at": event.get("source_fetched_at")})
        result.sort(key=lambda item: item["sort_time"], reverse=True)
        return {"event": event, "tickets": result, "performances": detail["performances"], "cast": detail["cast"]}

    async def claim_due_reminders(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim ticket alerts with a bounded retry/recovery window."""
        if not self.config.get('enabled', True) or not self.config.get("reminder_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        _, rows = await asyncio.to_thread(self.db.calendar_rows)
        nodes = self.config.get("ticket_reminder_hours", [24, 1])
        try:
            nodes = sorted({max(1, int(value)) for value in nodes}, reverse=True)
        except (TypeError, ValueError):
            nodes = [24, 1]
        try:
            recovery_minutes = max(5, int(self.config.get("reminder_recovery_minutes", 20)))
        except (TypeError, ValueError):
            recovery_minutes = 20
        with self.db._connect() as db:
            umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM ticket_group_subscriptions WHERE enabled=1")]
        selected: list[dict[str, Any]] = []
        for row in rows:
            if not self._source_is_fresh(row, current):
                continue
            if not self._brand_allowed(row["brands_json"]):
                continue
            try:
                deadline = datetime.fromisoformat(row["application_end"])
                started = datetime.fromisoformat(row["application_start"]) if row["application_start"] else None
            except ValueError:
                continue
            now_at_deadline_zone = current.astimezone(deadline.tzinfo)
            if not (deadline.tzinfo and started and started.tzinfo and started < deadline
                    and started <= now_at_deadline_zone < deadline):
                continue
            deadline_text, local = self._display_deadline(row["application_end"], zone)
            eligible_nodes = []
            for node in nodes:
                due = deadline - timedelta(hours=node)
                if due <= now_at_deadline_zone < min(due + timedelta(minutes=recovery_minutes), deadline):
                    eligible_nodes.append(node)
            # A long delayed cycle can overlap two nodes; send only the closer
            # one so a late 24-hour alert never lands beside the one-hour alert.
            for node in sorted(eligible_nodes)[:1]:
                due = deadline - timedelta(hours=node)
                for umo in umos:
                    created = self.db.subscription_created_at("ticket", umo)
                    if created and created.astimezone(deadline.tzinfo) > due:
                        continue
                    identity_ids = await asyncio.to_thread(self.db.ticket_identity_ids, row['id'])
                    keys = [hashlib.sha256(f"ticket|{umo}|{identity}|{row['application_end']}|{node}h".encode()).hexdigest()
                            for identity in identity_ids]
                    key = keys[0]
                    payload = f"{row['title']}｜{row['name']}｜{deadline_text}｜提前{node}小时"
                    claimed = await asyncio.to_thread(self.db.claim_delivery, key, umo, payload, 'legacy', keys[1:])
                    if claimed:
                        selected.append({"delivery_key": key, "umo": umo, "title": row["title"], "brands": json.loads(row["brands_json"]),
                                         "subtitle": f"{row['name']}｜{deadline_text}｜提前{node}小时", "url": row["url"] or row["source_url"],
                                         "remaining_minutes": max(0, int((deadline - now_at_deadline_zone).total_seconds() // 60))})
        if selected:
            logger.info("IM@S claimed ticket reminders: count=%s subscriptions=%s", len(selected), len(umos))
        return selected

    async def finish_reminders(self, rows: list[dict[str, Any]], success: bool) -> None:
        for row in rows:
            await asyncio.to_thread(self.db.finish_delivery, row["delivery_key"], success)

    async def claim_new_ticket_announcements(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim newly verified onsite lottery rounds for LIVE-subscribed groups.

        Candidate rows are written atomically with a successful special-page
        parse.  The source baseline rules live in the database, while this
        method only decides whether a still-valid candidate may be delivered.
        """
        if not self.config.get("enabled", True) or not self.config.get("ticket_new_announcement_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        rows = await asyncio.to_thread(self.db.new_ticket_round_rows)
        with self.db._connect() as db:
            umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM live_group_subscriptions WHERE enabled=1")]
        selected: list[dict[str, Any]] = []
        for row in rows:
            if not self._brand_allowed(row["brands_json"]) or not self._source_is_fresh(row, current):
                continue
            try:
                observed = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue
            start = end = None
            try:
                start = datetime.fromisoformat(row["application_start"]) if row.get("application_start") else None
            except (TypeError, ValueError):
                pass
            try:
                end = datetime.fromisoformat(row["application_end"]) if row.get("application_end") else None
            except (TypeError, ValueError):
                pass
            if end and end.tzinfo and current.astimezone(end.tzinfo) >= end:
                continue
            if start and end and start.tzinfo and end.tzinfo and end > start:
                if current.astimezone(start.tzinfo) < start:
                    status, ticket_status = "新抽选已公布／尚未开始", "upcoming"
                else:
                    status, ticket_status = "新抽选现已开放", "open"
            else:
                status, ticket_status = "新抽选已公布／起止时间待核验", "unknown"
            times: list[str] = []
            if start and start.tzinfo:
                times.append(self._ticket_time(row["application_start"], "开始", zone))
            else:
                times.append("开始时间待核验")
            if end and end.tzinfo:
                times.append(self._ticket_time(row["application_end"], "截止", zone))
            else:
                times.append("截止时间待核验")
            subtitle = f"{row['name']}｜{status}\n" + "\n".join(times)
            url = row.get("url") or row.get("source_url") or ""
            for umo in umos:
                enabled_since = await asyncio.to_thread(self.db.subscription_updated_at, "live", umo)
                # A group enabled after discovery deliberately does not receive
                # accumulated announcements, including after disable/re-enable.
                if enabled_since and observed < enabled_since:
                    continue
                identity_ids = await asyncio.to_thread(self.db.ticket_identity_ids, row['round_id'])
                keys = [f"ticket_new|{umo}|{identity}" for identity in identity_ids]
                key = keys[0]
                payload = f"#{row.get('public_number') or '?'} {row['title']}｜{row['name']}｜{status}"
                if await asyncio.to_thread(self.db.claim_delivery, key, umo, payload, "ticket_new", keys[1:]):
                    selected.append({"delivery_key": key, "notification_type": "ticket_new", "umo": umo,
                                     "title": row["title"], "brands": json.loads(row["brands_json"]),
                                     "public_number": row.get("public_number"), "subtitle": subtitle,
                                     "url": url, "ticket_status": ticket_status, "status_label": status,
                                     "kind": "ticket", "remaining_minutes": 0})
        return selected

    async def claim_due_live_reminders(self, current: datetime | None = None) -> list[dict[str, Any]]:
        """Claim only precise official start times, one hour before Beijing display time."""
        if not self.config.get("enabled", True) or not self.config.get("live_reminder_enabled", True):
            return []
        zone = ZoneInfo(str(self.config.get("display_timezone", "Asia/Shanghai")))
        current = (current or datetime.now(zone)).astimezone(zone)
        performances, _ = await asyncio.to_thread(self.db.calendar_rows)
        with self.db._connect() as db:
            umos = [str(x["umo"]) for x in db.execute("SELECT umo FROM live_group_subscriptions WHERE enabled=1")]
        selected: list[dict[str, Any]] = []
        for row in performances:
            if not self._brand_allowed(row["brands_json"]) or not self._source_is_fresh(row, current):
                continue
            moment, precise = self._performance_moment(row, zone)
            if not precise or not moment:
                continue
            due = moment - timedelta(hours=max(1, int(self.config.get("live_reminder_before_hours", 1))))
            if not (due <= current < due + timedelta(minutes=REMINDER_WINDOW_MINUTES)):
                continue
            for umo in umos:
                created = self.db.subscription_created_at("live", umo)
                if created and created.astimezone(zone) > due:
                    continue
                key = hashlib.sha256(f"live|{umo}|{row['id']}|{moment.isoformat()}".encode()).hexdigest()
                text = f"#{row.get('public_number') or '?'} {row['title']}｜北京时间 {moment:%Y/%m/%d %H:%M} 开演"
                if await asyncio.to_thread(self.db.claim_delivery, key, umo, text):
                    selected.append({"delivery_key": key, "umo": umo, "title": row["title"],
                                     "brands": json.loads(row["brands_json"]), "public_number": row.get("public_number"),
                                     "subtitle": f"北京时间 {moment:%Y/%m/%d %H:%M} 开演｜{row.get('session_label') or ''}",
                                     "url": row.get("source_url") or "", "remaining_minutes": max(0, int((moment-current).total_seconds() // 60))})
        return selected

    async def import_records(self, rows: list[dict[str, Any]], override: bool = False) -> int:
        """Controlled programmatic import: only explicit CMS IDs may be overwritten."""
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("id", "title")):
                raise ValueError("导入记录必须有非空 id 与 title")
            if not override and await self.detail(row["id"]):
                continue
            await asyncio.to_thread(self.db.upsert_event, {"id": row["id"], "title": row["title"], "brands": row.get("brands", []), "url": row.get("url"), "event_display": None, "venue": None, "updated": None})
            count += 1
        return count

    async def import_file(self, filename: str, kind: str, override: bool = False) -> int:
        """Read a host-provided JSON/CSV only from this plugin's own imports directory."""
        name = Path(filename).name
        expected = ".json" if kind == "json" else ".csv"
        if name != filename or not name.endswith(expected):
            raise ValueError(f"只接受 imports 目录中的 {expected} 文件名")
        path = self.data_dir / "imports" / name
        if not path.is_file():
            raise ValueError("文件不存在；请由管理员先放入本插件 data/imports 目录")
        if kind == "json":
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            rows = json.loads(content)
        else:
            def read_csv() -> list[dict[str, str]]:
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    return list(csv.DictReader(stream))
            rows = await asyncio.to_thread(read_csv)
        if not isinstance(rows, list):
            raise ValueError("导入文件根节点必须是记录数组")
        return await self.import_records(rows, override)
