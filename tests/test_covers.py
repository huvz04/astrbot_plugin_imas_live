"""Explicit sources, bounded background caching, local-only paired details."""
import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

from imas_live.cms import CmsArticle, SourceUnavailable
from imas_live.covers import (IMAGE_API, MAX_COVER_BYTES, cms_thumbnail_url, og_cover_url,
    official_image_url, validate_cover, download_cover, local_cover, store_cover)
from imas_live.database import Database
from imas_live.models import Evidence, Performance, ParsedPage
from imas_live.service import ImasLiveService

ROOT = 'https://idolmaster-official.jp/live_event/example/'
THUMB = '/idolmaster/jp/live-event/002/2026/09/cover.jpeg'
NOW = datetime.now(timezone.utc)
QUERY_NOW = datetime(2027, 11, 1, tzinfo=timezone.utc)


def png(color='red'):
    result = BytesIO()
    Image.new('RGB', (640, 360), color).save(result, 'PNG')
    return result.getvalue()


class CoverSources(unittest.TestCase):
    def test_cms_relative_thumbnail_uses_image_api_and_strips_resource_query(self):
        url = cms_thumbnail_url(THUMB+'?_=abcdef#top')
        self.assertEqual(urlsplit(url).path, '/sitern/api/idolmaster/Image/get')
        self.assertEqual(parse_qs(urlsplit(url).query), {'path': [THUMB]})
        self.assertEqual(url, cms_thumbnail_url(THUMB))
        self.assertNotIn('https://idolmaster-official.jp/idolmaster/', url)
        for value in [None, '', {}, '//evil.test/image.png', 'https://evil.test/image.png', '/other/file.jpg']:
            self.assertIsNone(cms_thumbnail_url(value))

    def test_static_og_relative_url_and_dynamic_official_image_api(self):
        self.assertEqual(og_cover_url('<meta property="og:image" content="ogp.png">', ROOT), ROOT+'ogp.png')
        self.assertEqual(og_cover_url('<meta property="og:image" content="ogp.png">', ROOT.rstrip('/')), ROOT+'ogp.png')
        url = cms_thumbnail_url(THUMB)
        self.assertEqual(og_cover_url('<meta property="og:image" content="'+url+'">',
            'https://idolmaster-official.jp/live_events/example'), url)

    def test_never_use_body_cast_sponsor_generic_logo_or_other_event(self):
        for html in ['<img src="main.png">', '<h2>CAST</h2><img src="bnr_day1.webp">',
                     '<meta property="og:image" content="https://idolmaster-official.jp/assets/ogp.png">',
                     '<meta property="og:image" content="logo.png">',
                     '<meta property="og:image" content="sponsor.png">',
                     '<meta property="og:image" content="../different/ogp.png">']:
            self.assertIsNone(og_cover_url(html, ROOT))
        self.assertFalse(official_image_url('https://idolmaster-official.jp.evil.test/file.jpg'))
        self.assertFalse(official_image_url('http://idolmaster-official.jp/file.jpg'))

    def test_image_decode_limits_reject_pseudo_images_tiny_logos_and_giant_canvas(self):
        self.assertEqual(validate_cover(png())[0], '.png')
        for size in [(20, 20), (8193, 180), (5000, 4100)]:
            result = BytesIO()
            Image.new('1', size).save(result, 'PNG')
            with self.assertRaises(ValueError):
                validate_cover(result.getvalue())
        with self.assertRaises(Exception):
            validate_cover(b'<html>not image</html>'*10)


