"""Exercise lifecycle and first-query coordination without requiring a QQ account."""
import asyncio
import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

from imas_live.service import ImasLiveService
from imas_live.render import CalendarRenderer
from imas_live.models import Evidence, Performance


class _CustomFilter:
    def __init__(self, raise_error=True):
        self.raise_error = raise_error


class _CommandEntry:
    def __init__(self, name, kind, handler=None, parent=None):
        self.name, self.kind, self.handler, self.parent = name, kind, handler, parent
        self.permissions, self.enabled = [], True

    @property
    def full_name(self):
        return f"{self.parent.full_name} {self.name}" if self.parent else self.name

    def get_complete_command_names(self):
        return [self.full_name]


class _CommandGroup:
    def __init__(self, entry, registry):
        self.parent_group, self._registry = entry, registry
        self.custom_filters = []

    def command(self, name):
        def decorator(handler):
            entry = _CommandEntry(name, "sub_command", handler, self.parent_group)
            handler._astrbot_entry = entry
            self._registry.append(entry)
            return handler
        return decorator

    def custom_filter(self, custom_filter, raise_error=True):
        def decorator(target):
            self.custom_filters.append(custom_filter(raise_error))
            return target
        return decorator


class _FilterHarness:
    """Small registration/dispatch harness matching AstrBot's command semantics."""
    CustomFilter = _CustomFilter
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    def __init__(self):
        self.registry = []

    def command(self, name):
        def decorator(handler):
            entry = _CommandEntry(name, "command", handler)
            handler._astrbot_entry = entry
            self.registry.append(entry)
            return handler
        return decorator

    def command_group(self, name):
        def decorator(_handler):
            entry = _CommandEntry(name, "group")
            self.registry.append(entry)
            return _CommandGroup(entry, self.registry)
        return decorator

    def permission_type(self, permission):
        def decorator(handler):
            handler._astrbot_entry.permissions.append(permission)
            return handler
        return decorator

    async def dispatch(self, plugin, text, event, is_admin=False):
        """Choose the longest enabled native command and bind its arguments."""
        parts = text.split()
        matches = [entry for entry in self.registry if entry.kind != "group" and entry.enabled
                   and parts[:len(entry.full_name.split())] == entry.full_name.split()]
        if not matches:
            return "unmatched"
        entry = max(matches, key=lambda item: len(item.full_name.split()))
        if self.PermissionType.ADMIN in entry.permissions and not is_admin:
            return "permission_denied"
        values = parts[len(entry.full_name.split()):]
        signature = inspect.signature(entry.handler, eval_str=True)
        params = list(signature.parameters.values())[2:]
        bound = []
        for index, parameter in enumerate(params):
            if index < len(values):
                value = values[index]
                bound.append(int(value) if parameter.annotation is int else value)
            elif parameter.default is not inspect.Parameter.empty:
                bound.append(parameter.default)
            else:
                return "parameter_error"
        return [result async for result in entry.handler(plugin, event, *bound)]


