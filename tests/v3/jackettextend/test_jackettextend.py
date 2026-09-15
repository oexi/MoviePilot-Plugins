import asyncio
import codecs
import copy
import inspect
import json
import threading
import types
import unittest
import xml.dom.minidom
from contextlib import contextmanager
from pathlib import Path

from importlib import import_module

import requests
from urllib3.exceptions import ReadTimeoutError


ROOT = Path(__file__).resolve().parents[3]


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _MediaType:
    MUSIC = types.SimpleNamespace(value="音乐", name="MUSIC")


class _MediaSource:
    IMDb = "imdb"


class _TorrentInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StringUtils:
    @staticmethod
    def clear(text, replace_word="", allow_space=False):
        return text

    @staticmethod
    def unify_datetime_str(value):
        return value

    @staticmethod
    def get_url_domain(value):
        return value


class _Response:
    status_code = 200
    headers = {"Content-Type": "application/xml"}
    text = ""
    wire_content = None
    closed = False

    def iter_content(self, chunk_size=1):
        body = self.wire_content
        if body is None:
            body = self.text.encode("utf-8")
        yield body

    def close(self):
        self.closed = True


class _GuardedResponse:
    """Streaming double that fails if production reads buffered properties."""

    def __init__(self, chunks, status_code=200, headers=None, error=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/xml"}
        self._chunks = tuple(chunks)
        self._error = error
        self.iterated_chunks = 0
        self.closed = False

    @property
    def content(self):
        raise AssertionError("streaming code must not read response.content")

    @property
    def text(self):
        raise AssertionError("streaming code must not read response.text")

    def iter_content(self, chunk_size=1):
        for chunk in self._chunks:
            self.iterated_chunks += 1
            yield chunk
        if self._error is not None:
            raise self._error

    def close(self):
        self.closed = True


@contextmanager
def _stream_response(response):
    try:
        yield response
    finally:
        if response is not None:
            response.close()


def _real_xml_response(content):
    """Build a real requests.Response around an exact wire payload."""
    response = requests.Response()
    response.status_code = 200
    response.headers = {"Content-Type": "application/xml"}
    response._content = content
    response._content_consumed = True
    return response


def _real_stream_response(stream_factory, headers):
    """Build a real response whose raw stream is controlled by a test."""
    close_calls = []
    response = requests.Response()
    response.status_code = 200
    response.headers = headers
    response.raw = types.SimpleNamespace(
        stream=lambda *_args, **_kwargs: stream_factory(),
        close=lambda: close_calls.append(True),
    )
    response._content_consumed = False
    return response, close_calls


class _RequestUtils:
    response = _Response()
    timeouts = []

    def __init__(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))

    def get_res(self, *args, **kwargs):
        return self.response

    def get_stream(self, *args, **kwargs):
        return _stream_response(self.get_res(*args, **kwargs))


@contextmanager
def loaded_module():
    module = import_module("app.plugins.jackettextend")
    patched = {
        "MediaType": _MediaType,
        "MediaSource": _MediaSource,
        "TorrentInfo": _TorrentInfo,
        "StringUtils": _StringUtils,
        "RequestUtils": _RequestUtils,
        "logger": _Logger(),
        "settings": types.SimpleNamespace(
            PROXY=None,
            TZ="UTC",
            USER_AGENT="test",
        ),
        # Keep this test-local so a test replacing ``to_thread`` cannot
        # mutate the process-wide asyncio module used by later tests.
        "asyncio": types.SimpleNamespace(to_thread=asyncio.to_thread),
    }
    previous = {name: getattr(module, name) for name in patched}
    previous_response = _RequestUtils.response
    previous_timeouts = list(_RequestUtils.timeouts)
    _RequestUtils.timeouts.clear()
    try:
        for name, value in patched.items():
            setattr(module, name, value)
        yield module
    finally:
        _uninstall_loaded_plugin_bridge(module)
        for name, value in previous.items():
            setattr(module, name, value)
        _RequestUtils.response = previous_response
        _RequestUtils.timeouts[:] = previous_timeouts


def _uninstall_loaded_plugin_bridge(module) -> None:
    """回收该生产模块在宿主 ChainBase 上留下的桥接 owner。"""
    try:
        from app.chain import ChainBase

        compat = module._host_compat
        state = getattr(ChainBase, compat._STATE_ATTR, None)
        if not isinstance(state, dict):
            return
        owners = state.get("owners")
        if isinstance(owners, dict):
            for key, record in list(owners.items()):
                owner = compat._owner_from_record(record)
                if owner is not None and owner.__class__.__module__ == module.__name__:
                    compat.uninstall(owner, owner_key=key)
            return
        owner = compat._owner_from_state(state)
        if owner is not None and owner.__class__.__module__ == module.__name__:
            compat.uninstall(owner)
    except Exception:  # noqa: BLE001  测试收尾不得掩盖原断言
        return


@contextmanager
def isolated_ui_module():
    """通过生产命名空间加载纯 UI 构造器。"""
    yield import_module("app.plugins.jackettextend._ui")


@contextmanager
def site_oper_modules(site_oper, eventmanager=None, event_type=None):
    """在真实宿主模块边界替换 Oper 与事件端口，并精确恢复属性。"""
    from app.db.oper import site as site_module
    from app.sdk import events as events_module
    from app.schemas import types as schema_types

    previous_site_oper = site_module.SiteOper
    previous_eventmanager = events_module.eventmanager
    previous_event_type = schema_types.EventType
    site_module.SiteOper = site_oper
    if eventmanager is not None:
        events_module.eventmanager = eventmanager
        schema_types.EventType = event_type
    try:
        yield
    finally:
        site_module.SiteOper = previous_site_oper
        events_module.eventmanager = previous_eventmanager
        schema_types.EventType = previous_event_type


