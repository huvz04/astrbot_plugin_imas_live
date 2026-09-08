"""Exercise lifecycle and first-query coordination without requiring a QQ account."""
import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from imas_live.service import ImasLiveService


def plugin_module():
    names = ['astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
             'astrbot.core', 'astrbot.core.utils', 'astrbot.core.utils.astrbot_path', '_live_test']
    modules = {name: types.ModuleType(name) for name in names}
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api'].logger = Mock()
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.event'].MessageChain = Mock()
    modules['astrbot.api.event'].filter = types.SimpleNamespace(command=lambda name: lambda f: f)
    modules['astrbot.api.star'].Context = object
    modules['astrbot.api.star'].Star = object
    modules['astrbot.core.utils.astrbot_path'].get_astrbot_plugin_data_path = lambda: '.'
    root = Path(__file__).parents[1]
    modules['_live_test'].__path__ = [str(root)]
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('_live_test.main', root / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def plugin_class():
    return plugin_module().ImasLivePlugin


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_month_argument_accepts_one_integer_only(self):
        parser = plugin_module().parse_live_month
        self.assertIsNone(parser(""))
        self.assertEqual(parser("01"), 1)
        self.assertEqual(parser("12"), 12)
        for invalid in ("0", "13", "-1", "1.5", "abc", "1 2"):
            with self.assertRaises(ValueError):
                parser(invalid)

    async def test_reload_starts_once_and_first_query_waits_for_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            cls = plugin_class()
            plugin = cls.__new__(cls)
            plugin.config = {}
            plugin.service = ImasLiveService(Path(directory))
            plugin.renderer = Mock()
            rendered = Path(directory) / 'calendar.png'; rendered.touch()
            plugin.renderer.render_calendar.return_value = [rendered]
            plugin._sync_task = plugin._reminder_task = None
            async def sync():
                await asyncio.sleep(0)
                plugin.service.db.set_meta('last_directory_sync', '2026-09-08T00:00:00+00:00')
                plugin.service.directory_ready.set()
                await asyncio.Event().wait()
            async def reminders():
                await asyncio.Event().wait()
            plugin._sync_loop, plugin._reminder_loop = sync, reminders
            event = Mock()
            event.plain_result.side_effect = lambda text: ('text', text)
            event.image_result.side_effect = lambda path: ('image', path)
            event.chain_result.side_effect = lambda chain: ('chain', chain)
            responses = [result async for result in plugin.imaslive(event)]
            self.assertEqual([result[0] for result in responses], ['text', 'chain'])
            self.assertTrue(plugin.service.directory_ready.is_set())
            running = (plugin._sync_task, plugin._reminder_task)
            await plugin.initialize()
            self.assertEqual(running, (plugin._sync_task, plugin._reminder_task))
            await plugin.terminate()
            self.assertTrue(all(task.cancelled() for task in running))
