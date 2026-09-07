from pathlib import Path
import unittest

from imas_live.parsing import parse_shiny_information, parse_ticket_page, parse_venue


SOURCES = Path(__file__).parents[2] / "research" / "imas-live" / "sources"


def fixture(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


class ParsingTests(unittest.TestCase):
    def test_venue_uses_only_an_explicit_official_field(self):
        html = "<dl><dt>開催場所</dt><dd>国立代々木競技場 第一体育館</dd></dl>"
        self.assertEqual(parse_venue(html), "国立代々木競技場 第一体育館")
        heading_html = "<h2><span>開催場所</span></h2><p>国立代々木競技場 第一体育館 <a href='https://example.test'>https://example.test</a></p>"
        self.assertEqual(parse_venue(heading_html), "国立代々木競技場 第一体育館")
        self.assertIsNone(parse_venue("<p>物販会場は後日発表</p>"))

    def test_gakuen_final_keeps_rounds_and_jst_deadline(self):
        result = parse_ticket_page(fixture("gakuen_final_ticket.html"), "https://idolmaster-official.jp/live_event/gkmas_livetour_shirube/ticket/final.php")
        round_ = next(item for item in result.ticket_rounds if "一般会員2次先行" in item.name)
        self.assertEqual(round_.sale_method, "lottery")
        self.assertEqual(round_.ticket_scope, "onsite")
        self.assertEqual(round_.application_start, "2026-09-05T12:00+09:00")
        self.assertEqual(round_.application_end, "2026-09-23T23:59+09:00")
        self.assertEqual(round_.result_at, "2026-10-10T13:00+09:00")
        self.assertEqual(round_.payment_end, "2026-10-14T23:59+09:00")
        self.assertTrue(round_.url and round_.url.startswith("https://asobiticket2.asobistore.jp/receptions/"))


    def test_sidem_keeps_first_come_and_resale_distinct(self):
        result = parse_ticket_page(fixture("sidem_ticket.html"), "https://idolmaster-official.jp/live_event/sidem11th/ticket/")
        methods = {item.sale_method for item in result.ticket_rounds}
        self.assertIn("first_come", methods)
        self.assertIn("resale", methods)
        self.assertTrue(any(item.sale_method == "first_come" and item.ticket_scope == "onsite" for item in result.ticket_rounds))
        self.assertTrue(any(item.sale_method == "resale" and item.ticket_scope == "onsite" for item in result.ticket_rounds))


    def test_shiny_goods_ticket_does_not_become_onsite_ticket(self):
        result = parse_ticket_page(fixture("shiny_single_page.html"), "https://idolmaster-official.jp/live_event/283production_msp/")
        self.assertTrue(any(item.ticket_scope == "goods" for item in result.ticket_rounds))


    def test_shiny_cast_is_announced_and_day_specific(self):
        result = parse_shiny_information(fixture("shiny_single_page.html"), "https://idolmaster-official.jp/live_event/283production_msp/")
        self.assertTrue({item.date for item in result.performances} >= {"2026-09-26", "2026-09-27"})
        self.assertEqual(len(result.cast), 56)
        self.assertTrue(all(item.status == "announced" for item in result.cast))
