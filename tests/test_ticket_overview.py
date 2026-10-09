"""Event-level summary cannot discard or mutate cached per-round details."""
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw
from imas_live.render import CalendarRenderer

NOW = datetime(2026, 10, 9, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


def row(state='open', event='a', **extra):
    return {'event_id': event, 'public_number': 7, 'kind': 'ticket',
        'title': event+' LIVE', 'brands': ['SIDEM'], 'ticket_status': state,
        'status_label': state, 'subtitle': 'Premium / SP席\n申请时间 2026/10/01 12:00',
        'url': 'https://asobiticket2.asobistore.jp/receptions/'+state,
        'performance_dates': ['2026-12-19', '2026-12-20'],
        'performance_venues': ['Official Hall'],
        'live_start': '2026-12-19T12:00:00+08:00', **extra}


class TicketOverviewTests(unittest.TestCase):
    def test_all_states_count_once_in_one_event_without_raw_fields_or_mutation(self):
        rows = [row(s) for s in ['open', 'urgent', 'upcoming', 'ended', 'stale', 'unknown']]
        rows.append(row('ended', status_label='已结束 · 待核验'))
        before = copy.deepcopy(rows)
        cards = CalendarRenderer.ticket_overview_entries(rows)
        self.assertEqual(rows, before)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card['status_counts'], dict(open=1, urgent=1, upcoming=1, ended=2, unknown=2))
        self.assertEqual(sum(card['status_counts'].values()), len(rows))
        self.assertEqual(card['ended_pending'], 1)
        self.assertEqual(card['ticket_status'], 'urgent')
        for expected in ['2026/12/19～2026/12/20', 'Official Hall', '进行 1',
                         '即将截止 1', '未开始 1', '已结束 2（1 待核验）', '待核验 2']:
            self.assertIn(expected, card['subtitle'])
        for omitted in ['Premium', 'SP席', '申请时间', '12:00', 'https://']:
            self.assertNotIn(omitted, card['subtitle'])
        self.assertNotIn('url', card)

    def test_counts_only_nonzero_and_stale_is_not_green_open(self):
        stale, = CalendarRenderer.ticket_overview_entries([row('stale')])
        self.assertEqual(stale['ticket_status'], 'unknown')
        self.assertIn('待核验 1', stale['subtitle'])
        self.assertNotIn('进行 0', stale['subtitle'])
        mixed, = CalendarRenderer.ticket_overview_entries([row('stale'), row('open')])
        self.assertIn('进行 1 · 待核验 1', mixed['subtitle'])

    def test_chronological_events_merge_all_dates_venues_not_adjacent_rounds(self):
        rows = [row('open', 'late'), row('ended', 'early', live_start='2026-11-01T12:00:00+08:00'),
                row('upcoming', 'late', performance_dates=['2026-12-21'], performance_venues=['Other Hall'])]
        cards = CalendarRenderer.ticket_overview_entries(rows)
        self.assertEqual([c['event_id'] for c in cards], ['early', 'late'])
        self.assertIn('2026/12/19～2026/12/21', cards[1]['subtitle'])
        self.assertIn('Official Hall／Other Hall', cards[1]['subtitle'])
        self.assertEqual(cards[1]['status_counts']['upcoming'], 1)

    def test_event_scope_overrides_reception_scope_without_changing_raw_detail(self):
        bound = row(performance_dates=['2027-01-23', '2027-01-24'], performance_venues=['Makuhari'],
            event_performance_dates=['2026-12-26', '2026-12-27', '2027-01-23', '2027-01-24'],
            event_performance_venues=['Nagano', 'Makuhari'], live_start='2026-12-26T12:00:00+08:00')
        other = row(event='other', live_start='2027-01-11T12:00:00+08:00')
        overview = CalendarRenderer.ticket_overview_entries([other, bound])
        self.assertEqual(overview[0]['event_id'], 'a')
        self.assertIn('2026/12/26～2027/01/24', overview[0]['subtitle'])
        self.assertIn('Nagano／Makuhari', overview[0]['subtitle'])
        self.assertEqual(bound['performance_venues'], ['Makuhari'])
        self.assertIn('1 项', overview[0]['status_label'])

    def test_renderer_draws_one_card_per_event_get_renderer_keeps_all_rounds(self):
        rows = [row(s) for s in ['open', 'urgent', 'upcoming', 'ended', 'stale']]
        with tempfile.TemporaryDirectory() as temp:
            renderer = CalendarRenderer(Path(temp))
            with patch.object(renderer, '_draw_card', wraps=renderer._draw_card) as draw_card, \
                 patch.object(ImageDraw.ImageDraw, 'text', autospec=True, wraps=ImageDraw.ImageDraw.text) as text:
                overview = renderer.render_ticket_overview(rows, NOW.date(), NOW.date(), NOW,
                    'no footer diagnostics', NOW)
                self.assertEqual(draw_card.call_count, 1)
                values = [c.args[2] for c in text.call_args_list]
                self.assertEqual(sum('update time' in t for t in values), 1)
                self.assertNotIn('no footer diagnostics', '\n'.join(values))
            with patch.object(renderer, '_draw_card', wraps=renderer._draw_card) as draw_card:
                detail = renderer.render_ticket(rows, NOW.date(), NOW.date(), NOW, '', NOW)
                self.assertEqual(draw_card.call_count, 5)
            with Image.open(overview) as small, Image.open(detail) as large:
                self.assertLess(small.height, large.height)

    def test_empty_state_and_source_time_footer(self):
        with tempfile.TemporaryDirectory() as temp:
            renderer = CalendarRenderer(Path(temp))
            with patch.object(ImageDraw.ImageDraw, 'text', autospec=True) as text:
                renderer.render_ticket_overview([], NOW.date(), NOW.date(), NOW, '尚未同步')
                values = [c.args[2] for c in text.call_args_list]
                self.assertTrue(any('数据尚未同步' in t for t in values))
                self.assertIn('update time --', values)
