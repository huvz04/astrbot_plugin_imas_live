import json
import unittest
from pathlib import Path
import httpx
from unittest.mock import AsyncMock

from imas_live.cms import OfficialCmsClient, SourceUnavailable


class CmsFixtureTests(unittest.TestCase):
    def test_live_directory_item_is_an_event_candidate_not_a_performance_count(self):
        fixture = Path(__file__).parent / "fixtures" / "live.json"
        item = json.loads(fixture.read_text(encoding="utf-8"))["data"]["article_list"][0]
        article = OfficialCmsClient._article(item)
        self.assertTrue(article.cms_id)
        self.assertTrue(article.title)
        self.assertIsInstance(article.brands, list)


class CmsFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_incomplete_directory_is_not_empty_success(self):
        client = OfficialCmsClient(min_interval=0)
        client.token = AsyncMock(return_value='test-token')
        cases = [{'total_count': 0, 'article_list': []}, {'total_count': 20, 'article_list': []},
                 {'total_count': 'invalid', 'article_list': []}, {'article_list': []},
                 {'total_count': 1, 'article_list': [None]}]
        for body in cases:
            with self.subTest(body=body):
                client._get = AsyncMock(return_value={'data': body})
                with self.assertRaises(SourceUnavailable):
                    await client.live_articles()
        await client.close()

    async def test_http_401_refreshes_token_before_retry(self):
        client = OfficialCmsClient(min_interval=0)
        client._token = 'expired'
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.path.endswith('Token/get'):
                return httpx.Response(200, json={'statusCode': 200, 'data': {'token': 'renewed'}})
            if request.url.params.get('token') == 'expired':
                return httpx.Response(401)
            return httpx.Response(200, json={'statusCode': 200, 'data': {'apiStatus': True}})
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        result = await client._get('idolmaster/Article/list', {'token': 'expired'})
        self.assertTrue(result['data']['apiStatus'])
        self.assertEqual(calls[-1].url.params['token'], 'renewed')
        self.assertEqual(len(calls), 3)
        await client.close()