def plugin_module():
    names = ['astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.star',
             'astrbot.core', 'astrbot.core.utils', 'astrbot.core.utils.astrbot_path', '_live_test']
    modules = {name: types.ModuleType(name) for name in names}
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api'].logger = Mock()
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.event'].MessageChain = Mock()
    command_filter = _FilterHarness()
    modules['astrbot.api.event'].filter = command_filter
    modules['astrbot.api.star'].Context = object
    modules['astrbot.api.star'].Star = type('StarStub', (), {'__init__': lambda self, context, config=None: None})
    modules['astrbot.core.utils.astrbot_path'].get_astrbot_plugin_data_path = lambda: '.'
    root = Path(__file__).parents[1]
    modules['_live_test'].__path__ = [str(root)]
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('_live_test.main', root / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module._test_filter = command_filter
    return module


def plugin_class():
    return plugin_module().ImasLivePlugin


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_imaslive_still_renders_cached_calendar_after_source_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            module = plugin_module()
            with patch.object(module, 'get_astrbot_plugin_data_path', return_value=directory):
                plugin = module.ImasLivePlugin(Mock(), {'enabled': True})
            source = 'https://idolmaster-official.jp/live_event/cached/'
            plugin.service.db.upsert_event({'id': 'cached', 'title': 'CACHED LIVE', 'url': source,
                                            'brands': ['SIDEM'], 'event_display': None, 'venue': 'Hall'})
            plugin.service.db.save_parsed('cached', source, 'v1', 'test', [], [
                Performance('day', '2026-10-10', '开演 18:00 JST', 'Hall',
                            evidence=Evidence(source, 'official', 'test'))], [], [])
            source_unavailable = module.ImasLiveService.sync.__globals__['SourceUnavailable']
            plugin.service._refresh_article = AsyncMock(side_effect=source_unavailable('专题未能解析'))
            with self.assertLogs('_live_test.imas_live.service', level='WARNING'):
                result = await plugin.service.sync(full_directory=False)
            self.assertEqual(result['failed_pages'], 1)
            event = Mock()
            event.stop_event.return_value = None
            event.plain_result.side_effect = lambda text: ('text', text)
            event.chain_result.side_effect = lambda chain: ('chain', chain)
            async def idle():
                await asyncio.Event().wait()
            plugin._sync_loop, plugin._reminder_loop = idle, idle
            responses = await asyncio.wait_for(module._test_filter.dispatch(plugin, 'imaslive 10', event), timeout=3)
            self.assertEqual([item[0] for item in responses], ['text', 'chain'])
            await plugin.terminate()

    async def test_first_directory_failure_returns_status_without_long_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            module = plugin_module()
            with patch.object(module, 'get_astrbot_plugin_data_path', return_value=directory):
                plugin = module.ImasLivePlugin(Mock(), {'enabled': True})
            source_unavailable = module.ImasLiveService.sync.__globals__['SourceUnavailable']
            plugin.service.client.live_articles = AsyncMock(side_effect=source_unavailable('官网不可用'))
            async def sync_once():
                await plugin.service.sync(full_directory=True)
                await asyncio.Event().wait()
            async def idle():
                await asyncio.Event().wait()
            plugin._sync_loop, plugin._reminder_loop = sync_once, idle
            event = Mock()
            event.stop_event.return_value = None
            event.plain_result.side_effect = lambda text: ('text', text)
            event.chain_result.side_effect = lambda chain: ('chain', chain)
            responses = await asyncio.wait_for(module._test_filter.dispatch(plugin, 'imaslive', event), timeout=3)
            self.assertEqual([item[0] for item in responses], ['text', 'text', 'chain'])
            self.assertIn('尚未成功', responses[1][1])
            await plugin.terminate()

    async def test_live_loads_without_flight_modules_or_dependency_and_preserves_old_database(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'astrbot_plugin_imas_live'
            data.mkdir()
            legacy = data / 'imas_flight.sqlite3'
            legacy.write_bytes(b'old-flight-data')
            with patch.dict(sys.modules, {'_live_test.imas_live.flight': None,
                                          '_live_test.imas_live.flight_render': None, 'aiohttp': None}):
                module = plugin_module()
                with patch.object(module, 'get_astrbot_plugin_data_path', return_value=directory):
                    context = Mock()
                    plugin = module.ImasLivePlugin(context, {'enabled': False})
            self.assertFalse(hasattr(plugin, 'flight_planner'))
            self.assertFalse(hasattr(plugin, '_flight_task'))
            context.register_web_api.assert_not_called()
            self.assertEqual(legacy.read_bytes(), b'old-flight-data')
            await plugin.service.close()

    async def test_month_argument_accepts_one_integer_only(self):
        parser = plugin_module().parse_live_month
        self.assertIsNone(parser(""))
        self.assertEqual(parser("01"), 1)
        self.assertEqual(parser("12"), 12)
        for invalid in ("0", "13", "-1", "1.5", "abc", "1 2"):
            with self.assertRaises(ValueError):
                parser(invalid)

    async def test_ticket_get_receives_action_and_number_as_separate_arguments(self):
        """AstrBot supplies command words as separate positional arguments."""
        module = plugin_module()
        cls = module.ImasLivePlugin
        plugin = cls.__new__(cls)
        plugin.config = {"display_timezone": "Asia/Shanghai"}
        plugin.service = Mock()
        plugin.service.ticket_detail = AsyncMock(return_value={
            "event": {"official_url": "https://example.test/event"},
            "tickets": [{"url": "https://example.test/ticket"}],
            "cast": [],
        })
        plugin.renderer = Mock()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "ticket.png"
            image.touch()
            plugin.renderer.render_ticket.return_value = image

            async def already_ready(_event):
                if False:
                    yield None

            plugin._wait_for_first_directory = already_ready
            event = Mock()
            event.chain_result.side_effect = lambda chain: ("chain", chain)
            responses = await module._test_filter.dispatch(plugin, "imasticket get 2", event)

        plugin.service.ticket_detail.assert_awaited_once_with(2)
        self.assertEqual([response[0] for response in responses], ["chain"])

    async def test_native_commands_have_no_duplicate_roots_and_keep_admin_filters(self):
        module = plugin_module()
        cls = module.ImasLivePlugin
        entries = module._test_filter.registry
        commands = {entry.full_name: entry for entry in entries}
        self.assertEqual(len(commands), len(entries), "AstrBot must not receive duplicate command names")
        self.assertEqual(set(commands), {
            "imaslive", "imaslive next", "imaslive enable", "imaslive disable",
            "imasticket", "imasticket get", "imasticket enable", "imasticket disable",
        })
        for name in ("imaslive enable", "imaslive disable", "imasticket enable", "imasticket disable"):
            self.assertEqual(commands[name].permissions, ["admin"])
        self.assertEqual(commands["imaslive next"].permissions, [])
        self.assertEqual(commands["imasticket get"].permissions, [])
        self.assertFalse(hasattr(cls, "_can_manage_subscription"))

        plugin = cls.__new__(cls)
        plugin.config = {}
        plugin.service = Mock()
        event = Mock()
        event.unified_msg_origin = "test:GroupMessage:42"
        event.plain_result.side_effect = lambda text: ("text", text)
        self.assertEqual(await module._test_filter.dispatch(plugin, "imaslive enable", event, is_admin=False), "permission_denied")
        plugin.service.set_subscription.assert_not_called()

        allowed = await module._test_filter.dispatch(plugin, "imaslive enable", event, is_admin=True)
        self.assertEqual(allowed[0][0], "text")
        plugin.service.set_subscription.assert_called_once_with("live", "test:GroupMessage:42", True)

        # Disabling the native command leaves no parameter-dispatch write path.
        plugin.service.set_subscription.reset_mock()
        commands["imaslive enable"].enabled = False
        self.assertEqual(await module._test_filter.dispatch(plugin, "imaslive enable", event, is_admin=True), [])
        plugin.service.set_subscription.assert_not_called()

        # A WebUI-style rename leaves no old write-capable path.
        commands["imaslive enable"].enabled = True
        commands["imaslive enable"].name = "imaslive start"
        self.assertEqual(await module._test_filter.dispatch(plugin, "imaslive enable", event, is_admin=True), [])
        plugin.service.set_subscription.assert_not_called()
        await module._test_filter.dispatch(plugin, "imaslive start", event, is_admin=True)
        plugin.service.set_subscription.assert_called_once_with("live", "test:GroupMessage:42", True)

    async def test_reload_starts_once_and_first_query_waits_for_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            module = plugin_module()
            cls = module.ImasLivePlugin
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
                plugin.service.directory_attempted.set()
                await asyncio.Event().wait()
            async def reminders():
                await asyncio.Event().wait()
            plugin._sync_loop, plugin._reminder_loop = sync, reminders
            event = Mock()
            event.plain_result.side_effect = lambda text: ('text', text)
            event.image_result.side_effect = lambda path: ('image', path)
            event.chain_result.side_effect = lambda chain: ('chain', chain)
            responses = await module._test_filter.dispatch(plugin, "imaslive", event)
            self.assertEqual([result[0] for result in responses], ['text', 'chain'])
            self.assertTrue(plugin.service.directory_ready.is_set())
            running = (plugin._sync_task, plugin._reminder_task)
            await plugin.initialize()
            self.assertEqual(running, (plugin._sync_task, plugin._reminder_task))
            await plugin.terminate()
            self.assertTrue(all(task.cancelled() for task in running))

    async def test_reminder_kinds_are_isolated_and_claimed_rows_are_completed(self):
        module = plugin_module()
        plugin = module.ImasLivePlugin.__new__(module.ImasLivePlugin)
        plugin.config = {}
        ticket = {"delivery_key": "ticket", "umo": "test:GroupMessage:42", "url": "https://example.test",
                  "title": "TEST LIVE", "subtitle": "提前1小时", "brands": ["SIDEM"], "remaining_minutes": 60}
        fresh = {"delivery_key": "new", "umo": "test:GroupMessage:42", "url": "https://example.test",
                 "title": "TEST LIVE", "subtitle": "新抽选", "brands": ["SIDEM"], "remaining_minutes": 0}
        plugin.service = Mock()
        plugin.service.refresh_due_ticket_sources = AsyncMock(return_value={"refreshed": 0, "failed": 0})
        plugin.service.claim_due_reminders = AsyncMock(return_value=[ticket])
        plugin.service.claim_due_live_reminders = AsyncMock(side_effect=RuntimeError("live broken"))
        plugin.service.claim_new_ticket_announcements = AsyncMock(return_value=[fresh])
        plugin._deliver_reminder_rows = AsyncMock()
        await plugin._run_reminder_cycle()
        self.assertEqual(plugin._deliver_reminder_rows.await_args_list,
                         [call("ticket", [ticket]), call("ticket_new", [fresh])])

        del plugin._deliver_reminder_rows
        plugin.context = Mock()
        plugin.context.send_message = AsyncMock(return_value=False)
        class Chain:
            def message(self, _text): return self
            def file_image(self, _path): return self
        module.MessageChain = Chain
        with tempfile.TemporaryDirectory() as directory:
            plugin.renderer = CalendarRenderer(Path(directory))
            plugin.service.finish_reminders = AsyncMock()
            await plugin._deliver_reminder_rows("ticket", [ticket])
        plugin.context.send_message.assert_awaited_once()
        plugin.service.finish_reminders.assert_awaited_once_with([ticket], False)
