"""Exercise lifecycle and first-query coordination without requiring a QQ account."""
import asyncio
import importlib.util
import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from imas_live.service import ImasLiveService


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
    modules['astrbot.api.star'].Star = object
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

    async def test_native_groups_expose_admin_children_without_a_plugin_admin_bypass(self):
        module = plugin_module()
        cls = module.ImasLivePlugin
        entries = module._test_filter.registry
        groups = {entry.name: entry for entry in entries if entry.kind == "group"}
        self.assertEqual(set(groups), {"imaslive", "imasticket"})
        children = {entry.full_name: entry for entry in entries if entry.kind == "sub_command"}
        self.assertEqual(set(children), {"imaslive next", "imaslive enable", "imaslive disable",
                                         "imasticket get", "imasticket enable", "imasticket disable"})
        group_event = Mock()
        group_event.get_message_str.return_value = "imaslive"
        self.assertFalse(cls.imaslive_group.custom_filters[0].filter(group_event, {}))
        group_event.get_message_str.return_value = "imaslive next gk"
        self.assertTrue(cls.imaslive_group.custom_filters[0].filter(group_event, {}))
        for name in ("imaslive enable", "imaslive disable", "imasticket enable", "imasticket disable"):
            self.assertEqual(children[name].permissions, ["admin"])
        self.assertEqual(children["imaslive next"].permissions, [])
        self.assertEqual(children["imasticket get"].permissions, [])
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

        # Disabling the native child leaves no parameter-dispatch write path.
        plugin.service.set_subscription.reset_mock()
        children["imaslive enable"].enabled = False
        self.assertEqual(await module._test_filter.dispatch(plugin, "imaslive enable", event, is_admin=True), [])
        plugin.service.set_subscription.assert_not_called()

        # A WebUI-style child rename leaves no old write-capable path.
        children["imaslive enable"].enabled = True
        children["imaslive enable"].name = "start"
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
