"""Overview-first, one native Nodes container, and cached query-window details."""
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from imas_live.models import CastAppearance, Evidence, Performance, TicketRound
from imas_live.service import ImasLiveService
from test_lifecycle import plugin_module

NOW = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
ROOT = 'https://idolmaster-official.jp/live_event/example/'


class CachedForwardDetails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ImasLiveService(Path(self.temp.name))
        self.service.client.event_html = AsyncMock(side_effect=AssertionError('forward must not crawl'))

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def save(self, event, performances, tickets=(), cast=()):
        root = ROOT.replace('example', event)
        self.service.db.upsert_event({'id': event, 'title': event+' LIVE', 'url': root, 'brands': []})
        self.service.db.save_parsed(event, root, 'initial', 'test', tickets, performances, cast, [])

    def performance(self, key, day, session='DAY1 开演 17:00 JST'):
        return Performance(key, day, session, 'Official Hall', evidence=Evidence(ROOT, 'official', 'test'))

    async def test_ticket_group_keeps_each_seat_url_status_and_both_timezones(self):
        tickets = [TicketRound(key, 'Premium先行', 'onsite', 'lottery',
            (NOW-timedelta(days=1)).isoformat(), end.isoformat(), url='https://asobiticket2.asobistore.jp/receptions/'+key,
            seats=seat, evidence=Evidence(ROOT, 'official', 'test'))
            for key, seat, end in [('sp', 'SP席', NOW+timedelta(hours=1)), ('normal', 'S席・A席', NOW+timedelta(days=5))]]
        self.save('a', [self.performance('d1', '2026-12-19')], tickets)
        entries, *_ = await self.service.ticket_entries(NOW)
        texts = await self.service.query_detail_texts(entries, 'ticket')
        self.assertEqual(len(texts), 1)
        text = texts[0]
        self.assertEqual(text.count('轮次：Premium先行'), 2)
        for expected in ['SP席', 'S席・A席', '/receptions/sp', '/receptions/normal', '北京时间', 'JST',
                         '24小时内截止', '抽选中', '2026-12-19', 'Official Hall', '/live_event/a/']:
            self.assertIn(expected, text)
        self.service.client.event_html.assert_not_awaited()

    async def test_real_saved_raw_cast_keys_and_window_never_leak_day2(self):
        self.save('a', [self.performance('d1', '2026-10-10'), self.performance('d2', '2026-11-10', 'DAY2 开演 18:00 JST')],
            cast=[CastAppearance('Day1 Actor', 'Role1', 'd1'), CastAppearance('Day2 Actor', 'Role2', 'd2'),
                  CastAppearance('Generic Actor', None, None)])
        entries, *_ = await self.service.calendar_entries(NOW, 10)
        self.assertEqual(entries[0]['performance_id'], 'a:d1')
        texts = await self.service.query_detail_texts(entries, 'live')
        self.assertIn('Day1 Actor（Role1）', texts[0])
        for outside in ['Day2 Actor', '2026-11-10', 'Generic Actor']:
            self.assertNotIn(outside, texts[0])
        self.assertIn('2026/10/10 16:00 北京时间', texts[0])
        self.service.client.event_html.assert_not_awaited()

    async def test_event_group_order_and_event_level_cast_without_inventing_times(self):
        self.save('a', [self.performance('d1', '2026-10-10', 'DAY1'), self.performance('d2', '2026-10-12', 'DAY2')],
                  cast=[CastAppearance('Official Actor', None, None)])
        self.save('b', [self.performance('d1', '2026-10-11', 'DAY1')])
        entries, *_ = await self.service.calendar_entries(NOW, 10)
        self.assertEqual([e['event_id'] for e in entries], ['a', 'b', 'a'])
        texts = await self.service.query_detail_texts(entries, 'live')
        self.assertEqual(len(texts), 2)
        self.assertIn('a LIVE', texts[0])
        self.assertEqual(texts[0].count('Official Actor'), 2)
        self.assertIn('未收录可核验的本场文字名单', texts[1])
        self.assertIn('开演时间待公布／待核验', texts[0])
        self.assertNotIn('00:00', texts[0])

    async def test_missing_day1_cast_falls_back_to_event_level_not_day2(self):
        self.save('a', [self.performance('d1', '2026-10-10'), self.performance('d2', '2026-11-10', 'DAY2')],
            cast=[CastAppearance('Day2 Actor', None, 'd2'), CastAppearance('Event-level Actor', None, None)])
        entries, *_ = await self.service.calendar_entries(NOW, 10)
        text = (await self.service.query_detail_texts(entries, 'live'))[0]
        self.assertIn('Event-level Actor', text)
        self.assertNotIn('Day2 Actor', text)


@dataclass
class NodeContract:
    content: list
    uin: str
    name: str


@dataclass
class NodesContract:
    nodes: list


class ChainContract:
    def __init__(self, chain):
        self.chain = chain


class QueryForwardDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = plugin_module()
        self.module.Node, self.module.Nodes, self.module.MessageChain = NodeContract, NodesContract, ChainContract
        self.plugin = self.module.ImasLivePlugin.__new__(self.module.ImasLivePlugin)
        self.plugin.config = {}
        self.plugin.service = Mock()
        self.plugin.service.query_detail_texts = AsyncMock(return_value=['#7 a\nSP / normal', '#8 b'])
        self.plugin.service.refresh_open_ticket_sources = AsyncMock(return_value={'refreshed': 0, 'failed': 0})
        self.entries = [{'event_id': 'a', 'public_number': 7, 'title': 'a LIVE', 'url': 'https://long.example/one'},
                        {'event_id': 'b', 'public_number': 8, 'title': 'b LIVE', 'url': 'https://long.example/two'}]
        self.plugin.service.ticket_entries = AsyncMock(return_value=(self.entries, NOW, NOW, ''))
        self.plugin.service.calendar_entries = AsyncMock(return_value=(self.entries, NOW, NOW, '', 'LIVE'))
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name)/'overview.png'
        self.image.touch()
        self.plugin.renderer = Mock()
        self.plugin.renderer.render_ticket.return_value = self.image
        self.plugin.renderer.render_calendar.return_value = [self.image]
        async def ready(_event):
            if False:
                yield None
        self.plugin._wait_for_first_directory = ready
        self.event = Mock()
        self.event.get_platform_name.return_value = 'aiocqhttp'
        self.event.get_platform_id.return_value = 'my-custom-qq-instance'
        self.event.get_self_id.return_value = '12345678'
        self.event.chain_result.side_effect = lambda chain: ('chain', chain)
        self.event.plain_result.side_effect = lambda text: ('text', text)
        self.event.send = AsyncMock()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_overview_yields_before_one_nodes_container_in_group_and_private(self):
        for origin in ['qq:GroupMessage:42', 'qq:FriendMessage:43']:
            with self.subTest(origin=origin):
                self.event.unified_msg_origin = origin
                self.event.send.reset_mock()
                generator = self.plugin.imasticket(self.event)
                image = await anext(generator)
                self.assertEqual(image[0], 'chain')
                self.assertEqual(len(image[1]), 1)  # no external Plain URL list
                self.event.send.assert_not_awaited()
                with self.assertRaises(StopAsyncIteration):
                    await anext(generator)
                chain = self.event.send.await_args.args[0]
                self.assertEqual(len(chain.chain), 1)
                container = chain.chain[0]
                self.assertIsInstance(container, NodesContract)
                self.assertEqual(len(container.nodes), 2)
                self.assertEqual([n.content[0].text for n in container.nodes], ['#7 a\nSP / normal', '#8 b'])
                self.assertTrue(all(n.uin == '12345678' and n.name == 'IM@S LIVE' for n in container.nodes))
                self.event.send.assert_awaited_once()

    async def test_live_month_also_sends_once_after_image(self):
        responses = [r async for r in self.plugin.imaslive(self.event, '10')]
        self.assertEqual([r[0] for r in responses], ['chain'])
        self.plugin.service.calendar_entries.assert_awaited_once_with(month=10)
        self.plugin.service.query_detail_texts.assert_awaited_once_with(self.entries, 'live')
        self.event.send.assert_awaited_once()

    async def test_failed_forward_keeps_image_and_short_index_without_long_urls(self):
        for failure in [RuntimeError('adapter does not support forwarding'), False]:
            self.event.send.reset_mock()
            self.event.send.side_effect = failure if isinstance(failure, Exception) else None
            self.event.send.return_value = failure
            responses = [r async for r in self.plugin.imasticket(self.event)]
            self.assertEqual([r[0] for r in responses], ['chain', 'text'])
            fallback = responses[1][1]
            self.assertIn('#7', fallback)
            self.assertIn('/imasticket get <编号>', fallback)
            self.assertNotIn('https://', fallback)
            self.assertNotIn('图片生成失败', fallback)

    async def test_unsupported_platform_or_missing_self_id_does_not_send_forward(self):
        for platform, self_id in [('telegram', '12345678'), ('aiocqhttp', None)]:
            self.event.get_platform_name.return_value = platform
            self.event.get_self_id.return_value = self_id
            responses = [r async for r in self.plugin.imasticket(self.event)]
            self.assertEqual([r[0] for r in responses], ['chain', 'text'])
        self.event.send.assert_not_awaited()
        self.plugin.service.query_detail_texts.assert_not_awaited()

    async def test_empty_result_only_emits_image_and_index_is_bounded(self):
        self.plugin.service.ticket_entries.return_value = ([], NOW, NOW, '')
        responses = [r async for r in self.plugin.imasticket(self.event)]
        self.assertEqual([r[0] for r in responses], ['chain'])
        self.event.send.assert_not_awaited()
        self.plugin.service.query_detail_texts.assert_not_awaited()
        index = self.plugin._query_index([{'event_id': str(i), 'public_number': i, 'title': 'x'*100} for i in range(40)])
        self.assertLess(len(index), 800)
        self.assertIn('另有 28 个活动', index)

    async def test_forward_format_error_is_separate_from_image_and_render_error_never_forwards(self):
        self.plugin.service.query_detail_texts.side_effect = ValueError('bad cache')
        responses = [r async for r in self.plugin.imasticket(self.event)]
        self.assertEqual([r[0] for r in responses], ['chain', 'text'])
        self.assertNotIn('图片生成失败', responses[1][1])
        self.event.send.assert_not_awaited()
        self.plugin.renderer.render_ticket.side_effect = ValueError('font')
        responses = [r async for r in self.plugin.imasticket(self.event)]
        self.assertEqual([r[0] for r in responses], ['text'])
        self.assertIn('图片生成失败', responses[0][1])