class Streaming(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.read = chunks, 0
    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


class CoverCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))
        self.db = self.service.db
        self.calls = []
        self.handler = lambda request: httpx.Response(200, content=png(), headers={'content-type': 'image/png', 'etag': 'v1'})
        def transport(request):
            self.calls.append(request)
            return self.handler(request)
        await self.service.client.client.aclose()
        self.service.client.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.service.client._wait_slot = AsyncMock()
        self.article = self.seed('a', THUMB)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def seed(self, event, thumbnail=THUMB, title=None, day='2027-12-01', venue='Hall'):
        article = CmsArticle(event, title or event+' LIVE', ROOT, [], day, venue, None,
                             {'_id': event, 'thumbnail': thumbnail})
        self.db.upsert_event(self.service._event_record(article))
        self.db.save_parsed(event, ROOT, event+' initial', 'test', [], [
            Performance('day', day, '开演 18:00 JST', venue, evidence=Evidence(ROOT, 'official', 'test'))], [], [])
        return article

    async def test_background_cache_metadata_atomic_files_and_unchanged_no_download(self):
        await self.service._cache_event_cover(self.article, ROOT+'ogp.png')
        row = self.db.event_cover('a')
        self.assertEqual(row['source_kind'], 'cms_thumbnail')
        self.assertEqual(row['source_url'], ROOT)
        self.assertEqual(row['thumbnail_cms_id'], 'a')
        self.assertEqual(row['content_hash'], hashlib.sha256(png()).hexdigest())
        self.assertTrue(row['verified_at'])
        self.assertTrue(Path(row['cached_path']).is_file())
        self.assertIn('event-covers', row['cached_path'])
        self.assertFalse(list(Path(self.temp.name).rglob('.cover-*')))
        await self.service._cache_event_cover(self.article, ROOT+'ogp.png')
        self.assertEqual(len(self.calls), 1)
        self.service.client._wait_slot.assert_awaited_once()

    async def test_expired_cache_revalidation_304_keeps_hash_and_local_file(self):
        await self.service._cache_event_cover(self.article, None)
        previous = self.db.event_cover('a')
        with self.db._connect() as conn:
            conn.execute('UPDATE event_covers SET verified_at=?', ((NOW-timedelta(days=2)).isoformat(),))
        self.handler = lambda _: httpx.Response(304, headers={'etag': 'v1'})
        await self.service._cache_event_cover(self.article, None)
        self.assertEqual(self.calls[-1].headers['if-none-match'], 'v1')
        self.assertEqual(self.db.event_cover('a')['content_hash'], previous['content_hash'])
        self.assertIsNotNone(local_cover(self.db.event_cover('a'), Path(self.temp.name)/'event-covers'))

    async def test_corrupt_cache_redownload_same_content_repairs_existing_hash_path(self):
        await self.service._cache_event_cover(self.article, None)
        path = Path(self.db.event_cover('a')['cached_path'])
        path.write_bytes(b'corrupted')
        self.assertIsNone(local_cover(self.db.event_cover('a'), Path(self.temp.name)/'event-covers'))
        await self.service._cache_event_cover(self.article, None)
        self.assertEqual(path.read_bytes(), png())
        self.assertEqual(len(self.calls), 2)

    async def test_thumbnail_failure_uses_explicit_og_and_cached_fallback_not_redownloaded(self):
        def responses(request):
            if request.url.host.startswith('cmsapi'):
                return httpx.Response(404)
            return httpx.Response(200, content=png('blue'), headers={'content-type': 'image/png'})
        self.handler = responses
        await self.service._cache_event_cover(self.article, ROOT+'ogp.png')
        self.assertEqual(self.db.event_cover('a')['image_url'], ROOT+'ogp.png')
        self.assertEqual(self.db.event_cover('a')['source_kind'], 'og_image')
        await self.service._cache_event_cover(self.article, ROOT+'ogp.png')
        self.assertEqual(sum(r.url.host == 'idolmaster-official.jp' for r in self.calls), 1)

    async def test_dynamic_ssr_fallback_fetches_head_not_first_body_image(self):
        url = 'https://idolmaster-official.jp/live_events/example'
        self.article.url = url
        self.db.upsert_event(self.service._event_record(CmsArticle('a', 'a LIVE', url, [], '2027年12月1日', 'Hall', None, {'thumbnail': None})))
        image_url = cms_thumbnail_url(THUMB)
        self.service.client.fetch_html = AsyncMock(return_value='<meta property="og:image" content="'+image_url+'"><img src="wrong.png">')
        await self.service._cache_event_cover(self.article, None)
        self.service.client.fetch_html.assert_awaited_once_with(url)
        self.assertEqual(self.db.event_cover('a')['image_url'], image_url)

    async def test_same_topic_city_thumbnails_remain_per_article_not_per_url(self):
        other = self.seed('b', THUMB.replace('cover.jpeg', 'city-b.jpeg'), day='2027-12-02', venue='Other Hall')
        await self.service._cache_event_cover(self.article, ROOT+'ogp.png')
        await self.service._cache_event_cover(other, ROOT+'ogp.png')
        self.assertNotEqual(self.db.event_cover('a')['image_url'], self.db.event_cover('b')['image_url'])
        self.assertEqual([parse_qs(r.url.query.decode())['path'][0] for r in self.calls], [THUMB, THUMB.replace('cover.jpeg', 'city-b.jpeg')])
        self.assertEqual(len(self.db.list_events(limit=10)), 2)

    async def test_failures_do_not_change_verified_facts_and_have_retry_backoff(self):
        for response in [lambda _: httpx.Response(200, content=b'<html>not image</html>'*10, headers={'content-type': 'image/png'}),
                         lambda _: httpx.Response(503),
                         lambda _: httpx.Response(200, content=b'html', headers={'content-type': 'text/html'})]:
            with self.subTest(response=response):
                with self.db._connect() as conn:
                    conn.execute('UPDATE event_covers SET attempted_at=NULL,error=NULL')
                self.handler = response
                await self.service._cache_event_cover(self.article, None)
                self.assertTrue(self.db.event_cover('a')['error'])
                self.assertIsNone(self.db.event_cover('a')['cached_path'])
                count = len(self.calls)
                await self.service._cache_event_cover(self.article, None)
                self.assertEqual(len(self.calls), count)
                self.assertEqual(self.db.detail('a')['event']['source_quality'], 'verified')

    async def test_streaming_cap_stops_before_tail_and_preflight_content_length_rejects(self):
        stream = Streaming([b'x'*(2*1024*1024), b'x'*(2*1024*1024+1), b'never read'])
        self.handler = lambda _: httpx.Response(200, stream=stream, headers={'content-type': 'image/png'})
        with self.assertRaisesRegex(ValueError, 'streamed byte limit'):
            await download_cover(self.service.client, cms_thumbnail_url(THUMB))
        self.assertEqual(stream.read, 2)
        stream = Streaming([b'never read'])
        self.handler = lambda _: httpx.Response(200, stream=stream, headers={'content-type': 'image/png', 'content-length': str(MAX_COVER_BYTES+1)})
        with self.assertRaisesRegex(ValueError, 'content length'):
            await download_cover(self.service.client, cms_thumbnail_url(THUMB))
        self.assertEqual(stream.read, 0)

    async def test_untrusted_redirect_and_timeout_are_isolated(self):
        self.handler = lambda _: httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        with self.assertRaisesRegex(ValueError, 'untrusted'):
            await download_cover(self.service.client, cms_thumbnail_url(THUMB))
        self.assertEqual(len(self.calls), 1)
        self.service.client._wait_slot.side_effect = asyncio.TimeoutError()
        await self.service._cache_event_cover(self.article, None)
        self.assertEqual(self.db.event_cover('a')['error'], 'TimeoutError')

    async def test_query_details_are_event_paired_local_only_and_corrupt_image_is_text_only(self):
        other = self.seed('b', THUMB.replace('cover.jpeg', 'city-b.jpeg'), day='2027-12-02', venue='Other Hall')
        await self.service._cache_event_cover(other, None)
        await self.service._cache_event_cover(self.article, None)
        self.service.client.event_html = AsyncMock(side_effect=AssertionError('query must not fetch'))
        self.service.client.fetch_html = AsyncMock(side_effect=AssertionError('query must not fetch'))
        rows, *_ = await self.service.calendar_entries(QUERY_NOW, 12)
        # Reverse the groups to prove pairing by event identity, not cache order.
        details = await self.service.query_details(list(reversed(rows)), 'live')
        self.assertEqual([d['event_id'] for d in details], ['b', 'a'])
        for detail in details:
            self.assertIn(detail['event_id']+' LIVE', detail['text'])
            self.assertEqual(detail['image_path'], self.db.event_cover(detail['event_id'])['cached_path'])
        texts = await self.service.query_detail_texts(rows, 'live')
        self.assertTrue(all(isinstance(t, str) for t in texts))
        self.assertEqual(len(self.calls), 2)
        Path(self.db.event_cover('b')['cached_path']).unlink()
        self.assertIsNone((await self.service.query_details(list(reversed(rows)), 'live'))[0]['image_path'])
        self.service.client.event_html.assert_not_awaited()
        self.service.client.fetch_html.assert_not_awaited()

    async def test_budget_removes_images_not_text_and_path_escape_is_rejected(self):
        await self.service._cache_event_cover(self.article, None)
        rows, *_ = await self.service.calendar_entries(QUERY_NOW, 12)
        with patch('imas_live.service.FORWARD_BUDGET', 1):
            detail, = await self.service.query_details(rows, 'live')
        self.assertIsNone(detail['image_path'])
        self.assertIn('a LIVE', detail['text'])
        self.assertIsNone(local_cover(self.db.event_cover('a'), Path(self.temp.name)/'elsewhere'))

    async def test_cover_work_only_background_future_refresh_and_exception_does_not_fail_sync(self):
        self.service._collect_special = AsyncMock(return_value=ParsedPage(performances=[
            Performance('day', '2027-12-01', '开演 18:00 JST', 'Hall', evidence=Evidence(ROOT, 'official', 'test'))]))
        self.service._cache_event_cover = AsyncMock(side_effect=RuntimeError('cache disk failure'))
        await self.service._refresh_article(self.article)
        self.service._cache_event_cover.assert_not_awaited()
        with self.assertLogs('imas_live.service', level='WARNING'):
            await self.service._refresh_article(self.article, cache_cover=True)
        self.assertEqual(self.db.detail('a')['event']['source_quality'], 'verified')
        self.service._cache_event_cover.reset_mock()
        self.service._collect_special.return_value.performances[0].date = '2000-01-01'
        await self.service._refresh_article(self.article, cache_cover=True)
        self.service._cache_event_cover.assert_not_awaited()

    async def test_upgrade_and_event_merge_inherit_cover_alias_without_foreign_key_failure(self):
        with self.db._connect() as conn:
            conn.execute('DROP TABLE event_covers')  # legacy schema without covers
        self.db = self.service.db = Database(self.db.path)
        self.assertIsNone(self.db.event_cover('a'))
        with patch.object(self.db, '_same_event', return_value=False):
            duplicate = self.seed('changed-cms', THUMB, title='a LIVE')
        await self.service._cache_event_cover(duplicate, None)
        before = self.db.event_cover('changed-cms')['cached_path']
        self.assertEqual(self.db.reconcile_event_identities(), 1)
        self.assertEqual(self.db.event_cover('a')['cached_path'], before)
        self.assertEqual(self.db.event_cover('changed-cms')['cached_path'], before)
        self.db.save_event_cover('changed-cms', cms_thumbnail_url(THUMB), ROOT, 'cms_thumbnail', before,
                                hashlib.sha256(png()).hexdigest())
        with self.db._connect() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM event_covers').fetchone()[0], 1)
        self.assertEqual(self.db.reconcile_event_identities(), 0)

    async def test_explicit_cover_is_available_when_ticket_parser_has_no_facts(self):
        self.service._collect_special = AsyncMock(return_value=ParsedPage())
        with self.assertRaises(SourceUnavailable):
            await self.service._refresh_article(self.article, cache_cover=True)
        self.assertIsNotNone(local_cover(self.db.event_cover('a'), Path(self.temp.name)/'event-covers'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.db.detail('a')['performances'][0]['date'], '2027-12-01')
