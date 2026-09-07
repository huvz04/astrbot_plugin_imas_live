import json
import unittest
from pathlib import Path

from imas_live.cms import OfficialCmsClient


class CmsFixtureTests(unittest.TestCase):
    def test_live_directory_item_is_an_event_candidate_not_a_performance_count(self):
        fixture = Path(__file__).parents[2] / "research" / "imas-live" / "sources" / "live.json"
        item = json.loads(fixture.read_text(encoding="utf-8"))["data"]["article_list"][0]
        article = OfficialCmsClient._article(item)
        self.assertTrue(article.cms_id)
        self.assertTrue(article.title)
        self.assertIsInstance(article.brands, list)
