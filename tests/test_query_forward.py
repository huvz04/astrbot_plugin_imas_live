"""Overview-first, one native Nodes container, and cached query-window details."""
import tempfile
import inspect
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


async def stopped_pipeline(event, handler, *args):
    """Essential v4.27.2/3 call_handler -> _process_stages boundary.

    Official sources: astrbot/core/pipeline/context_utils.py and
    astrbot/core/pipeline/scheduler.py at tag v4.27.3. The wrapper yields
    after EACH generator result, but only AFTER a coroutine completes.
    The scheduler breaks on the first wrapper yield when stopped.
    """
    async def call_handler():
        pending = handler(event, *args)
        if inspect.isasyncgen(pending):
            async for result in pending:
                yield result
        else:
            yield await pending
    async for _ in call_handler():
        if event.is_stopped():
            break


class QueryForwardDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = plugin_module()
        self.module.Node, self.module.Nodes, self.module.MessageChain = NodeContract, NodesContract, ChainContract
        self.plugin = self.module.ImasLivePlugin.__new__(self.module.ImasLivePlugin)
        self.plugin.config = {}
        self.plugin.service = Mock()
        self.plugin.service.query_details = AsyncMock(return_value=[
            {'event_id': 'a', 'text': '#7 a\nSP / normal', 'image_path': None},
            {'event_id': 'b', 'text': '#8 b', 'image_path': None}])
        self.plugin.service.refresh_open_ticket_sources = AsyncMock(return_value={'refreshed': 0, 'failed': 0})
        self.entries = [{'event_id': 'a', 'public_number': 7, 'title': 'a LIVE', 'url': 'https://long.example/one'},
                        {'event_id': 'b', 'public_number': 8, 'title': 'b LIVE', 'url': 'https://long.example/two'}]
        self.plugin.service.ticket_entries = AsyncMock(return_value=(self.entries, NOW, NOW, ''))
        self.plugin.service.calendar_entries = AsyncMock(return_value=(self.entries, NOW, NOW, '', 'LIVE'))
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name)/'overview.png'
        self.image.touch()
        self.plugin.renderer = Mock()
        self.plugin.renderer.render_ticket_overview.return_value = self.image
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
        self.stopped = False
        def stop():
            self.stopped = True
        self.event.stop_event.side_effect = stop
        self.event.is_stopped.side_effect = lambda: self.stopped

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_overview_sends_before_one_nodes_container_in_group_and_private(self):
        for origin in ['qq:GroupMessage:42', 'qq:FriendMessage:43']:
            with self.subTest(origin=origin):
                self.event.unified_msg_origin = origin
                self.event.send.reset_mock()
                self.assertTrue(inspect.iscoroutinefunction(self.plugin.imasticket))
                await self.plugin.imasticket(self.event)
                image = self.event.send.await_args_list[0].args[0]
                self.assertEqual(len(image.chain), 1)  # no external Plain URL list
                self.assertEqual(image.chain[0][0], 'image')
                chain = self.event.send.await_args.args[0]
                self.assertEqual(len(chain.chain), 1)
                container = chain.chain[0]
                self.assertIsInstance(container, NodesContract)
                self.assertEqual(len(container.nodes), 2)
                self.assertEqual([n.content[0].text for n in container.nodes], ['#7 a\nSP / normal', '#8 b'])
                self.assertTrue(all(n.uin == '12345678' and n.name == 'IM@S LIVE' for n in container.nodes))
                self.assertEqual(self.event.send.await_count, 2)

    async def test_live_month_also_sends_once_after_image(self):
        await self.plugin.imaslive(self.event, '10')
        self.plugin.service.calendar_entries.assert_awaited_once_with(month=10)
        self.plugin.service.query_details.assert_awaited_once_with(self.entries, 'live')
        self.assertEqual(self.event.send.await_count, 2)
        self.assertEqual(self.event.send.await_args_list[0].args[0].chain[0][0], 'image')
        self.assertIsInstance(self.event.send.await_args.args[0].chain[0], NodesContract)

    async def test_failed_forward_keeps_image_and_short_index_without_long_urls(self):
        for failure in [RuntimeError('adapter does not support forwarding'), False]:
            self.event.send.reset_mock()
            self.event.send.side_effect = [None, failure, None]
            await self.plugin.imasticket(self.event)
            self.assertEqual(self.event.send.await_count, 3)
            fallback = self.event.send.await_args.args[0].chain[0].text
            self.assertIn('#7', fallback)
            self.assertIn('/imasticket get <编号>', fallback)
            self.assertNotIn('https://', fallback)
            self.assertNotIn('图片生成失败', fallback)

    async def test_unsupported_platform_or_missing_self_id_does_not_send_forward(self):
        for platform, self_id in [('telegram', '12345678'), ('aiocqhttp', None)]:
            self.event.send.reset_mock()
            self.event.get_platform_name.return_value = platform
            self.event.get_self_id.return_value = self_id
            await self.plugin.imasticket(self.event)
            self.assertEqual(self.event.send.await_count, 2)
            self.assertIn('活动索引', self.event.send.await_args.args[0].chain[0].text)
        self.plugin.service.query_details.assert_not_awaited()

    async def test_empty_result_only_emits_image_and_index_is_bounded(self):
        self.plugin.service.ticket_entries.return_value = ([], NOW, NOW, '')
        await self.plugin.imasticket(self.event)
        self.event.send.assert_awaited_once()
        self.assertEqual(self.event.send.await_args.args[0].chain[0][0], 'image')
        self.plugin.service.query_details.assert_not_awaited()
        index = self.plugin._query_index([{'event_id': str(i), 'public_number': i, 'title': 'x'*100} for i in range(40)])
        self.assertLess(len(index), 800)
        self.assertIn('另有 28 个活动', index)

    async def test_forward_format_error_is_separate_from_image_and_render_error_never_forwards(self):
        self.plugin.service.query_details.side_effect = ValueError('bad cache')
        await self.plugin.imasticket(self.event)
        self.assertEqual(self.event.send.await_count, 2)
        self.assertNotIn('图片生成失败', self.event.send.await_args.args[0].chain[0].text)
        self.event.send.reset_mock()
        self.plugin.service.query_details.reset_mock()
        self.plugin.renderer.render_ticket_overview.side_effect = ValueError('font')
        await self.plugin.imasticket(self.event)
        self.event.send.assert_awaited_once()
        self.assertIn('图片生成失败', self.event.send.await_args.args[0][1])
        self.plugin.service.query_details.assert_not_awaited()

    async def test_failed_image_never_sends_or_formats_forward(self):
        for failure in [RuntimeError('image failed'), False]:
            self.event.send.reset_mock()
            self.event.send.side_effect = [failure]
            await self.plugin.imasticket(self.event)
            self.event.send.assert_awaited_once()
            self.plugin.service.query_details.assert_not_awaited()

    async def test_legacy_first_yield_reproduces_missing_forward(self):
        forward = AsyncMock()
        async def legacy(event):
            event.stop_event()
            yield 'overview'
            await forward()
        await stopped_pipeline(self.event, legacy)
        self.assertTrue(self.stopped)
        forward.assert_not_awaited()

    async def test_stopped_scheduler_completes_both_queries_and_first_sync_warnings(self):
        for command, args in [('imaslive', ('10',)), ('imasticket', ())]:
            for first_sync in [False, True]:
                with self.subTest(command=command, first_sync=first_sync):
                    self.event.send.reset_mock()
                    async def ready(event):
                        if first_sync:
                            yield event.plain_result('首次同步中')
                            yield event.plain_result('官网暂不可用，使用缓存')
                    self.plugin._wait_for_first_directory = ready
                    await stopped_pipeline(self.event, getattr(self.plugin, command), *args)
                    sent = [c.args[0] for c in self.event.send.await_args_list]
                    self.assertEqual(len(sent), 4 if first_sync else 2)
                    self.assertEqual(sent[-2].chain[0][0], 'image')
                    self.assertIsInstance(sent[-1].chain[0], NodesContract)
                    if first_sync:
                        self.assertEqual([c[0] for c in sent[:2]], ['text', 'text'])

    async def test_get_next_and_admin_responses_complete_with_stopped_event(self):
        self.plugin.renderer.render_ticket.return_value = self.image
        self.plugin.service.ticket_detail = AsyncMock(return_value={
            'event': {'official_url': ROOT}, 'tickets': self.entries, 'cast': []})
        self.plugin.service.next_entry = AsyncMock(return_value={
            **self.entries[0], 'official_url': ROOT, 'cast': []})
        self.event.unified_msg_origin = 'qq:GroupMessage:42'
        cases = [('imasticket_get', (7,)), ('imaslive_next', ()),
                 ('imaslive_enable', ()), ('imaslive_disable', ()),
                 ('imasticket_enable', ()), ('imasticket_disable', ())]
        for command, args in cases:
            with self.subTest(command=command):
                self.event.send.reset_mock()
                handler = getattr(self.plugin, command)
                self.assertTrue(inspect.iscoroutinefunction(handler))
                await stopped_pipeline(self.event, handler, *args)
                self.event.send.assert_awaited_once()
                self.assertTrue(self.stopped)
        self.assertEqual(self.plugin.service.set_subscription.call_args_list,
            [unittest.mock.call('live', 'qq:GroupMessage:42', True),
             unittest.mock.call('live', 'qq:GroupMessage:42', False),
             unittest.mock.call('ticket', 'qq:GroupMessage:42', True),
             unittest.mock.call('ticket', 'qq:GroupMessage:42', False)])

    async def test_usage_errors_directly_reply_with_stopped_event(self):
        for command, args in [('imaslive', ('13',)), ('imasticket', ('wrong',)),
                              ('imaslive_next', ('cg', 'extra')), ('imasticket_get', (0,))]:
            self.event.send.reset_mock()
            await stopped_pipeline(self.event, getattr(self.plugin, command), *args)
            self.event.send.assert_awaited_once()
            self.assertIn('用法', self.event.send.await_args.args[0][1])

    async def test_native_cover_nodes_keep_each_event_image_text_pair_and_at_most_one_image(self):
        self.plugin.service.query_details.return_value = [
            {'event_id': 'b', 'text': '#8 b', 'image_path': str(self.image)},
            {'event_id': 'a', 'text': '#7 a', 'image_path': None}]
        await self.plugin.imasticket(self.event)
        nodes = self.event.send.await_args.args[0].chain[0].nodes
        self.assertEqual(nodes[0].content[0], ('image', str(self.image)))
        self.assertEqual(nodes[0].content[1].text, '#8 b')
        self.assertEqual([part.text for part in nodes[1].content], ['#7 a'])

    async def test_failed_cover_forward_retries_text_forward_then_short_index_if_needed(self):
        self.plugin.service.query_details.return_value[0]['image_path'] = str(self.image)
        for failure in [RuntimeError('image not supported'), False]:
            for retry_failed in [False, True]:
                with self.subTest(failure=failure, retry_failed=retry_failed):
                    self.event.send.reset_mock()
                    self.event.send.side_effect = [None, failure, False if retry_failed else None, None]
                    await self.plugin.imasticket(self.event)
                    self.assertEqual(self.event.send.await_count, 4 if retry_failed else 3)
                    original = self.event.send.await_args_list[1].args[0].chain[0].nodes
                    retried = self.event.send.await_args_list[2].args[0].chain[0].nodes
                    self.assertEqual(original[0].content[0][0], 'image')
                    self.assertTrue(all(len(node.content) == 1 and hasattr(node.content[0], 'text') for node in retried))
                    if retry_failed:
                        self.assertIn('活动索引', self.event.send.await_args.args[0].chain[0].text)

    async def test_missing_cover_does_not_skip_text_or_retry_text_only_forward(self):
        self.plugin.service.query_details.return_value[0]['image_path'] = str(self.image.parent/'missing.png')
        await self.plugin.imasticket(self.event)
        self.assertEqual(self.event.send.await_count, 2)
        self.assertTrue(all(len(n.content) == 1 for n in self.event.send.await_args.args[0].chain[0].nodes))
