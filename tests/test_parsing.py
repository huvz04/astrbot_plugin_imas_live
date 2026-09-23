from pathlib import Path
import unittest

from imas_live.parsing import official_roster_image_urls, parse_shiny_information, parse_ticket_page, parse_venue


SOURCES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (SOURCES / name).read_text(encoding="utf-8")


class ParsingTests(unittest.TestCase):
    def test_million_roster_images_exclude_day_title_banners(self):
        html = '''<section><h2><span>出演者</span></h2><ul>
          <li><img src="../images/information/bnr_day1.webp"></li>
          <li><img src="../images/information/bnr_day2.webp"></li>
          <li><img src="../images/information/bnr_day1_title01.webp"></li>
        </ul></section>'''
        urls = official_roster_image_urls(html, "https://idolmaster-official.jp/live_event/million14th/information/")
        self.assertEqual(urls, [
            "https://idolmaster-official.jp/live_event/million14th/images/information/bnr_day1.webp",
            "https://idolmaster-official.jp/live_event/million14th/images/information/bnr_day2.webp",
        ])
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

    def test_iuoafa_ticket_details_are_aggregated_inside_one_reception(self):
        result = parse_ticket_page(fixture("iuoafa.html"), "https://idolmaster-official.jp/live_event/IUOAFA/")
        self.assertEqual(len(result.ticket_rounds), 1)
        round_ = result.ticket_rounds[0]
        self.assertEqual(round_.name, "アソビストアプレミアム会員先行")
        self.assertEqual(round_.application_start, "2026-09-06T12:00+09:00")
        self.assertEqual(round_.application_end, "2026-10-28T23:59+09:00")
        self.assertIn("12か月会員", round_.eligibility or "")

    def test_nested_ticket_details_keep_each_reception_fields_separate(self):
        html = '''<details><summary>チケット</summary>
          <details><summary>会員1次先行</summary><dl><dt>受付期間</dt><dd>2026年9月1日12:00～9月10日23:59</dd>
          <dt>受付URL</dt><dd><a href="https://asobiticket2.asobistore.jp/receptions/first">申込</a></dd></dl></details>
          <details><summary>会員2次先行</summary><dl><dt>受付期間</dt><dd>2026年9月11日12:00～9月20日23:59</dd>
          <dt>受付URL</dt><dd><a href="https://asobiticket2.asobistore.jp/receptions/second">申込</a></dd></dl></details>
        </details>'''
        rounds = parse_ticket_page(html, "https://idolmaster-official.jp/live_event/test/").ticket_rounds
        self.assertEqual([(r.name, r.application_end, r.url.rsplit('/', 1)[-1]) for r in rounds], [
            ("会員1次先行", "2026-09-10T23:59+09:00", "first"),
            ("会員2次先行", "2026-09-20T23:59+09:00", "second"),
        ])


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