class JackettV3ContractTest(unittest.TestCase):
    @staticmethod
    def _page_texts(page):
        values = []

        def walk(node):
            if isinstance(node, dict):
                if "text" in node:
                    values.append(node["text"])
                for child in node.get("content", []):
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(page)
        return values

    def test_metadata_uses_lowercase_author(self):
        manifest = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["JackettExtend"]["author"], "oexi")
        with loaded_module() as module:
            self.assertEqual(module.JackettExtend.plugin_author, "oexi")

    def test_host_normalization_matches_safe_http_base_contract(self):
        with loaded_module() as module:
            normalize = module.JackettExtend._normalize_host
            cases = {
                None: "",
                "": "",
                " jackett:9117 ": "http://jackett:9117",
                "HTTP://jackett:9117/": "http://jackett:9117",
                "https://jackett:9117/base/path///": "https://jackett:9117/base/path",
                "ftp://jackett:9117": "",
                "http://user:pass@jackett:9117": "",
                "http://jackett:abc": "",
                "http://jackett:9117?foo=bar": "",
                "http://jackett:9117/#fragment": "",
            }
            for raw, expected in cases.items():
                with self.subTest(raw=raw):
                    self.assertEqual(normalize(raw), expected)

    def test_async_module_uses_current_to_thread_provider(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            calls = []
            thread_calls = []

            def search(**kwargs):
                calls.append(kwargs)
                return ["ok"]

            async def fake_to_thread(func, *args, **kwargs):
                thread_calls.append(func)
                return func(*args, **kwargs)

            module.asyncio.to_thread = fake_to_thread
            plugin.search_torrents = search
            result = asyncio.run(plugin.async_search_torrents(site={"domain": "jackett_extend.nyaa"}, keyword="x"))

            self.assertEqual(result, ["ok"])
            self.assertEqual(thread_calls, [search])
            self.assertEqual(calls[0]["site"]["domain"], "jackett_extend.nyaa")
            self.assertTrue(inspect.iscoroutinefunction(module.JackettExtend.async_search_torrents))
            self.assertTrue(inspect.iscoroutinefunction(module.JackettExtend.get_module(plugin)["async_search_torrents"]))

    def test_get_service_exposes_shared_scheduler_contract(self):
        with loaded_module() as module:
            class FakeCronTrigger:
                def __init__(self, expression, timezone=None):
                    self.expression = expression
                    self.timezone = timezone

                @classmethod
                def from_crontab(cls, expression, timezone=None):
                    return cls(expression, timezone=timezone)

            module.CronTrigger = FakeCronTrigger
            plugin = object.__new__(module.JackettExtend)
            plugin._enabled = True
            plugin._cron = "*/15 * * * *"
            plugin._sync_generation = 4
            calls = []

            def sync_all(generation=None):
                calls.append(generation)

            plugin._JackettExtend__sync_all = sync_all

            services = plugin.get_service()

            self.assertEqual(len(services), 2)
            initial, recurring = services
            self.assertEqual(initial["id"], "jackett_extend_sync_initial")
            self.assertEqual(initial["trigger"], "date")
            self.assertEqual(initial["func_kwargs"], {"generation": 4})
            self.assertIn("run_date", initial["kwargs"])
            self.assertEqual(initial["kwargs"]["misfire_grace_time"], 60)
            self.assertTrue(initial["kwargs"]["run_date"].tzinfo)

            self.assertEqual(recurring["id"], "jackett_extend_sync")
            self.assertIsInstance(recurring["trigger"], FakeCronTrigger)
            self.assertEqual(recurring["trigger"].expression, "*/15 * * * *")
            self.assertEqual(recurring["trigger"].timezone, "UTC")
            self.assertTrue(callable(recurring["func"]))
            self.assertEqual(recurring["func_kwargs"], {"generation": 4})
            self.assertEqual(recurring["kwargs"], {
                "max_instances": 1,
                "coalesce": True,
                "misfire_grace_time": 3600,
            })
            initial["func"](**initial["func_kwargs"])
            recurring["func"](**recurring["func_kwargs"])
            self.assertEqual(calls, [4, 4])

            plugin._enabled = False
            self.assertEqual(plugin.get_service(), [])

    def test_timeout_is_bounded_and_written_to_form_defaults(self):
        with loaded_module() as module:
            self.assertEqual(module.JackettExtend._normalize_timeout(-1), 5)
            self.assertEqual(module.JackettExtend._normalize_timeout(999), 120)
            self.assertEqual(module.JackettExtend._normalize_timeout("bad"), 30)
            for value in (float("inf"), float("-inf"), float("nan")):
                self.assertEqual(module.JackettExtend._normalize_timeout(value), 30)
            plugin = object.__new__(module.JackettExtend)
            plugin._indexers_cache = []
            plugin._indexers_cache_ts = 1
            form, defaults = plugin.get_form()
            self.assertEqual(defaults["timeout"], 30)
            def contains_timeout(node):
                if isinstance(node, dict):
                    if node.get("props", {}).get("model") == "timeout":
                        return True
                    return any(contains_timeout(child) for child in node.get("content", []))
                if isinstance(node, list):
                    return any(contains_timeout(child) for child in node)
                return False
            self.assertTrue(contains_timeout(form))

    def test_get_page_renders_privacy_types_and_public_fallback(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._indexers = [
                {"id": "public", "domain": "public", "privacy": "public", "public": True},
                {"id": "semi", "domain": "semi", "privacy": "semi-private", "public": False},
                {"id": "private", "domain": "private", "privacy": "private", "public": False},
                # A profile without new metadata uses the established public
                # boolean as a coarse fallback.
                {"id": "fallback-public", "domain": "fallback-public", "public": True},
                {"id": "fallback-private", "domain": "fallback-private", "public": False},
                {"id": "unknown", "domain": "unknown", "privacy": "unknown", "public": False},
                # Invalid legacy data remains visibly unknown rather than
                # being silently treated as private.
                {"id": "malformed", "domain": "malformed", "public": "false"},
            ]

            page = plugin.get_page()
            texts = self._page_texts(page)
            self.assertIn("类型", texts)
            for label in ("公开", "半公开", "私有", "未知"):
                self.assertIn(label, texts)
            self.assertNotIn("True", texts)
            self.assertNotIn("False", texts)

    def test_get_form_preserves_configuration_models_options_and_description(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin.get_indexers = lambda filter_selected=False, **_kwargs: [
                {"name": "Nyaa", "indexer_id": "nyaa"},
            ]

            form, defaults = plugin.get_form()

            self.assertEqual(defaults, {
                "enabled": False,
                "proxy": False,
                "host": "",
                "api_key": "",
                "password": "",
                "cron": "0 0 * * *",
                "timeout": 30,
                "indexer_sites": [],
            })

            fields = {}
            alerts = []

            def collect(node):
                if isinstance(node, dict):
                    props = node.get("props", {})
                    model = props.get("model")
                    if model:
                        fields[model] = props
                    if node.get("component") == "VAlert":
                        alerts.append(props)
                    for child in node.get("content", []):
                        collect(child)
                elif isinstance(node, list):
                    for child in node:
                        collect(child)

            collect(form)
            self.assertEqual(
                set(fields),
                {"enabled", "timeout", "proxy", "host", "api_key", "password", "cron", "indexer_sites"},
            )
            self.assertEqual(fields["enabled"]["label"], "启用插件")
            self.assertEqual(fields["timeout"]["placeholder"], "30")
            self.assertIn("范围 5-120 秒", fields["timeout"]["hint"])
            self.assertEqual(fields["host"]["placeholder"], "http://127.0.0.1:9117")
            self.assertEqual(fields["api_key"]["label"], "Api Key")
            self.assertEqual(fields["password"]["type"], "password")
            self.assertEqual(fields["cron"]["placeholder"], "0 0 * * *")
            self.assertEqual(fields["indexer_sites"]["items"], [
                {"title": "Nyaa (nyaa)", "value": "nyaa"},
            ])
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["type"], "info")
            self.assertEqual(alerts[0]["variant"], "tonal")
            self.assertEqual(
                alerts[0]["text"],
                "该方式通过 Jackett Torznab API 扩展检索，站点由插件自动注册到站点列表，"
                "并随定时任务与白名单配置自动同步新增、更新与移除。"
                "如遇网络或 API 错误，请查看日志确认 Jackett 地址、Api Key 与密码配置正确。",
            )

    def test_get_page_preserves_table_rows_and_empty_load_behavior(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._indexers = [{
                "id": "JackettExtend-Nyaa",
                "domain": "jackett_extend.nyaa",
                "privacy": "semi-private",
                "public": False,
            }]

            page = plugin.get_page()
            table = page[0]["content"][0]["content"][0]
            header = table["content"][0]["content"][0]["content"]
            row = table["content"][1]["content"][0]
            self.assertEqual([cell["text"] for cell in header], ["id", "站点domain", "类型"])
            self.assertEqual(
                [cell["text"] for cell in row["content"]],
                ["JackettExtend-Nyaa", "https://jackett_extend.nyaa/", "半公开"],
            )

            plugin._ensure_sites_loaded = lambda **_kwargs: False
            self.assertEqual(plugin.get_page(), [])

    def test_old_form_request_cannot_publish_cache_after_reload(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 7
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
                "password": "",
                "proxy": False,
                "timeout": 17,
            }
            plugin._indexers_cache = None
            plugin._indexers_cache_ts = 0.0
            entered = threading.Event()
            release = threading.Event()

            def delayed_fetch(config_snapshot=None, generation=None):
                self.assertEqual(config_snapshot["host"], "https://old.invalid")
                self.assertEqual(generation, 7)
                entered.set()
                self.assertTrue(release.wait(2))
                return [{"name": "old", "indexer_id": "old"}]

            plugin._JackettExtend__fetch_indexers = delayed_fetch
            result = []
            worker = threading.Thread(
                target=lambda: result.append(plugin.get_form()),
            )
            worker.start()
            self.assertTrue(entered.wait(2))

            old_event = plugin._sync_stop_event
            with plugin._state_lock:
                old_event.set()
                plugin._sync_generation = 8
                plugin._sync_stop_event = threading.Event()
                plugin._config_snapshot = {
                    "host": "https://new.invalid",
                    "api_key": "new-key",
                    "password": "",
                    "proxy": False,
                    "timeout": 19,
                }
                plugin._indexers_cache = [{"name": "new", "indexer_id": "new"}]
                plugin._indexers_cache_ts = 123.0

            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(plugin._indexers_cache, [{"name": "new", "indexer_id": "new"}])
            self.assertEqual(plugin._indexers_cache_ts, 123.0)

    def test_old_form_failure_cannot_refresh_new_cache_timestamp_or_error(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 11
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
                "password": "",
                "proxy": False,
                "timeout": 17,
            }
            plugin._indexers_cache = [{"name": "old", "indexer_id": "old"}]
            plugin._indexers_cache_ts = 1.0
            entered = threading.Event()
            release = threading.Event()

            def delayed_failure(config_snapshot=None, generation=None):
                entered.set()
                self.assertTrue(release.wait(2))
                plugin._record_error("old_failure", generation=generation)
                return None

            plugin._JackettExtend__fetch_indexers = delayed_failure
            result = []
            worker = threading.Thread(
                target=lambda: result.append(plugin.get_form()),
            )
            worker.start()
            self.assertTrue(entered.wait(2))

            old_event = plugin._sync_stop_event
            with plugin._state_lock:
                old_event.set()
                plugin._sync_generation = 12
                plugin._sync_stop_event = threading.Event()
                plugin._indexers_cache = [{"name": "new", "indexer_id": "new"}]
                plugin._indexers_cache_ts = 456.0
                plugin._last_error = "new_failure"
                plugin._last_error_at = 456.0

            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(plugin._indexers_cache, [{"name": "new", "indexer_id": "new"}])
            self.assertEqual(plugin._indexers_cache_ts, 456.0)
            self.assertEqual(plugin._last_error, "new_failure")
            self.assertEqual(plugin._last_error_at, 456.0)

    def test_old_page_request_cannot_publish_sites_after_reload(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 15
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
                "password": "",
                "proxy": False,
                "timeout": 17,
            }
            plugin._indexers = []
            plugin._fetch_ok = False
            plugin._indexers_cache = None
            plugin._indexers_cache_ts = 0.0
            entered = threading.Event()
            release = threading.Event()

            def delayed_fetch(config_snapshot=None, generation=None):
                entered.set()
                self.assertTrue(release.wait(2))
                return [{"id": "old", "domain": "jackett_extend.old"}]

            plugin._JackettExtend__fetch_indexers = delayed_fetch
            result = []
            worker = threading.Thread(
                target=lambda: result.append(plugin.get_page()),
            )
            worker.start()
            self.assertTrue(entered.wait(2))

            old_event = plugin._sync_stop_event
            with plugin._state_lock:
                old_event.set()
                plugin._sync_generation = 16
                plugin._sync_stop_event = threading.Event()
                plugin._indexers = [{"id": "new", "domain": "jackett_extend.new"}]
                plugin._fetch_ok = True

            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(
                plugin._indexers,
                [{"id": "new", "domain": "jackett_extend.new"}],
            )
            self.assertTrue(plugin._fetch_ok)
            self.assertEqual(result, [[]])

    def test_ui_builders_are_host_independent_and_do_not_mutate_inputs(self):
        with isolated_ui_module() as ui:
            options = [{"title": "Nyaa (nyaa)", "value": "nyaa"}]
            options_before = copy.deepcopy(options)
            form, defaults = ui.build_form(options, timeout_default=30, timeout_min=5, timeout_max=120)

            self.assertEqual(options, options_before)
            self.assertEqual(defaults["timeout"], 30)
            self.assertEqual(
                form[0]["content"][2]["content"][2]["content"][0]["props"]["items"],
                options_before,
            )

            indexers = [{
                "id": "JackettExtend-Nyaa",
                "domain": "jackett_extend.nyaa",
                "privacy": "public",
                "public": True,
            }]
            indexers_before = copy.deepcopy(indexers)
            page = ui.build_page(indexers)
            self.assertEqual(indexers, indexers_before)
            self.assertEqual(
                page[0]["content"][0]["content"][0]["content"][1]["content"][0]["content"][1]["text"],
                "https://jackett_extend.nyaa/",
            )

    def test_keyword_mask_does_not_retain_original_prefix(self):
        with loaded_module() as module:
            masked = module.JackettExtend._JackettExtend__mask_keyword("Secret Title")
            self.assertNotIn("Secret", masked)
            self.assertNotIn("Title", masked)
            self.assertIn("(12)", masked)

    def test_whitelist_serializations_become_canonical_ids(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            for raw in (
                ["Nyaa", " AnimeTosho "],
                "Nyaa, AnimeTosho",
                "['Nyaa', 'AnimeTosho']",
                ["['Nyaa'", "'AnimeTosho']"],
                "['[\\\"Nyaa\\\", \\\"AnimeTosho\\\"]']",
            ):
                plugin._indexer_sites = raw
                self.assertEqual(plugin._parse_indexer_sites(), ["nyaa", "animetosho"])

            plugin.stop_service = lambda: None
            plugin.init_plugin({
                "enabled": False,
                "indexer_sites": "['Nyaa', 'AnimeTosho']",
            })
            self.assertEqual(plugin._indexer_sites, ["nyaa", "animetosho"])
            self.assertTrue(plugin._indexer_sites_explicit)

    def test_indexer_profiles_filter_bad_rows_and_encode_special_ids(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)

            class Response:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                text = "[]"
                closed = False

                @staticmethod
                def json():
                    return [
                        None,
                        {"id": "foo.bar/baz", "name": "Special", "type": "public", "caps": []},
                    ]

                @staticmethod
                def iter_content(chunk_size=1):
                    yield json.dumps([
                        None,
                        {"id": "foo.bar/baz", "name": "Special", "type": "public", "caps": []},
                    ]).encode("utf-8")

                @classmethod
                def close(cls):
                    cls.closed = True

            class Request:
                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                def get_res(self, *_args, **_kwargs):
                    return Response()

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            original = module.RequestUtils
            module.RequestUtils = Request
            try:
                result = plugin._JackettExtend__fetch_indexers({
                    "host": "https://jackett.invalid",
                    "api_key": "key",
                    "password": "",
                    "proxy": False,
                    "timeout": 30,
                })
            finally:
                module.RequestUtils = original
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["indexer_id"], "foo.bar/baz")
            self.assertEqual(result[0]["privacy"], "public")
            self.assertTrue(result[0]["public"])
            self.assertIn("foo.bar%2Fbaz", result[0]["url"])
            self.assertIn("foo.bar%2Fbaz", result[0]["domain"])

    def test_fetch_indexers_classifies_transport_errors_and_preserves_empty(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            snapshot = {
                "host": "https://jackett.invalid",
                "api_key": "secret-key",
                "password": "",
                "proxy": False,
                "timeout": 30,
            }
            logs = []
            module.logger = types.SimpleNamespace(
                warning=lambda message: logs.append(str(message)),
                error=lambda message: logs.append(str(message)),
                debug=lambda message: logs.append(str(message)),
                info=lambda message: logs.append(str(message)),
            )

            class Response:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                closed = False

                @staticmethod
                def json():
                    return [{"id": "nyaa", "name": "Nyaa", "type": "public", "caps": []}]

                @staticmethod
                def iter_content(chunk_size=1):
                    yield json.dumps([
                        {"id": "nyaa", "name": "Nyaa", "type": "public", "caps": []},
                    ]).encode("utf-8")

                @classmethod
                def close(cls):
                    cls.closed = True

            class Request:
                mode = "normal"
                response = Response()
                calls = []

                def __init__(self, *args, **kwargs):
                    self.calls.append(("init", kwargs))

                def get_res(self, url, **kwargs):
                    self.calls.append(("get", url, kwargs))
                    if self.mode == "timeout":
                        raise requests.Timeout("https://jackett.invalid/?apikey=secret-key&q=private-title")
                    if self.mode == "request_error":
                        raise requests.RequestException(
                            "https://jackett.invalid/?apikey=secret-key&q=private-title"
                        )
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            cases = (
                ("timeout", "timeout", None),
                ("request_error", "request_error", None),
                ("empty", "empty", None),
                ("normal", None, ["nyaa"]),
            )
            for mode, expected_error, expected_indexers in cases:
                with self.subTest(mode=mode):
                    Request.mode = mode
                    Request.response = None if mode == "empty" else Response()
                    Request.calls.clear()
                    plugin._last_error = None
                    result = plugin._JackettExtend__fetch_indexers(snapshot)

                    if expected_indexers is None:
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(
                            [indexer["indexer_id"] for indexer in result],
                            expected_indexers,
                        )
                    self.assertEqual(plugin._last_error, expected_error)
                    self.assertTrue(Request.calls[-1][2]["raise_exception"])

            rendered_logs = " ".join(logs)
            self.assertNotIn("secret-key", rendered_logs)
            self.assertNotIn("private-title", rendered_logs)

    def test_parse_torznab_classifies_transport_errors_and_preserves_empty(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._state_lock = module.JackettExtend._state_lock
            url = "https://jackett.invalid/results?apikey=secret-key&q=private-title"
            logs = []
            module.logger = types.SimpleNamespace(
                warning=lambda message: logs.append(str(message)),
                error=lambda message: logs.append(str(message)),
                debug=lambda message: logs.append(str(message)),
                info=lambda message: logs.append(str(message)),
            )

            class Response:
                status_code = 200
                headers = {"Content-Type": "application/xml"}
                text = (
                    "<rss><channel>"
                    "<item><title>Release</title>"
                    "<enclosure url='https://site/release.torrent'/></item>"
                    "</channel></rss>"
                )
                closed = False

                @classmethod
                def iter_content(cls, chunk_size=1):
                    yield cls.text.encode("utf-8")

                @classmethod
                def close(cls):
                    cls.closed = True

            class Request:
                mode = "normal"
                response = Response()
                calls = []

                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, url, **kwargs):
                    self.calls.append((url, kwargs))
                    if self.mode == "timeout":
                        raise requests.Timeout("https://jackett.invalid/?apikey=secret-key&q=private-title")
                    if self.mode == "request_error":
                        raise requests.RequestException(
                            "https://jackett.invalid/?apikey=secret-key&q=private-title"
                        )
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            cases = (
                ("timeout", "timeout", 0),
                ("request_error", "request_error", 0),
                ("empty", "empty", 0),
                ("normal", None, 1),
            )
            for mode, expected_error, expected_count in cases:
                with self.subTest(mode=mode):
                    Request.mode = mode
                    Request.response = None if mode == "empty" else Response()
                    Request.calls.clear()
                    plugin._last_search_error = None
                    plugin._last_search_error_at = 0.0
                    result = plugin._JackettExtend__parse_torznab_xml(url)

                    self.assertEqual(len(result), expected_count)
                    self.assertEqual(plugin._last_search_error, expected_error)
                    self.assertTrue(Request.calls[-1][1]["raise_exception"])

            rendered_logs = " ".join(logs)
            self.assertNotIn("secret-key", rendered_logs)
            self.assertNotIn("private-title", rendered_logs)

    def test_streaming_xml_and_rest_reads_never_touch_buffered_body(self):
        with loaded_module() as module:
            xml = (
                '<?xml version="1.0" encoding="UTF-16"?><rss><channel>'
                "<item><title>流式标题</title>"
                "<enclosure url='https://site/stream.torrent'/></item>"
                "</channel></rss>"
            ).encode("utf-16")
            xml_response = _GuardedResponse(
                (xml[:7], xml[7:]),
                headers={"Content-Type": "application/rss+xml"},
            )

            class XmlRequest:
                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                def get_stream(self, *args, **kwargs):
                    return _stream_response(xml_response)

            module.RequestUtils = XmlRequest
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            results = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "流式站点"},
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "流式标题")
            self.assertTrue(xml_response.closed)
            self.assertEqual(xml_response.iterated_chunks, 2)

            payload = json.dumps([{
                "id": "nyaa",
                "name": "Nyaa",
                "type": "public",
                "caps": [],
            }]).encode("utf-8")
            rest_response = _GuardedResponse(
                (payload[:2], payload[2:]),
                headers={"Content-Type": "application/json"},
            )

            class RestRequest:
                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                def get_stream(self, *args, **kwargs):
                    return _stream_response(rest_response)

            module.RequestUtils = RestRequest
            fetch_result = plugin._JackettExtend__fetch_indexers({
                "host": "https://jackett.invalid",
                "api_key": "key",
                "password": "",
                "proxy": False,
                "timeout": 12,
            })

            self.assertEqual([item["indexer_id"] for item in fetch_result], ["nyaa"])
            self.assertTrue(rest_response.closed)
            self.assertEqual(rest_response.iterated_chunks, 2)

    def test_streaming_limits_and_read_errors_close_responses(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._state_lock = module.JackettExtend._state_lock
            plugin.TORZNAB_MAX_XML_BYTES = 8
            xml_url = "https://jackett.invalid/results?q=title&apikey=secret"

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    pass

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.response)

            module.RequestUtils = Request

            oversized_xml = _GuardedResponse(
                (b"x" * (plugin.TORZNAB_MAX_XML_BYTES + 1), b"tail"),
                headers={"Content-Type": "application/xml"},
            )
            Request.response = oversized_xml
            plugin._last_search_error = None
            self.assertEqual(plugin._JackettExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "xml_too_large")
            self.assertTrue(oversized_xml.closed)
            self.assertEqual(oversized_xml.iterated_chunks, 1)

            rejected_xml = _GuardedResponse(
                (),
                status_code=503,
                headers={"Content-Type": "application/xml"},
                error=AssertionError("status rejection must not read the body"),
            )
            Request.response = rejected_xml
            plugin._last_search_error = None
            self.assertEqual(plugin._JackettExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "http_503")
            self.assertTrue(rejected_xml.closed)
            self.assertEqual(rejected_xml.iterated_chunks, 0)

            truncated_xml = _GuardedResponse(
                (b"<rss>",),
                headers={"Content-Type": "application/xml"},
                error=requests.exceptions.ChunkedEncodingError("truncated"),
            )
            Request.response = truncated_xml
            plugin._last_search_error = None
            self.assertEqual(plugin._JackettExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "request_error")
            self.assertTrue(truncated_xml.closed)

            plugin.REST_MAX_JSON_BYTES = 8
            oversized_json = _GuardedResponse(
                (b"123456789", b"tail"),
                headers={"Content-Type": "application/json"},
            )
            Request.response = oversized_json
            plugin._last_error = None
            self.assertIsNone(plugin._JackettExtend__fetch_indexers({
                "host": "https://jackett.invalid",
                "api_key": "key",
                "password": "",
                "proxy": False,
                "timeout": 12,
            }))
            self.assertEqual(plugin._last_error, "json_too_large")
            self.assertTrue(oversized_json.closed)
            self.assertEqual(oversized_json.iterated_chunks, 1)

    def test_real_response_read_timeout_is_distinct_from_connection_errors(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._state_lock = module.JackettExtend._state_lock
            xml_url = "https://jackett.invalid/results?q=title&apikey=secret"

            def timeout_stream():
                yield b"<rss>"
                raise ReadTimeoutError(None, "/test", "timed out")

            xml_response, xml_close_calls = _real_stream_response(
                timeout_stream,
                {"Content-Type": "application/xml"},
            )

            class Request:
                response = xml_response

                def __init__(self, *args, **kwargs):
                    pass

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.response)

            module.RequestUtils = Request
            plugin._last_search_error = None
            self.assertEqual(plugin._JackettExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "timeout")
            self.assertEqual(xml_close_calls, [True])

            def timeout_json_stream():
                yield b"["
                raise ReadTimeoutError(None, "/test", "timed out")

            json_response, json_close_calls = _real_stream_response(
                timeout_json_stream,
                {"Content-Type": "application/json"},
            )
            Request.response = json_response
            plugin._last_error = None
            self.assertIsNone(plugin._JackettExtend__fetch_indexers({
                "host": "https://jackett.invalid",
                "api_key": "key",
                "password": "",
                "proxy": False,
                "timeout": 12,
            }))
            self.assertEqual(plugin._last_error, "timeout")
            self.assertEqual(json_close_calls, [True])

            def connection_stream():
                yield b"<rss>"
                raise requests.ConnectionError("ordinary connection failure")

            connection_response, connection_close_calls = _real_stream_response(
                connection_stream,
                {"Content-Type": "application/xml"},
            )
            Request.response = connection_response
            plugin._last_search_error = None
            self.assertEqual(plugin._JackettExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "request_error")
            self.assertEqual(connection_close_calls, [True])

            def chunk_stream():
                yield b"["
                raise requests.exceptions.ChunkedEncodingError("truncated")

            chunk_response, chunk_close_calls = _real_stream_response(
                chunk_stream,
                {"Content-Type": "application/json"},
            )
            Request.response = chunk_response
            plugin._last_error = None
            self.assertIsNone(plugin._JackettExtend__fetch_indexers({
                "host": "https://jackett.invalid",
                "api_key": "key",
                "password": "",
                "proxy": False,
                "timeout": 12,
            }))
            self.assertEqual(plugin._last_error, "request_error")
            self.assertEqual(chunk_close_calls, [True])

    def test_password_login_uses_streaming_response_and_reuses_session_cookies(self):
        with loaded_module() as module:
            class Cookies:
                def __bool__(self):
                    return True

                def get_dict(self):
                    return {"jackett-session": "opaque"}

            class Session:
                def __init__(self):
                    self.cookies = Cookies()
                    self.closed = False

                def close(self):
                    self.closed = True

            session = Session()
            login_response = _GuardedResponse((), headers={"Content-Type": "text/html"})
            indexer_response = _GuardedResponse(
                (json.dumps([{
                    "id": "nyaa",
                    "name": "Nyaa",
                    "type": "public",
                    "caps": [],
                }]).encode("utf-8"),),
                headers={"Content-Type": "application/json"},
            )
            calls = []

            class Request:
                def __init__(self, *args, **kwargs):
                    calls.append(("init", kwargs))

                def post_res(self, *args, **kwargs):
                    calls.append(("post", args, kwargs))
                    return login_response

                def get_stream(self, *args, **kwargs):
                    calls.append(("get_stream", args, kwargs))
                    return _stream_response(indexer_response)

            original_session_factory = module.requests.session
            module.requests.session = lambda: session
            module.RequestUtils = Request
            try:
                result = object.__new__(module.JackettExtend)._JackettExtend__fetch_indexers({
                    "host": "https://jackett.invalid",
                    "api_key": "key",
                    "password": "admin-password",
                    "proxy": False,
                    "timeout": 23,
                })
            finally:
                module.requests.session = original_session_factory

            self.assertEqual([item["indexer_id"] for item in result], ["nyaa"])
            self.assertTrue(login_response.closed)
            self.assertTrue(indexer_response.closed)
            self.assertTrue(session.closed)
            self.assertEqual(calls[0][1]["session"], session)
            self.assertEqual(calls[1][2]["stream"], True)
            self.assertEqual(calls[2][1]["cookies"], {"jackett-session": "opaque"})

    def test_finite_all_stale_whitelist_never_becomes_all(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._indexer_sites = ["removed"]
            plugin._indexer_sites_explicit = True
            selected = plugin._apply_indexer_selection([
                {"indexer_id": "nyaa", "domain": "jackett_extend.nyaa"},
            ])
            self.assertEqual(selected, [])

    def test_sync_captures_generation_config_snapshot_before_status(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 7
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
                "password": "old-password",
                "proxy": True,
                "timeout": 17,
            }
            captured = {}

            def status(generation=None, config_snapshot=None):
                captured.update(config_snapshot or {})
                return False

            plugin.get_status = status
            plugin._JackettExtend__sync_all(generation=7)
            self.assertEqual(captured["host"], "https://old.invalid")
            self.assertEqual(captured["api_key"], "old-key")
            self.assertEqual(captured["password"], "old-password")
            self.assertTrue(captured["proxy"])
            self.assertEqual(captured["timeout"], 17)

    def test_stale_generation_cannot_commit_cache_state_or_side_effects(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 11
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
                "password": "old-password",
                "proxy": False,
                "timeout": 17,
            }
            cached = [{"indexer_id": "cached"}]
            authoritative = [{"indexer_id": "current"}]
            selected = [{"indexer_id": "selected"}]
            plugin._indexers_cache = cached
            plugin._indexers_cache_ts = 123.0
            plugin._authoritative_indexers = authoritative
            plugin._indexers = selected
            plugin._fetch_ok = True
            plugin._sync_ready = True
            plugin._last_sync_ok = True
            plugin._indexer_sites = []
            entered = threading.Event()
            release = threading.Event()
            side_effects = []

            def delayed_fetch(config_snapshot=None, generation=None):
                entered.set()
                self.assertTrue(release.wait(2))
                return [{
                    "indexer_id": "stale-result",
                    "domain": "jackett_extend.stale-result",
                }]

            plugin._JackettExtend__fetch_indexers = delayed_fetch
            plugin._JackettExtend__register_site = (
                lambda *_args, **_kwargs: side_effects.append("register")
            )
            plugin._JackettExtend__sync_remove_stale_sites = (
                lambda *_args, **_kwargs: side_effects.append("cleanup")
            )
            worker = threading.Thread(
                target=plugin._JackettExtend__sync_all,
                kwargs={"generation": 11},
            )
            worker.start()
            self.assertTrue(entered.wait(2))
            with plugin._state_lock:
                plugin._sync_generation = 12
            release.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertIs(plugin._indexers_cache, cached)
            self.assertEqual(plugin._indexers_cache_ts, 123.0)
            self.assertIs(plugin._authoritative_indexers, authoritative)
            self.assertIs(plugin._indexers, selected)
            self.assertTrue(plugin._last_sync_ok)
            self.assertEqual(side_effects, [])

    def test_none_config_clears_previous_credentials_and_defaults(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin.stop_service = lambda: None
            plugin._host = "https://old.invalid"
            plugin._api_key = "old-key"
            plugin._password = "old-password"
            plugin._enabled = True
            plugin._proxy = True
            plugin._timeout = 99
            plugin._indexer_sites = ["nyaa"]
            plugin.init_plugin(None)
            self.assertEqual(plugin._host, "")
            self.assertEqual(plugin._api_key, "")
            self.assertEqual(plugin._password, "")
            self.assertFalse(plugin._enabled)
            self.assertFalse(plugin._proxy)
            self.assertEqual(plugin._timeout, 30)
            self.assertEqual(plugin._indexer_sites, [])

    def test_diagnostic_endpoint_never_returns_credentials(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._host = "https://user:password@jackett.invalid/?apikey=secret"
            plugin._api_key = "secret"
            plugin._password = "password"
            plugin._enabled = True
            plugin._indexers = []
            plugin._authoritative_indexers = []
            plugin._fetch_ok = True
            plugin._sync_ready = False
            plugin._last_sync_ok = False
            plugin._last_sync_at = 0
            plugin._last_error = "timeout"
            plugin._last_error_at = 1
            payload = plugin.api_status()
            rendered = repr(payload)
            self.assertNotIn("secret", rendered)
            self.assertNotIn("password", rendered)
            self.assertNotIn("user:", rendered)
            self.assertNotIn("host", payload.model_dump())
            self.assertEqual(payload.last_error, "timeout")

    def test_probe_failure_keeps_sync_error_and_reports_local_probe_error(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._host = ""
            plugin._api_key = ""
            plugin._password = ""
            plugin._enabled = True
            plugin._last_error = "sync_timeout"
            plugin._last_error_at = 11
            plugin._indexers = []
            plugin._authoritative_indexers = []
            plugin._fetch_ok = True
            plugin._sync_ready = True
            plugin._last_sync_ok = True
            plugin._last_sync_at = 10

            payload = plugin.api_test()

            self.assertFalse(payload.ok)
            self.assertFalse(payload.connected)
            self.assertEqual(payload.last_error, "sync_timeout")
            self.assertEqual(payload.last_error_at, 11)
            self.assertEqual(payload.probe_error, "missing_config")
            self.assertIsNotNone(payload.probe_error_at)

    def test_probe_success_keeps_sync_error_and_reports_success(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._host = "https://jackett.invalid"
            plugin._api_key = "key"
            plugin._password = ""
            plugin._enabled = True
            plugin._last_error = "sync_timeout"
            plugin._last_error_at = 11
            plugin._indexers = []
            plugin._authoritative_indexers = []
            plugin._fetch_ok = False
            plugin._sync_ready = False
            plugin._last_sync_ok = False
            plugin._last_sync_at = 10

            def fetch_probe(**_kwargs):
                return [{"indexer_id": "nyaa", "domain": "jackett_extend.nyaa"}]

            plugin._JackettExtend__fetch_indexers = fetch_probe
            payload = plugin.api_test()

            self.assertTrue(payload.ok)
            self.assertTrue(payload.connected)
            self.assertEqual(payload.last_error, "sync_timeout")
            self.assertEqual(payload.last_error_at, 11)
            self.assertIsNone(payload.probe_error)
            self.assertIsNone(payload.probe_error_at)

    def test_search_error_isolated_from_sync_error_and_exposed_in_status(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = (
                '<?xml version="1.0"?><error code="100" '
                'description="secret diagnostic"/>'
            )
            plugin = object.__new__(module.JackettExtend)
            plugin._host = "https://jackett.invalid"
            plugin._api_key = "key"
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = "sync_timeout"
            plugin._last_error_at = 11
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results?q=secret&apikey=key",
            )
            payload = plugin.api_status()

            self.assertEqual(result, [])
            self.assertEqual(plugin._last_error, "sync_timeout")
            self.assertEqual(plugin._last_search_error, "torznab_error")
            self.assertEqual(payload.last_error, "sync_timeout")
            self.assertEqual(payload.last_search_error, "torznab_error")
            rendered = repr(payload)
            self.assertNotIn("secret", rendered)
            self.assertNotIn("diagnostic", rendered)
            self.assertNotIn("key", rendered)

    def test_parser_populates_v3_context_and_deduplicates_per_site(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>One</title><guid>same-guid</guid><comments>https://site/item/1</comments>
                <enclosure url='https://site/one.torrent'/><size>12</size>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='seeders' value='4'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='peers' value='5'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='grabs' value='7'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='uploadvolumefactor' value='2.5'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='downloadvolumefactor' value='0'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='infohash' value='abc'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='label' value='trusted'/>
              </item>
              <item><title>Different infohash, same GUID</title><guid>same-guid</guid><enclosure url='https://site/two.torrent'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='infohash' value='different-hash'/>
              </item>
              <item><title>Two</title><guid>unique-guid</guid><enclosure url='magnet:?xt=urn:btih:def'/></item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock
            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results?apikey=secret",
                site={"id": 3, "name": "Nyaa", "cookie": "cookie", "ua": "ua", "proxy": True, "pri": 2},
                mtype=_MediaType.MUSIC,
            )
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0].size, 12.0)
            self.assertEqual(result[0].seeders, 4)
            self.assertEqual(result[0].peers, 5)
            self.assertEqual(result[0].grabs, 7)
            self.assertEqual(result[0].uploadvolumefactor, 2.5)
            self.assertEqual(result[0].downloadvolumefactor, 0.0)
            self.assertEqual(result[0].site, 3)
            self.assertEqual(result[0].site_cookie, "cookie")
            self.assertEqual(result[0].site_order, 2)
            self.assertEqual(result[0].labels, ["trusted"])
            self.assertEqual(result[0].category, "音乐")
            self.assertEqual(_RequestUtils.timeouts[-1], 12)

    def test_parser_clears_shared_page_url_for_distinct_torrents(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>Bangumi A</title><guid>guid-a</guid>
                <comments>https://bangumi.example/</comments>
                <enclosure url='https://jackett.invalid/a.torrent'/>
              </item>
              <item><title>Bangumi B</title><guid>guid-b</guid>
                <comments>https://bangumi.example/</comments>
                <enclosure url='https://jackett.invalid/b.torrent'/>
              </item>
              <item><title>Normal Detail</title><guid>guid-c</guid>
                <comments>https://tracker.example/details/c</comments>
                <enclosure url='https://jackett.invalid/c.torrent'/>
              </item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Bangumi Moe"},
            )

            self.assertEqual(len(result), 3)
            self.assertIsNone(result[0].page_url)
            self.assertIsNone(result[1].page_url)
            self.assertEqual(result[2].page_url, "https://tracker.example/details/c")
            self.assertEqual(
                [item.enclosure for item in result[:2]],
                [
                    "https://jackett.invalid/a.torrent",
                    "https://jackett.invalid/b.torrent",
                ],
            )

    def test_parser_prefers_raw_content_when_text_guess_conflicts_with_xml_encoding(self):
        with loaded_module() as module:
            title = "Café release"
            xml = (
                '<?xml version="1.0" encoding="ISO-8859-1"?>'
                "<rss><channel><item>"
                f"<title>{title}</title>"
                "<enclosure url='https://site/latin1.torrent'/>"
                "</item></channel></rss>"
            )
            response = _Response()
            response.wire_content = xml.encode("iso-8859-1")
            # Simulate an HTTP charset guess that decoded the wire bytes as
            # UTF-8 before the parser received the response.
            response.text = response.wire_content.decode("utf-8", errors="replace")
            _RequestUtils.response = response
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].title, title)

    def test_parser_reads_stream_body_when_response_has_no_buffered_body(self):
        with loaded_module() as module:
            response = _Response()
            response.text = (
                '<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
                "<item><title>Text fallback</title>"
                "<enclosure url='https://site/text-fallback.torrent'/>"
                "</item></channel></rss>"
            )
            self.assertFalse(hasattr(response, "content"))
            _RequestUtils.response = response
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].title, "Text fallback")

    def test_parser_accepts_normal_utf8_raw_content(self):
        with loaded_module() as module:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?><rss><channel>'
                "<item><title>正常 UTF-8</title>"
                "<enclosure url='https://site/utf8.torrent'/>"
                "</item></channel></rss>"
            )
            response = _Response()
            response.wire_content = xml.encode("utf-8")
            response.text = ""
            _RequestUtils.response = response
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].title, "正常 UTF-8")

    def test_parser_rejects_dtd_in_all_supported_wire_encodings(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            encodings = (
                ("utf-8", "UTF-8", codecs.BOM_UTF8),
                ("utf-16-le", "UTF-16", codecs.BOM_UTF16_LE),
                ("utf-16-be", "UTF-16", codecs.BOM_UTF16_BE),
                ("utf-32-le", "UTF-32", codecs.BOM_UTF32_LE),
                ("utf-32-be", "UTF-32", codecs.BOM_UTF32_BE),
            )
            for encoding, declaration, bom in encodings:
                for with_declaration in (False, True):
                    for with_bom in (False, True):
                        with self.subTest(
                                encoding=encoding,
                                with_declaration=with_declaration,
                                with_bom=with_bom):
                            xml = (
                                f'<?xml version="1.0" encoding="{declaration}"?>'
                                if with_declaration else ""
                            ) + (
                                '<!DOCTYPE rss [<!ENTITY x "expanded">]>'
                                '<rss><channel><item><title>&x;</title>'
                                '<enclosure url="https://site/release.torrent"/>'
                                '</item></channel></rss>'
                            )
                            content = xml.encode(encoding)
                            if with_bom:
                                content = bom + content
                            response = _real_xml_response(content)
                            self.assertIsInstance(response, requests.Response)
                            Request.response = response
                            plugin._last_search_error = None

                            self.assertEqual(
                                plugin._JackettExtend__parse_torznab_xml(
                                    "https://jackett.invalid/results?q=private-title&apikey=secret"
                                ),
                                [],
                            )
                            self.assertEqual(plugin._last_search_error, "xml_doctype")

    def test_parser_preserves_xml_declaration_encoding_for_real_response(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            cases = (
                ("utf-8", "UTF-8", "中文标题"),
                ("utf-16-le", "UTF-16", "日本語タイトル"),
                ("utf-16-be", "UTF-16", "中文と日本語"),
                ("iso-8859-1", "ISO-8859-1", "Café release"),
            )
            for encoding, declaration, title in cases:
                with self.subTest(encoding=encoding):
                    xml = (
                        f'<?xml version="1.0" encoding="{declaration}"?><rss><channel>'
                        f'<item><title>{title}</title>'
                        '<enclosure url="https://site/release.torrent"/>'
                        '</item></channel></rss>'
                    )
                    Request.response = _real_xml_response(xml.encode(encoding))
                    plugin._last_search_error = None

                    result = plugin._JackettExtend__parse_torznab_xml(
                        "https://jackett.invalid/results",
                        site={"name": "Encoding Site"},
                    )

                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0].title, title)
                    self.assertIsNone(plugin._last_search_error)

    def test_parser_normalizes_valid_imdb_identity(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>IMDb result</title><enclosure url='https://site/imdb.torrent'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='imdbid' value=' TT1234567 '/>
              </item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].media_source, _MediaSource.IMDb)
            self.assertEqual(result[0].media_id, "tt1234567")

    def test_parser_accepts_imdb_identity_without_maximum_digit_count(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>Long IMDb result</title><enclosure url='https://site/long-imdb.torrent'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='imdbid' value='tt12345678901234567890'/>
              </item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].media_source, _MediaSource.IMDb)
            self.assertEqual(result[0].media_id, "tt12345678901234567890")

    def test_parser_keeps_torrents_with_invalid_imdb_identity_unset(self):
        invalid_values = (
            "",
            "1234567",
            "tt123456",
            "TT0000000000",
            "tt123456x",
            "tt1234567!",
            "tt１２３４５６７",
        )
        items = "".join(
            f"""<item><title>Invalid IMDb {index}</title>
              <enclosure url='https://site/invalid-imdb-{index}.torrent'/>
              <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed'
                name='imdbid' value='{value}'/>
            </item>"""
            for index, value in enumerate(invalid_values)
        )

        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>"
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), len(invalid_values))
            for torrent in result:
                self.assertIsNone(torrent.media_source)
                self.assertIsNone(torrent.media_id)

    def test_parser_falls_back_for_negative_size_and_counts(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>Negative numerics</title><enclosure url='https://site/negative.torrent'/>
                <size>-1</size>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='seeders' value='-2'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='peers' value='-3'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='grabs' value='-4'/>
              </item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results",
                site={"name": "Nyaa"},
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].size, 0.0)
            self.assertEqual(result[0].seeders, 0)
            self.assertEqual(result[0].peers, 0)
            self.assertEqual(result[0].grabs, 0)

    def test_parser_falls_back_for_invalid_size_and_counts_and_is_strict_json_safe(self):
        for index, value in enumerate(("not-a-number", "NaN", "Infinity", "-Infinity")):
            with self.subTest(value=value), loaded_module() as module:
                _RequestUtils.response = _Response()
                _RequestUtils.response.text = f"""<?xml version='1.0'?><rss><channel>
                  <item><title>Nonfinite numerics {index}</title>
                    <enclosure url='https://site/nonfinite-{index}.torrent'/>
                    <size>{value}</size>
                    <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='seeders' value='{value}'/>
                    <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='peers' value='{value}'/>
                    <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='grabs' value='{value}'/>
                  </item>
                </channel></rss>"""
                plugin = object.__new__(module.JackettExtend)
                plugin._timeout = 12
                plugin._proxy = False
                plugin._last_error = None
                plugin._state_lock = module.JackettExtend._state_lock

                result = plugin._JackettExtend__parse_torznab_xml(
                    "https://jackett.invalid/results",
                    site={"name": "Nyaa"},
                )

                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].size, 0.0)
                self.assertEqual(result[0].seeders, 0)
                self.assertEqual(result[0].peers, 0)
                self.assertEqual(result[0].grabs, 0)
                json.dumps(result[0].__dict__, allow_nan=False)

    def test_parser_rejects_invalid_promotion_factors_without_marking_free(self):
        for index, value in enumerate(("NaN", "Infinity", "-Infinity", "-1")):
            with self.subTest(value=value), loaded_module() as module:
                _RequestUtils.response = _Response()
                _RequestUtils.response.text = f"""<?xml version='1.0'?><rss><channel>
                  <item><title>Invalid factors {index}</title>
                    <enclosure url='https://site/invalid-factors-{index}.torrent'/>
                    <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed'
                      name='uploadvolumefactor' value='{value}'/>
                    <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed'
                      name='downloadvolumefactor' value='{value}'/>
                  </item>
                </channel></rss>"""
                plugin = object.__new__(module.JackettExtend)
                plugin._timeout = 12
                plugin._proxy = False
                plugin._last_error = None
                plugin._state_lock = module.JackettExtend._state_lock

                result = plugin._JackettExtend__parse_torznab_xml(
                    "https://jackett.invalid/results",
                    site={"name": "Nyaa"},
                )

                self.assertEqual(len(result), 1)
                self.assertIsNone(result[0].uploadvolumefactor)
                self.assertIsNone(result[0].downloadvolumefactor)
                json.dumps(result[0].__dict__, allow_nan=False)

    def test_parser_duplicate_infohash_prefers_http_torrent_over_magnet(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = """<?xml version='1.0'?><rss><channel>
              <item><title>Magnet first</title><guid>g1</guid><enclosure url='magnet:?xt=urn:btih:same'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='infohash' value='same'/>
              </item>
              <item><title>HTTP second</title><guid>g2</guid><enclosure url='https://site/dl/same.torrent'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='infohash' value='same'/>
              </item>
            </channel></rss>"""
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock
            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results?q=private-title&apikey=secret",
                site={"name": "Nyaa"},
            )
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].title, "HTTP second")
            self.assertTrue(result[0].enclosure.startswith("https://"))

    def test_parser_rejects_http_200_torznab_error_without_leaking_details(self):
        with loaded_module() as module:
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = (
                '<?xml version="1.0"?><error code="100" '
                'description="secret diagnostic"/>'
            )
            logs = []
            module.logger = types.SimpleNamespace(
                warning=lambda message: logs.append(message),
            )
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock
            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results?q=private-title&apikey=secret",
            )
            self.assertEqual(result, [])
            self.assertEqual(plugin._last_search_error, "torznab_error")
            rendered_logs = " ".join(logs)
            self.assertNotIn("description", rendered_logs)
            self.assertNotIn("secret diagnostic", rendered_logs)
            self.assertNotIn("secret", rendered_logs)
            self.assertNotIn("private-title", rendered_logs)

    def test_parser_rejects_doctype_and_oversized_body(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock
            for body in (
                "<!DOCTYPE rss [<!ENTITY x 'expanded'>]><rss><channel></channel></rss>",
                "<rss><channel>" + "x" * (module.JackettExtend.TORZNAB_MAX_XML_BYTES + 1) + "</channel></rss>",
            ):
                _RequestUtils.response = _Response()
                _RequestUtils.response.text = body
                self.assertEqual(
                    plugin._JackettExtend__parse_torznab_xml("https://jackett.invalid/results?q=x"),
                    [],
                )

    def test_parser_applies_size_limit_to_raw_content(self):
        with loaded_module() as module:
            response = _Response()
            response.wire_content = (
                b"<rss><channel>"
                + b"x" * (module.JackettExtend.TORZNAB_MAX_XML_BYTES + 1)
                + b"</channel></rss>"
            )
            # A smaller text representation must not bypass the wire-size
            # limit when raw content is available.
            response.text = "<rss><channel /></rss>"
            _RequestUtils.response = response
            plugin = object.__new__(module.JackettExtend)
            plugin._timeout = 12
            plugin._proxy = False
            plugin._last_error = None
            plugin._state_lock = module.JackettExtend._state_lock

            result = plugin._JackettExtend__parse_torznab_xml(
                "https://jackett.invalid/results?q=x"
            )

            self.assertEqual(result, [])
            self.assertEqual(plugin._last_search_error, "xml_too_large")

            plugin.TORZNAB_MAX_ITEMS = 1
            _RequestUtils.response = _Response()
            _RequestUtils.response.text = (
                "<rss><channel>"
                "<item><title>One</title><enclosure url='https://site/one.torrent'/></item>"
                "<item><title>Two</title><enclosure url='https://site/two.torrent'/></item>"
                "</channel></rss>"
            )
            self.assertEqual(
                plugin._JackettExtend__parse_torznab_xml("https://jackett.invalid/results?q=x"),
                [],
            )

    def test_empty_snapshot_cannot_trigger_destructive_cleanup(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 1
            plugin._fetch_ok = False
            plugin._sync_ready = False
            plugin._authoritative_indexers = None
            plugin._indexers = []
            called = []

            def failed_status(generation=None, config_snapshot=None):
                plugin._fetch_ok = True
                plugin._sync_ready = False
                plugin._authoritative_indexers = []
                plugin._indexers = []
                return False

            plugin.get_status = failed_status
            plugin._JackettExtend__cleanup_stale_selection = lambda *_args: called.append("selection")
            plugin._JackettExtend__sync_remove_stale_sites = lambda *_args: called.append("sites")
            plugin._JackettExtend__sync_all(generation=1)
            self.assertEqual(called, [])

    def test_sync_does_not_fallback_to_legacy_status_signature(self):
        """V3 synchronization must call the current snapshot-aware contract."""
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 1

            # A pre-V3 test/adapter override accepted only ``generation``.
            # Keeping a retry for that shape would hide an ABI mismatch and
            # retain the historical compatibility branch in V3-only code.
            def legacy_status(generation=None):
                return False

            plugin.get_status = legacy_status
            with self.assertRaises(TypeError):
                plugin._JackettExtend__sync_all(generation=1)

    def test_sync_stage_failure_keeps_last_sync_ok_false(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 9
            plugin._fetch_ok = True
            plugin._sync_ready = True
            plugin._authoritative_indexers = [
                {"indexer_id": "nyaa", "domain": "jackett_extend.nyaa"},
            ]
            plugin._indexers = list(plugin._authoritative_indexers)
            plugin._indexer_sites = []

            class BrokenSites:
                @staticmethod
                def add_indexer(*_args, **_kwargs):
                    raise RuntimeError("broken helper")

            plugin.sites_helper = BrokenSites()
            plugin.get_status = lambda generation=None, config_snapshot=None: True
            plugin._JackettExtend__register_site = lambda *_args, **_kwargs: False
            plugin._JackettExtend__sync_remove_stale_sites = lambda *_args, **_kwargs: True
            plugin._JackettExtend__sync_all(generation=9)
            self.assertFalse(plugin._last_sync_ok)

    def test_old_generation_is_rejected_after_reload_event_replacement(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 4

            self.assertFalse(plugin._sync_is_current(generation=3))
            self.assertTrue(plugin._sync_is_current(generation=4))

    def test_all_stale_selection_stays_empty_and_does_not_cleanup_sites(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._sync_stop_event = __import__("threading").Event()
            plugin._sync_generation = 2
            plugin._fetch_ok = True
            plugin._sync_ready = True
            plugin._indexer_sites = ["removed"]
            authoritative = [
                {"indexer_id": "nyaa", "domain": "jackett_extend.nyaa"},
                {"indexer_id": "animetosho", "domain": "jackett_extend.animetosho"},
            ]
            plugin._authoritative_indexers = authoritative
            plugin._indexers = []
            plugin.sites_helper = None
            registered = []
            cleanup_snapshots = []

            def status(generation=None, config_snapshot=None):
                return True

            def cleanup_selection(_snapshot, generation=None):
                # Keep the finite stale whitelist; cleanup must not turn it
                # into the all-indexers meaning.
                return True

            plugin.get_status = status
            plugin._JackettExtend__cleanup_stale_selection = cleanup_selection
            plugin._JackettExtend__register_site = (
                lambda item, generation=None: registered.append(item["indexer_id"])
            )
            plugin._JackettExtend__sync_remove_stale_sites = (
                lambda snapshot, generation=None:
                cleanup_snapshots.append([item["indexer_id"] for item in snapshot])
            )

            plugin._JackettExtend__sync_all(generation=2)

            self.assertEqual(registered, [])
            self.assertEqual(cleanup_snapshots, [])


class JackettSyncBoundaryTest(unittest.TestCase):
    @staticmethod
    def _active_plugin(module, generation=1):
        plugin = object.__new__(module.JackettExtend)
        plugin._sync_stop_event = threading.Event()
        plugin._sync_generation = generation
        return plugin

    @staticmethod
    def _event_manager(fail=False, invalidate=None):
        state = types.SimpleNamespace(calls=[])

        class EventManager:
            def send_event(self, event, payload):
                state.calls.append((event, payload))
                if invalidate is not None:
                    invalidate()
                if fail:
                    raise RuntimeError("event unavailable")

        return EventManager(), state

    def test_register_site_adds_rows_via_current_oper_import(self):
        with loaded_module() as module:
            state = types.SimpleNamespace(adds=[], updates=[], lookups=[])

            class FakeSiteOper:
                def get_by_domain(self, domain):
                    state.lookups.append(domain)
                    return None

                def add(self, **payload):
                    state.adds.append(payload)

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = self._active_plugin(module)
            indexer = {
                "name": "Nyaa",
                "domain": "jackett_extend.nyaa",
                "public": True,
                "proxy": True,
            }

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__register_site(indexer, generation=1)

            self.assertTrue(result)
            self.assertEqual(state.lookups, ["jackett_extend.nyaa"])
            self.assertEqual(state.adds, [{
                "name": "Nyaa",
                "domain": "jackett_extend.nyaa",
                "url": "https://jackett_extend.nyaa/",
                "public": 1,
                "proxy": 1,
                "is_active": True,
                "pri": 1,
            }])
            self.assertEqual(state.updates, [])
            self.assertEqual(
                events.calls,
                [("SiteUpdated", {"domain": "jackett_extend.nyaa"})],
            )

    def test_register_site_updates_only_plugin_owned_fields(self):
        with loaded_module() as module:
            existing = types.SimpleNamespace(
                id=41,
                domain="jackett_extend.nyaa",
                is_active=False,
                pri=99,
                proxy=1,
                custom_flag="keep",
            )
            state = types.SimpleNamespace(updates=[])

            class FakeSiteOper:
                def get_by_domain(self, domain):
                    self.lookup = domain
                    return existing

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))

                def add(self, **_payload):
                    raise AssertionError("existing rows must not use add")

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = self._active_plugin(module)
            indexer = {
                "name": "Nyaa renamed",
                "domain": "jackett_extend.nyaa",
                "public": False,
                "proxy": False,
            }

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__register_site(indexer, generation=1)

            self.assertTrue(result)
            self.assertEqual(
                state.updates,
                [(41, {
                    "name": "Nyaa renamed",
                    "url": "https://jackett_extend.nyaa/",
                    "public": 0,
                })],
            )
            self.assertEqual(existing.is_active, False)
            self.assertEqual(existing.pri, 99)
            self.assertEqual(existing.proxy, 1)
            self.assertEqual(existing.custom_flag, "keep")
            self.assertEqual(
                events.calls,
                [("SiteUpdated", {"domain": "jackett_extend.nyaa"})],
            )

    def test_register_site_add_conflict_rechecks_and_updates(self):
        with loaded_module() as module:
            existing = types.SimpleNamespace(id=7, domain="jackett_extend.nyaa")
            state = types.SimpleNamespace(adds=[], lookups=[], updates=[])

            class FakeSiteOper:
                def get_by_domain(self, domain):
                    state.lookups.append(domain)
                    return None if len(state.lookups) == 1 else existing

                def add(self, **payload):
                    state.adds.append(payload)
                    raise RuntimeError("duplicate")

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = self._active_plugin(module)

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__register_site({
                    "name": "Nyaa",
                    "domain": "jackett_extend.nyaa",
                    "public": True,
                    "proxy": False,
                }, generation=1)

            self.assertTrue(result)
            self.assertEqual(len(state.adds), 1)
            self.assertEqual(
                state.updates,
                [(7, {
                    "name": "Nyaa",
                    "url": "https://jackett_extend.nyaa/",
                    "public": 1,
                })],
            )
            self.assertEqual(state.lookups, ["jackett_extend.nyaa", "jackett_extend.nyaa"])
            self.assertEqual(
                events.calls,
                [("SiteUpdated", {"domain": "jackett_extend.nyaa"})],
            )

    def test_register_site_add_false_contract_rechecks_and_updates(self):
        with loaded_module() as module:
            existing = types.SimpleNamespace(id=17, domain="jackett_extend.nyaa")
            state = types.SimpleNamespace(adds=[], lookups=[], updates=[])

            class FakeSiteOper:
                def get_by_domain(self, domain):
                    state.lookups.append(domain)
                    return None if len(state.lookups) == 1 else existing

                def add(self, **payload):
                    state.adds.append(payload)
                    return False, "站点已存在"

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = self._active_plugin(module)

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__register_site({
                    "name": "Nyaa",
                    "domain": "jackett_extend.nyaa",
                    "public": True,
                    "proxy": False,
                }, generation=1)

            self.assertTrue(result)
            self.assertEqual(len(state.adds), 1)
            self.assertEqual(state.lookups, ["jackett_extend.nyaa", "jackett_extend.nyaa"])
            self.assertEqual(
                state.updates,
                [(17, {
                    "name": "Nyaa",
                    "url": "https://jackett_extend.nyaa/",
                    "public": 1,
                })],
            )
            self.assertEqual(
                events.calls,
                [("SiteUpdated", {"domain": "jackett_extend.nyaa"})],
            )

    def test_register_site_event_failure_reports_false_after_db_success(self):
        with loaded_module() as module:
            state = types.SimpleNamespace(adds=[])

            class FakeSiteOper:
                def get_by_domain(self, _domain):
                    return None

                def add(self, **payload):
                    state.adds.append(payload)

            eventmanager, events = self._event_manager(fail=True)
            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = self._active_plugin(module)

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__register_site({
                    "name": "Nyaa",
                    "domain": "jackett_extend.nyaa",
                    "public": True,
                }, generation=1)

            self.assertFalse(result)
            self.assertEqual(len(state.adds), 1)
            self.assertEqual(
                events.calls,
                [("SiteUpdated", {"domain": "jackett_extend.nyaa"})],
            )

    def test_remove_stale_sites_deletes_only_virtual_rows_and_emits_deleted(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="jackett_extend.old"),
                types.SimpleNamespace(id=2, domain="ordinary.example"),
                types.SimpleNamespace(id=3, domain="jackett_extend.keep"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return records

                def delete(self, site_id):
                    state.deleted.append(site_id)

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = self._active_plugin(module)
            plugin._sync_ready = True

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__sync_remove_stale_sites(
                    [{"domain": "jackett_extend.keep"}],
                    generation=1,
                )

            self.assertTrue(result)
            self.assertEqual(state.deleted, [1])
            self.assertEqual(
                events.calls,
                [("SiteDeleted", {"site_id": 1})],
            )

    def test_remove_stale_sites_event_failure_makes_stage_fail(self):
        with loaded_module() as module:
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [types.SimpleNamespace(id=1, domain="jackett_extend.old")]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            eventmanager, events = self._event_manager(fail=True)
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = self._active_plugin(module)
            plugin._sync_ready = True

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__sync_remove_stale_sites(
                    [{"domain": "jackett_extend.keep"}],
                    generation=1,
                )

            self.assertFalse(result)
            self.assertEqual(state.deleted, [1])
            self.assertEqual(
                events.calls,
                [("SiteDeleted", {"site_id": 1})],
            )

    def test_remove_stale_sites_stops_after_generation_invalidates(self):
        with loaded_module() as module:
            state = types.SimpleNamespace(deleted=[])
            plugin = self._active_plugin(module)

            class FakeSiteOper:
                def list(self):
                    return [
                        types.SimpleNamespace(id=1, domain="jackett_extend.first"),
                        types.SimpleNamespace(id=2, domain="jackett_extend.second"),
                    ]

                def delete(self, site_id):
                    state.deleted.append(site_id)
                    with plugin._state_lock:
                        plugin._sync_generation += 1

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin._sync_ready = True

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                result = plugin._JackettExtend__sync_remove_stale_sites(
                    [{"domain": "jackett_extend.keep"}],
                    generation=1,
                )

            self.assertFalse(result)
            self.assertEqual(state.deleted, [1])
            self.assertEqual(
                events.calls,
                [("SiteDeleted", {"site_id": 1})],
            )

    def test_lifecycle_cleanup_deletes_only_jackett_sites_and_is_idempotent(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="jackett_extend.old"),
                types.SimpleNamespace(id=2, domain="ordinary.example"),
                types.SimpleNamespace(id=3, domain="prowlarr_extend.7"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.JackettExtend)

            with site_oper_modules(
                FakeSiteOper,
                eventmanager=eventmanager,
                event_type=event_type,
            ):
                self.assertTrue(plugin._JackettExtend__remove_managed_sites())
                self.assertTrue(plugin._JackettExtend__remove_managed_sites())

            self.assertEqual(state.deleted, [1])
            self.assertEqual(
                events.calls,
                [("SiteDeleted", {"site_id": 1})],
            )

    def test_lifecycle_cleanup_contains_row_and_event_failures(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="jackett_extend.first"),
                types.SimpleNamespace(id=2, domain="jackett_extend.second"),
                types.SimpleNamespace(id=3, domain="jackett_extend.third"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return records

                def delete(self, site_id):
                    if site_id == 2:
                        raise RuntimeError("delete unavailable")
                    state.deleted.append(site_id)

            events = types.SimpleNamespace(calls=[])

            class EventManager:
                def send_event(self, event, payload):
                    events.calls.append((event, payload))
                    if payload["site_id"] == 3:
                        raise RuntimeError("event unavailable")

            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.JackettExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                result = plugin._JackettExtend__remove_managed_sites()

            self.assertFalse(result)
            self.assertEqual(state.deleted, [1, 3])
            self.assertEqual(events.calls, [
                ("SiteDeleted", {"site_id": 1}),
                ("SiteDeleted", {"site_id": 3}),
            ])

    def test_disabled_init_cleans_sites_without_network_sync(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="jackett_extend.nyaa"),
                types.SimpleNamespace(id=2, domain="ordinary.example"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.JackettExtend)

            with site_oper_modules(FakeSiteOper, eventmanager, event_type):
                plugin.init_plugin({"enabled": False})

            self.assertFalse(plugin.get_state())
            self.assertEqual(state.deleted, [1])
            self.assertEqual(events.calls, [
                ("SiteDeleted", {"site_id": 1}),
            ])

    def test_stop_service_quiesces_without_deleting_sites(self):
        with loaded_module() as module:
            records = [types.SimpleNamespace(id=1, domain="jackett_extend.nyaa")]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            eventmanager, events = self._event_manager()
            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.JackettExtend)
            plugin._enabled = True

            with site_oper_modules(FakeSiteOper, eventmanager, event_type):
                result = plugin.stop_service()

            self.assertTrue(result)
            self.assertFalse(plugin.get_state())
            self.assertEqual(state.deleted, [])
            self.assertEqual(events.calls, [])

    def test_stop_service_waits_for_commit_lock_before_returning(self):
        with loaded_module() as module:
            plugin = object.__new__(module.JackettExtend)
            plugin._enabled = True
            plugin._sync_stop_event = threading.Event()
            commit_entered = threading.Event()
            release_commit = threading.Event()

            def hold_commit_lock():
                with plugin._sync_lock:
                    commit_entered.set()
                    release_commit.wait(2)

            commit_worker = threading.Thread(target=hold_commit_lock)
            commit_worker.start()
            self.assertTrue(commit_entered.wait(2))
            stop_returned = threading.Event()
            stop_worker = threading.Thread(
                target=lambda: (plugin.stop_service(), stop_returned.set()),
            )
            stop_worker.start()
            try:
                self.assertFalse(stop_returned.wait(0.05))
            finally:
                release_commit.set()
            self.assertTrue(stop_returned.wait(2))
            commit_worker.join(2)
            stop_worker.join(2)
            self.assertFalse(commit_worker.is_alive())
            self.assertFalse(stop_worker.is_alive())

    def test_host_stop_then_init_preserves_site_identity_and_user_fields(self):
        with loaded_module() as module:
            class FakeCronTrigger:
                @classmethod
                def from_crontab(cls, _expression, timezone=None):
                    return cls()

            record = types.SimpleNamespace(
                id=91,
                domain="jackett_extend.nyaa",
                name="Old name",
                pri=7,
                is_active=False,
                downloader="keep-downloader",
                proxy=1,
                references={"search": [91], "subscribe": [91]},
            )
            state = types.SimpleNamespace(deleted=[], updates=[])

            class FakeSiteOper:
                def list(self):
                    return [record]

                def get_by_domain(self, _domain):
                    return record

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))
                    for key, value in payload.items():
                        setattr(record, key, value)

            class EventManager:
                def send_event(self, _event, _payload):
                    return None

            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            module.CronTrigger = FakeCronTrigger
            old_plugin = object.__new__(module.JackettExtend)
            old_plugin._enabled = True
            old_plugin._sync_stop_event = threading.Event()
            old_plugin._sync_generation = 3
            new_plugin = object.__new__(module.JackettExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                old_plugin.stop_service()
                new_plugin.init_plugin({
                    "enabled": True,
                    "host": "https://jackett.invalid",
                    "api_key": "key",
                })
                self.assertTrue(new_plugin._JackettExtend__register_site({
                    "name": "New name",
                    "domain": "jackett_extend.nyaa",
                    "public": True,
                    "proxy": False,
                }, generation=new_plugin._sync_generation))

            self.assertEqual(state.deleted, [])
            self.assertEqual(record.id, 91)
            self.assertEqual(record.pri, 7)
            self.assertFalse(record.is_active)
            self.assertEqual(record.downloader, "keep-downloader")
            self.assertEqual(record.proxy, 1)
            self.assertEqual(record.references, {"search": [91], "subscribe": [91]})
            self.assertEqual(state.updates, [(
                91,
                {
                    "name": "New name",
                    "url": "https://jackett_extend.nyaa/",
                    "public": 1,
                },
            )])

    def test_reload_replaces_generation_event_without_plugin_owned_worker(self):
        with loaded_module() as module:
            class FakeCronTrigger:
                @classmethod
                def from_crontab(cls, expression, timezone=None):
                    return cls()

            module.CronTrigger = FakeCronTrigger
            plugin = object.__new__(module.JackettExtend)
            cleanup_calls = []
            plugin._JackettExtend__remove_managed_sites = lambda: cleanup_calls.append(True)

            plugin.init_plugin({
                "enabled": True,
                "host": "https://first.invalid",
                "api_key": "first-key",
            })
            first_event = plugin._sync_stop_event
            first_generation = plugin._sync_generation
            first_services = plugin.get_service()
            self.assertEqual(first_services[0]["trigger"], "date")
            self.assertEqual(first_services[0]["func_kwargs"], {"generation": first_generation})
            self.assertNotIn("_sync_thread", plugin.__dict__)

            plugin.init_plugin({
                "enabled": True,
                "host": "https://second.invalid",
                "api_key": "second-key",
            })
            second_event = plugin._sync_stop_event
            second_generation = plugin._sync_generation

            self.assertTrue(first_event.is_set())
            self.assertIsNot(first_event, second_event)
            self.assertGreaterEqual(second_generation, first_generation + 2)
            second_services = plugin.get_service()
            self.assertEqual(second_services[0]["trigger"], "date")
            self.assertEqual(second_services[0]["func_kwargs"], {"generation": second_generation})
            self.assertNotIn("_sync_thread", plugin.__dict__)
            self.assertEqual(cleanup_calls, [])

            plugin.stop_service()
            self.assertTrue(second_event.is_set())
            self.assertEqual(cleanup_calls, [])


if __name__ == "__main__":
    unittest.main()
