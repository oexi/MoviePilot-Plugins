"""Current MoviePilot V3 contracts for the Prowlarr extension.

These tests import the plugin through the production ``app.plugins`` path.
Boundary doubles only replace outbound responses and local persistence while
the pure indexer/Torznab/UI helpers are tested independently.
"""

import asyncio
import codecs
import json
import threading
import types
import unittest
import xml.dom.minidom
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.parsers.expat import ExpatError

from importlib import import_module

import requests
from urllib3.exceptions import ReadTimeoutError


ROOT = Path(__file__).resolve().parents[3]


class _Logger:
    messages = []

    def __getattr__(self, _name):
        return lambda *args, **kwargs: self.messages.append(" ".join(map(str, args)))


class _MediaType:
    MUSIC = types.SimpleNamespace(value="音乐", name="MUSIC")


class _MediaSource:
    IMDb = "imdb"


class _TorrentInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StringUtils:
    clear_calls = []

    @staticmethod
    def clear(text, replace_word="", allow_space=False):
        _StringUtils.clear_calls.append((text, replace_word, allow_space))
        if text == "Test123, S!":
            return "Test123 S"
        if text == "落第賢者の学院無双 ~二度目の転生、Sランクチート魔術師冒険録~":
            return "落第賢者の学院無双 二度目の転生 Sランクチート魔術師冒険録"
        return text

    @staticmethod
    def unify_datetime_str(value):
        return value

    @staticmethod
    def get_url_domain(value):
        return value


class _CronTrigger:
    def __init__(self, expression, timezone=None):
        self.expression = expression
        self.timezone = timezone

    @classmethod
    def from_crontab(cls, expression, timezone=None):
        return cls(expression, timezone)


class _Response:
    def __init__(self, status_code=200, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text
        self._wire_content = text.encode("utf-8")
        self._payload = payload
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield self._wire_content

    def close(self):
        self.closed = True

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


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


@contextmanager
def loaded_module():
    module = import_module("app.plugins.prowlarrextend")
    patched = {
        "MediaType": _MediaType,
        "MediaSource": _MediaSource,
        "TorrentInfo": _TorrentInfo,
        "StringUtils": _StringUtils,
        "CronTrigger": _CronTrigger,
        "logger": _Logger(),
        "settings": types.SimpleNamespace(
            PROXY={"http": "http://proxy.invalid"},
            TZ="UTC",
            USER_AGENT="test",
        ),
        "asyncio": types.SimpleNamespace(to_thread=asyncio.to_thread),
    }
    previous = {name: getattr(module, name) for name in patched}
    try:
        for name, value in patched.items():
            setattr(module, name, value)
        yield module
    finally:
        _uninstall_loaded_plugin_bridge(module)
        for name, value in previous.items():
            setattr(module, name, value)


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
def site_oper_modules(site_oper, eventmanager, event_type):
    """在真实宿主模块边界替换 Oper 与事件端口，并精确恢复属性。"""
    from app.db.oper import site as site_module
    from app.sdk import events as events_module
    from app.schemas import types as schema_types

    previous_site_oper = site_module.SiteOper
    previous_eventmanager = events_module.eventmanager
    previous_event_type = schema_types.EventType
    site_module.SiteOper = site_oper
    events_module.eventmanager = eventmanager
    schema_types.EventType = event_type
    try:
        yield
    finally:
        site_module.SiteOper = previous_site_oper
        events_module.eventmanager = previous_eventmanager
        schema_types.EventType = previous_event_type


class ProwlarrV3ContractTest(unittest.TestCase):
    def test_host_normalization_rejects_non_http_explicit_schemes(self):
        with loaded_module() as module:
            normalize = module.ProwlarrExtend._normalize_host
            self.assertEqual(normalize("prowlarr:9696"), "http://prowlarr:9696")
            self.assertEqual(normalize("https://prowlarr:9696/base/"), "https://prowlarr:9696/base")
            self.assertEqual(normalize("ftp://prowlarr:9696"), "")
            self.assertEqual(normalize("http://user:pass@prowlarr:9696"), "")

    def test_metadata_module_and_async_search_contract(self):
        manifest = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
        with loaded_module() as module:
            self.assertEqual(manifest["ProwlarrExtend"]["version"], module.ProwlarrExtend.plugin_version)
            self.assertEqual(manifest["ProwlarrExtend"]["icon"], "Prowlarr.png")
            self.assertEqual(manifest["ProwlarrExtend"]["author"], "oexi")
            self.assertEqual(manifest["JackettExtend"]["version"], "3.2.21")
        with loaded_module() as module:
            self.assertEqual(module.ProwlarrExtend.plugin_icon, "Prowlarr.png")
            self.assertEqual(module.ProwlarrExtend.plugin_author, "oexi")
            self.assertEqual(module.ProwlarrExtend.plugin_version, manifest["ProwlarrExtend"]["version"])
            self.assertEqual(module.ProwlarrExtend.plugin_config_prefix, "prowlarr_extend_")

            plugin = object.__new__(module.ProwlarrExtend)
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
            result = asyncio.run(plugin.async_search_torrents(
                site={"domain": "prowlarr_extend.7"}, keyword="x"
            ))

            self.assertEqual(result, ["ok"])
            self.assertEqual(thread_calls, [search])
            self.assertEqual(calls[0]["site"]["domain"], "prowlarr_extend.7")
            self.assertIn("async_search_torrents", plugin.get_module())

    def test_fetch_uses_read_only_v1_endpoint_header_auth_and_filters_resources(self):
        with loaded_module() as module:
            raw = [
                {
                    "id": 7,
                    "name": "Torrent Alpha",
                    "enable": True,
                    "protocol": "torrent",
                    "supportsSearch": True,
                    "privacy": "public",
                    "capabilities": {"categories": [{"id": 2000, "name": "Movies"}]},
                },
                {"id": 8, "name": "Disabled", "enable": False, "protocol": "torrent", "supportsSearch": True},
                {"id": 9, "name": "Usenet", "enable": True, "protocol": "usenet", "supportsSearch": True},
                {"id": 10, "name": "No search", "enable": True, "protocol": "torrent", "supportsSearch": False},
            ]
            response = _Response(
                headers={"Content-Type": "application/json; charset=utf-8"},
                text=json.dumps(raw),
                payload=raw,
            )
            calls = []

            class Request:
                def __init__(self, *args, **kwargs):
                    calls.append(("init", kwargs))

                def get_res(self, url, **kwargs):
                    calls.append(("get", url, kwargs))
                    return response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            result = plugin._ProwlarrExtend__fetch_indexers({
                "host": "https://prowlarr.invalid/",
                "api_key": "not-a-real-key",
                "proxy": True,
                "timeout": 17,
            })

            self.assertEqual([item["indexer_id"] for item in result], ["7"])
            self.assertEqual(calls[1][1], "https://prowlarr.invalid/api/v1/indexer")
            self.assertEqual(calls[0][1]["timeout"], 17)
            self.assertEqual(calls[0][1]["headers"]["X-Api-Key"], "not-a-real-key")
            self.assertEqual(calls[1][2]["proxies"], module.settings.PROXY if hasattr(module, "settings") else {"http": "http://proxy.invalid"})
            self.assertTrue(calls[1][2]["raise_exception"])

    def test_fetch_http_json_empty_and_bounds_fail_closed(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            snapshot = {"host": "https://prowlarr.invalid", "api_key": "key", "timeout": 30, "proxy": False}

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *args, **kwargs):
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            cases = [
                _Response(status_code=401, text="{}", payload=[]),
                _Response(headers={"Content-Type": "text/plain"}, text="[]", payload=[]),
                _Response(headers={"Content-Type": "application/json"}, text="not-json", payload=ValueError()),
                _Response(headers={"Content-Type": "application/json"}, text="{}", payload={}),
                None,
            ]
            for response in cases:
                with self.subTest(response=response):
                    Request.response = response
                    self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers(snapshot))

            class TimeoutRequest(Request):
                def get_res(self, *args, **kwargs):
                    import requests
                    raise requests.Timeout("network timeout")

            module.RequestUtils = TimeoutRequest
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers(snapshot))
            self.assertEqual(plugin._last_error, "timeout")

            oversized = _Response(
                headers={"Content-Type": "application/json"},
                text="x" * (module.ProwlarrExtend.REST_MAX_JSON_BYTES + 1),
                payload=[],
            )
            Request.response = oversized
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers(snapshot))

            plugin.REST_MAX_ITEMS = 1
            too_many = [{"id": 1}, {"id": 2}]
            Request.response = _Response(
                headers={"Content-Type": "application/json"},
                text=json.dumps(too_many),
                payload=too_many,
            )
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers(snapshot))

    def test_search_uses_numeric_newznab_route_safe_query_and_v3_fields(self):
        with loaded_module() as module:
            xml = """<?xml version='1.0'?><rss><channel>
              <item><title>Release</title><guid>guid-1</guid>
                <comments>https://tracker.invalid/release</comments>
                <enclosure url='https://tracker.invalid/release.torrent'/><size>42</size>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='seeders' value='3'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='peers' value='4'/>
                <torznab:attr xmlns:torznab='http://torznab.com/schemas/2015/feed' name='infohash' value='abc'/>
              </item></channel></rss>"""
            response = _Response(
                headers={"Content-Type": "application/rss+xml"},
                text=xml,
            )
            calls = []

            class Request:
                def __init__(self, *args, **kwargs):
                    calls.append(("init", kwargs))

                def get_res(self, url, **kwargs):
                    calls.append(("get", url, kwargs))
                    return response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            }
            results = plugin.search_torrents(
                {"id": 4, "name": "Alpha", "domain": "prowlarr_extend.7"},
                keyword="A&B / secret",
                cat="2000, 5010",
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Release")
            self.assertEqual(results[0].size, 42.0)
            self.assertEqual(results[0].seeders, 3)
            self.assertEqual(results[0].site, 4)
            self.assertEqual(calls[1][1].split("?")[0], "https://prowlarr.invalid/api/v1/indexer/7/newznab")
            query = parse_qs(urlparse(calls[1][1]).query)
            self.assertEqual(query["q"], ["A&B / secret"])
            self.assertEqual(query["cat"], ["2000,5010"])
            self.assertNotIn("apikey", calls[1][1].lower())
            self.assertEqual(calls[0][1]["headers"]["X-Api-Key"], "not-a-real-key")
            self.assertTrue(calls[1][2]["raise_exception"])

            calls.clear()
            self.assertEqual(plugin.search_torrents(
                {"name": "bad", "domain": "prowlarr_extend.0"}, keyword="x"
            ), [])
            self.assertEqual(calls, [])

    def test_search_nfkc_normalizes_full_width_keyword_before_cleaning_and_query(self):
        _StringUtils.clear_calls.clear()
        with loaded_module() as module:
            calls = []

            class Request:
                def __init__(self, *args, **kwargs):
                    calls.append(("init", kwargs))

                def get_res(self, url, **kwargs):
                    calls.append(("get", url, kwargs))
                    return _Response(
                        headers={"Content-Type": "application/rss+xml"},
                        text="<?xml version='1.0'?><rss><channel /></rss>",
                    )

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            }
            plugin.search_torrents(
                {"id": 4, "name": "Alpha", "domain": "prowlarr_extend.7"},
                keyword="Ｔｅｓｔ１２３， Ｓ！",
            )

            query = parse_qs(urlparse(calls[1][1]).query)
            self.assertEqual(query["q"], ["Test123 S"])
            self.assertEqual(
                _StringUtils.clear_calls,
                [("Test123, S!", " ", True)],
            )

            calls.clear()
            title = "落第賢者の学院無双 ～二度目の転生、Ｓランクチート魔術師冒険録～"
            expected = "落第賢者の学院無双 二度目の転生 Sランクチート魔術師冒険録"
            plugin.search_torrents(
                {"id": 4, "name": "Alpha", "domain": "prowlarr_extend.7"},
                keyword=title,
            )
            query = parse_qs(urlparse(calls[1][1]).query)
            self.assertEqual(query["q"], [expected])

    def test_search_keeps_normalized_chinese_and_ascii_keyword_unchanged(self):
        _StringUtils.clear_calls.clear()
        with loaded_module() as module:
            calls = []

            class Request:
                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, url, **kwargs):
                    calls.append(url)
                    return _Response(
                        headers={"Content-Type": "application/rss+xml"},
                        text="<?xml version='1.0'?><rss><channel /></rss>",
                    )

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            }
            keywords = ("落第賢者の学院無双 Sランク", "ASCII 123")
            for keyword in keywords:
                plugin.search_torrents(
                    {"id": 4, "name": "Alpha", "domain": "prowlarr_extend.7"},
                    keyword=keyword,
                )

            self.assertEqual(
                [parse_qs(urlparse(url).query)["q"][0] for url in calls],
                list(keywords),
            )

    def test_yts_numeric_imdb_attr_reaches_moviepilot_identity_fast_path(self):
        with loaded_module() as module:
            fixture = (ROOT / "tests" / "fixtures" / "prowlarr_extend_yts.xml").read_text(
                encoding="utf-8"
            )
            response = _Response(
                headers={"Content-Type": "application/rss+xml"},
                text=fixture,
            )

            class Request:
                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            }

            results = plugin.search_torrents(
                {"id": 4, "name": "Sample Prowlarr", "domain": "prowlarr_extend.7"},
                keyword="Sample Film",
                mtype=types.SimpleNamespace(value="电影", name="MOVIE"),
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].media_source, module.MediaSource.IMDb)
            self.assertEqual(results[0].media_id, "tt0123456")
            self.assertEqual(results[0].category, "电影")
            self.assertEqual(results[0].size, 734003200.0)

            # Model the current MoviePilot SearchChain identity branch.  Keep
            # the title fallback deliberately false: a canonical IMDb ID must
            # still admit the release when title parsing cannot do so.
            target_media_source = module.MediaSource.IMDb
            target_media_id = "tt0123456"
            title_fallback_matches = "Different Target" in results[0].title
            identity_fast_path_matches = bool(
                results[0].media_source == target_media_source
                and target_media_id
                and results[0].media_id == target_media_id
            )
            self.assertFalse(title_fallback_matches)
            self.assertTrue(identity_fast_path_matches)

    def test_empty_site_browse_has_bounded_flaresolverr_budget(self):
        with loaded_module() as module:
            timeouts = []

            class Request:
                def __init__(self, *args, **kwargs):
                    timeouts.append(kwargs.get("timeout"))

                def get_res(self, *_args, **_kwargs):
                    return _Response(
                        headers={"Content-Type": "application/rss+xml"},
                        text="<?xml version='1.0'?><rss><channel /></rss>",
                    )

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            }
            site = {"id": 4, "name": "Sample Prowlarr", "domain": "prowlarr_extend.7"}

            result = asyncio.run(plugin.async_refresh_torrents(site=site, keyword=None))

            self.assertEqual(result, [])
            self.assertEqual(timeouts, [module.ProwlarrExtend.BROWSE_TIMEOUT_MIN])

            timeouts.clear()
            plugin.search_torrents(site=site, keyword="Sample Film")
            self.assertEqual(timeouts, [12])

    def test_empty_refresh_propagates_sanitized_http_429_only(self):
        with loaded_module() as module:
            response = _Response(
                status_code=429,
                headers={
                    "Content-Type": "application/rss+xml",
                    "Retry-After": "sensitive-upstream-value",
                },
                text="<?xml version='1.0'?><rss><channel /></rss>",
            )

            class Request:
                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            site = {"id": 4, "name": "Sample Prowlarr", "domain": "prowlarr_extend.7"}

            # Ordinary search stays fail-closed, including an empty keyword
            # supplied through the generic search route.
            self.assertEqual(plugin.search_torrents(site=site, keyword="title"), [])
            self.assertEqual(plugin.search_torrents(site=site, keyword=None), [])

            with self.assertRaises(module._host_compat.SanitizedUpstreamError) as raised:
                plugin.refresh_torrents(site=site, keyword=None)
            self.assertEqual(raised.exception.category, "http_429")
            self.assertNotIn("sensitive-upstream-value", str(raised.exception))

            with self.assertRaises(module._host_compat.SanitizedUpstreamError):
                asyncio.run(plugin.async_refresh_torrents(site=site, keyword=None))

    def test_streaming_rest_and_xml_reads_never_touch_buffered_body(self):
        with loaded_module() as module:
            xml = (
                '<?xml version="1.0" encoding="UTF-16"?><rss><channel>'
                "<item><title>流式标题</title>"
                "<enclosure url='https://site/stream.torrent'/></item>"
                "</channel></rss>"
            ).encode("utf-16")
            xml_response = _GuardedResponse(
                (xml[:5], xml[5:]),
                headers={"Content-Type": "application/rss+xml"},
            )
            xml_calls = []

            class XmlRequest:
                def __init__(self, *args, **kwargs):
                    xml_calls.append(kwargs)

                def get_stream(self, *args, **kwargs):
                    xml_calls.append(kwargs)
                    return _stream_response(xml_response)

            module.RequestUtils = XmlRequest
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": True,
                "timeout": 12,
            }
            results = plugin._ProwlarrExtend__parse_torznab_xml(
                "https://prowlarr.invalid/results",
                site={"name": "流式站点"},
                keyword="title",
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "流式标题")
            self.assertTrue(xml_response.closed)
            self.assertEqual(xml_response.iterated_chunks, 2)
            self.assertEqual(xml_calls[0]["headers"]["X-Api-Key"], "not-a-real-key")
            self.assertTrue(xml_calls[1]["raise_exception"])

            payload = json.dumps([{
                "id": 7,
                "name": "Torrent Alpha",
                "enable": True,
                "protocol": "torrent",
                "supportsSearch": True,
                "privacy": "public",
                "capabilities": {"categories": []},
            }]).encode("utf-8")
            rest_response = _GuardedResponse(
                (payload[:3], payload[3:]),
                headers={"Content-Type": "application/json"},
            )
            rest_calls = []

            class RestRequest:
                def __init__(self, *args, **kwargs):
                    rest_calls.append(kwargs)

                def get_stream(self, *args, **kwargs):
                    rest_calls.append(kwargs)
                    return _stream_response(rest_response)

            module.RequestUtils = RestRequest
            fetched = plugin._ProwlarrExtend__fetch_indexers({
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 12,
            })

            self.assertEqual([item["indexer_id"] for item in fetched], ["7"])
            self.assertTrue(rest_response.closed)
            self.assertEqual(rest_response.iterated_chunks, 2)

    def test_streaming_limits_and_read_errors_close_responses(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            plugin._state_lock = module.ProwlarrExtend._state_lock
            plugin.TORZNAB_MAX_XML_BYTES = 8
            xml_url = "https://prowlarr.invalid/results?q=title"

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
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "xml_too_large")
            self.assertTrue(oversized_xml.closed)
            self.assertEqual(oversized_xml.iterated_chunks, 1)

            rejected_xml = _GuardedResponse(
                (),
                status_code=401,
                headers={"Content-Type": "application/xml"},
                error=AssertionError("status rejection must not read the body"),
            )
            Request.response = rejected_xml
            plugin._last_search_error = None
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "http_401")
            self.assertTrue(rejected_xml.closed)
            self.assertEqual(rejected_xml.iterated_chunks, 0)

            truncated_xml = _GuardedResponse(
                (b"<rss>",),
                headers={"Content-Type": "application/xml"},
                error=requests.exceptions.ChunkedEncodingError("truncated"),
            )
            Request.response = truncated_xml
            plugin._last_search_error = None
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(xml_url), [])
            self.assertEqual(plugin._last_search_error, "request_error")
            self.assertTrue(truncated_xml.closed)

            plugin.REST_MAX_JSON_BYTES = 4
            oversized_json = _GuardedResponse(
                (b"1234", b"5", b"tail"),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
            Request.response = oversized_json
            plugin._last_error = None
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers({
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }))
            self.assertEqual(plugin._last_error, "json_too_large")
            self.assertTrue(oversized_json.closed)
            self.assertEqual(oversized_json.iterated_chunks, 2)

    def test_real_response_read_timeout_is_distinct_and_refresh_stays_sanitized(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            plugin._state_lock = module.ProwlarrExtend._state_lock
            site = {"id": 4, "name": "Sample Prowlarr", "domain": "prowlarr_extend.7"}

            def timeout_xml_stream():
                yield b"<rss>"
                raise ReadTimeoutError(None, "/test", "timed out")

            xml_response, xml_close_calls = _real_stream_response(
                timeout_xml_stream,
                {"Content-Type": "application/xml"},
            )

            class Request:
                response = xml_response

                def __init__(self, *args, **kwargs):
                    pass

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.response)

            module.RequestUtils = Request
            with self.assertRaises(module._host_compat.SanitizedUpstreamError) as raised:
                plugin.refresh_torrents(site=site, keyword=None)
            self.assertEqual(raised.exception.category, "timeout")
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
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers({
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
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
            self.assertEqual(plugin.search_torrents(site=site, keyword="title"), [])
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
            self.assertIsNone(plugin._ProwlarrExtend__fetch_indexers({
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }))
            self.assertEqual(plugin._last_error, "request_error")
            self.assertEqual(chunk_close_calls, [True])

    def test_torznab_http_json_xml_timeout_empty_and_limits_fail_closed(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            url = "https://prowlarr.invalid/api/v1/indexer/7/newznab?q=PrivateTitle"

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                def get_res(self, *_args, **_kwargs):
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            cases = (
                None,
                _Response(status_code=401, headers={"Content-Type": "application/xml"}, text="<error/ >"),
                _Response(headers={"Content-Type": "application/json"}, text='{"error":"denied"}'),
                _Response(headers={"Content-Type": "application/xml"}, text=""),
                _Response(headers={"Content-Type": "application/xml"}, text="<rss>"),
                _Response(headers={"Content-Type": "application/xml"}, text='<error code="410" description="disabled"/>'),
                _Response(headers={"Content-Type": "application/xml"}, text="<!DOCTYPE rss><rss/ >"),
            )
            for response in cases:
                with self.subTest(response=response):
                    Request.response = response
                    self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(url), [])

            Request.response = _Response(
                headers={"Content-Type": "application/xml"},
                text="<rss><channel>" + "x" * (plugin.TORZNAB_MAX_XML_BYTES + 1) + "</channel></rss>",
            )
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(url), [])

            plugin.TORZNAB_MAX_ITEMS = 1
            Request.response = _Response(
                headers={"Content-Type": "application/xml"},
                text=("<rss><channel>"
                      "<item><title>One</title><enclosure url='https://site/one.torrent'/></item>"
                      "<item><title>Two</title><enclosure url='https://site/two.torrent'/></item>"
                      "</channel></rss>"),
            )
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(url), [])

            class TimeoutRequest(Request):
                def get_res(self, *_args, **_kwargs):
                    import requests
                    raise requests.Timeout("sensitive transport detail")

            module.RequestUtils = TimeoutRequest
            self.assertEqual(plugin._ProwlarrExtend__parse_torznab_xml(url), [])
            self.assertEqual(plugin._last_search_error, "timeout")
            rendered_logs = "\n".join(_Logger.messages)
            self.assertNotIn("PrivateTitle", rendered_logs)
            self.assertNotIn("sensitive transport detail", rendered_logs)

    def test_torznab_uses_original_content_when_host_text_is_malformed(self):
        with loaded_module() as module:
            valid_xml = ("<?xml version='1.0'?><rss><channel>"
                         "<item><title>Wire Release</title>"
                         "<enclosure url='https://site/wire.torrent'/>"
                         "</item></channel></rss>").encode("utf-8")
            # Parsing the host-provided text would raise ExpatError, while
            # ``content`` contains the valid original response bytes.
            response = _Response(
                headers={"Content-Type": "application/rss+xml"},
                text="<rss>",
            )
            response._wire_content = valid_xml
            with self.assertRaises(ExpatError):
                xml.dom.minidom.parseString(response.text)

            class Request:
                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }

            results = plugin.search_torrents(
                {"id": 4, "name": "Wire Site", "domain": "prowlarr_extend.7"},
                keyword="Wire Release",
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Wire Release")
            self.assertEqual(results[0].enclosure, "https://site/wire.torrent")

    def test_parser_rejects_dtd_in_all_supported_wire_encodings(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            plugin._last_error = None
            plugin._state_lock = module.ProwlarrExtend._state_lock

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
                                plugin._ProwlarrExtend__parse_torznab_xml(
                                    "https://prowlarr.invalid/results?q=private-title&apikey=secret"
                                ),
                                [],
                            )
                            self.assertEqual(plugin._last_search_error, "xml_doctype")

    def test_parser_preserves_xml_declaration_encoding_for_real_response(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            plugin._last_error = None
            plugin._state_lock = module.ProwlarrExtend._state_lock

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

                    result = plugin._ProwlarrExtend__parse_torznab_xml(
                        "https://prowlarr.invalid/results",
                        site={"name": "Encoding Site"},
                    )

                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0].title, title)
                    self.assertIsNone(plugin._last_search_error)

    def test_parser_clears_shared_page_url_for_distinct_torrents(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._config_snapshot = {
                "host": "https://prowlarr.invalid",
                "api_key": "not-a-real-key",
                "proxy": False,
                "timeout": 9,
            }
            plugin._last_error = None
            plugin._state_lock = module.ProwlarrExtend._state_lock

            class Request:
                response = None

                def __init__(self, *args, **kwargs):
                    pass

                def get_res(self, *_args, **_kwargs):
                    return self.response

                def get_stream(self, *args, **kwargs):
                    return _stream_response(self.get_res(*args, **kwargs))

            module.RequestUtils = Request
            xml = """<?xml version='1.0'?><rss><channel>
              <item><title>Shared A</title><guid>guid-a</guid>
                <comments>https://tracker.example/</comments>
                <enclosure url='https://prowlarr.invalid/a.torrent'/>
              </item>
              <item><title>Shared B</title><guid>guid-b</guid>
                <comments>https://tracker.example/</comments>
                <enclosure url='https://prowlarr.invalid/b.torrent'/>
              </item>
              <item><title>Normal Detail</title><guid>guid-c</guid>
                <comments>https://tracker.example/details/c</comments>
                <enclosure url='https://prowlarr.invalid/c.torrent'/>
              </item>
            </channel></rss>"""
            Request.response = _real_xml_response(xml.encode("utf-8"))

            result = plugin._ProwlarrExtend__parse_torznab_xml(
                "https://prowlarr.invalid/results",
                site={"name": "Shared Detail Site"},
            )

            self.assertEqual(len(result), 3)
            self.assertIsNone(result[0].page_url)
            self.assertIsNone(result[1].page_url)
            self.assertEqual(result[2].page_url, "https://tracker.example/details/c")
            self.assertEqual(
                [item.enclosure for item in result[:2]],
                [
                    "https://prowlarr.invalid/a.torrent",
                    "https://prowlarr.invalid/b.torrent",
                ],
            )

    def test_service_config_and_diagnostics_are_current_v3_contracts(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._enabled = True
            plugin._cron = "*/15 * * * *"
            plugin._sync_generation = 3
            plugin._ProwlarrExtend__sync_all = lambda generation=None: generation
            services = plugin.get_service()
            self.assertEqual(len(services), 2)
            initial, recurring = services
            self.assertEqual(initial["id"], "prowlarr_extend_sync_initial")
            self.assertEqual(initial["trigger"], "date")
            self.assertEqual(initial["func_kwargs"], {"generation": 3})
            self.assertIn("run_date", initial["kwargs"])
            self.assertTrue(initial["kwargs"]["run_date"].tzinfo)
            self.assertEqual(recurring["id"], "prowlarr_extend_sync")
            self.assertEqual(recurring["func_kwargs"], {"generation": 3})
            self.assertEqual(recurring["kwargs"]["max_instances"], 1)
            self.assertEqual(recurring["kwargs"]["coalesce"], True)
            self.assertNotIn("_sync_thread", plugin.__dict__)

            plugin._host = "https://user:secret@prowlarr.invalid"
            plugin._api_key = "not-a-real-key"
            plugin._indexers = []
            plugin._authoritative_indexers = []
            plugin._fetch_ok = False
            plugin._sync_ready = False
            plugin._last_sync_ok = False
            plugin._last_error = "timeout"
            plugin._last_error_at = 1
            payload = plugin.api_status()
            rendered = repr(payload)
            self.assertNotIn("not-a-real-key", rendered)
            self.assertNotIn("user:", rendered)
            self.assertNotIn("host", payload.model_dump())
            self.assertEqual(payload.last_error, "timeout")

            plugin._enabled = False
            self.assertEqual(plugin.get_service(), [])

    def test_old_form_request_cannot_publish_cache_after_reload(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 7
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
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

            plugin._ProwlarrExtend__fetch_indexers = delayed_fetch
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
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 11
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
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

            plugin._ProwlarrExtend__fetch_indexers = delayed_failure
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
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 15
            plugin._config_snapshot = {
                "host": "https://old.invalid",
                "api_key": "old-key",
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
                return [{"id": "old", "domain": "prowlarr_extend.old"}]

            plugin._ProwlarrExtend__fetch_indexers = delayed_fetch
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
                plugin._indexers = [{"id": "new", "domain": "prowlarr_extend.new"}]
                plugin._fetch_ok = True

            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(
                plugin._indexers,
                [{"id": "new", "domain": "prowlarr_extend.new"}],
            )
            self.assertTrue(plugin._fetch_ok)
            self.assertEqual(result, [[]])

    def test_register_site_add_false_contract_rechecks_and_updates(self):
        with loaded_module() as module:
            existing = types.SimpleNamespace(id=17, domain="prowlarr_extend.7")
            state = types.SimpleNamespace(adds=[], lookups=[], updates=[], events=[])

            class FakeSiteOper:
                def get_by_domain(self, domain):
                    state.lookups.append(domain)
                    return None if len(state.lookups) == 1 else existing

                def add(self, **payload):
                    state.adds.append(payload)
                    return False, "站点已存在"

                def update(self, site_id, payload):
                    state.updates.append((site_id, payload))

            class EventManager:
                def send_event(self, event, payload):
                    state.events.append((event, payload))

            event_type = types.SimpleNamespace(SiteUpdated="SiteUpdated")
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._sync_stop_event = threading.Event()
            plugin._sync_generation = 1

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                result = plugin._ProwlarrExtend__register_site({
                    "name": "Indexer 7",
                    "domain": "prowlarr_extend.7",
                    "public": True,
                    "proxy": False,
                }, generation=1)

            self.assertTrue(result)
            self.assertEqual(len(state.adds), 1)
            self.assertEqual(state.lookups, ["prowlarr_extend.7", "prowlarr_extend.7"])
            self.assertEqual(
                state.updates,
                [(17, {
                    "name": "Indexer 7",
                    "url": "https://prowlarr_extend.7/",
                    "public": 1,
                })],
            )
            self.assertEqual(
                state.events,
                [("SiteUpdated", {"domain": "prowlarr_extend.7"})],
            )

    def test_lifecycle_cleanup_deletes_only_prowlarr_sites_and_is_idempotent(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="prowlarr_extend.7"),
                types.SimpleNamespace(id=2, domain="ordinary.example"),
                types.SimpleNamespace(id=3, domain="jackett_extend.old"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            calls = []

            class EventManager:
                def send_event(self, event, payload):
                    calls.append((event, payload))

            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.ProwlarrExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                self.assertTrue(plugin._ProwlarrExtend__remove_managed_sites())
                self.assertTrue(plugin._ProwlarrExtend__remove_managed_sites())

            self.assertEqual(state.deleted, [1])
            self.assertEqual(calls, [("SiteDeleted", {"site_id": 1})])

    def test_lifecycle_cleanup_contains_row_and_event_failures(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="prowlarr_extend.7"),
                types.SimpleNamespace(id=2, domain="prowlarr_extend.8"),
                types.SimpleNamespace(id=3, domain="prowlarr_extend.9"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return records

                def delete(self, site_id):
                    if site_id == 2:
                        raise RuntimeError("delete unavailable")
                    state.deleted.append(site_id)

            calls = []

            class EventManager:
                def send_event(self, event, payload):
                    calls.append((event, payload))
                    if payload["site_id"] == 3:
                        raise RuntimeError("event unavailable")

            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.ProwlarrExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                result = plugin._ProwlarrExtend__remove_managed_sites()

            self.assertFalse(result)
            self.assertEqual(state.deleted, [1, 3])
            self.assertEqual(calls, [
                ("SiteDeleted", {"site_id": 1}),
                ("SiteDeleted", {"site_id": 3}),
            ])

    def test_disabled_init_cleans_sites_without_network_sync(self):
        with loaded_module() as module:
            records = [
                types.SimpleNamespace(id=1, domain="prowlarr_extend.7"),
                types.SimpleNamespace(id=2, domain="ordinary.example"),
            ]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            calls = []

            class EventManager:
                def send_event(self, event, payload):
                    calls.append((event, payload))

            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.ProwlarrExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                plugin.init_plugin({"enabled": False})

            self.assertFalse(plugin.get_state())
            self.assertEqual(state.deleted, [1])
            self.assertEqual(calls, [
                ("SiteDeleted", {"site_id": 1}),
            ])

    def test_stop_service_quiesces_without_deleting_sites(self):
        with loaded_module() as module:
            records = [types.SimpleNamespace(id=1, domain="prowlarr_extend.7")]
            state = types.SimpleNamespace(deleted=[])

            class FakeSiteOper:
                def list(self):
                    return [record for record in records if record.id not in state.deleted]

                def delete(self, site_id):
                    state.deleted.append(site_id)

            calls = []

            class EventManager:
                def send_event(self, event, payload):
                    calls.append((event, payload))

            event_type = types.SimpleNamespace(SiteDeleted="SiteDeleted")
            plugin = object.__new__(module.ProwlarrExtend)
            plugin._enabled = True

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                result = plugin.stop_service()

            self.assertTrue(result)
            self.assertFalse(plugin.get_state())
            self.assertEqual(state.deleted, [])
            self.assertEqual(calls, [])

    def test_stop_service_waits_for_commit_lock_before_returning(self):
        with loaded_module() as module:
            plugin = object.__new__(module.ProwlarrExtend)
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
                domain="prowlarr_extend.7",
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
            old_plugin = object.__new__(module.ProwlarrExtend)
            old_plugin._enabled = True
            old_plugin._sync_stop_event = threading.Event()
            old_plugin._sync_generation = 3
            new_plugin = object.__new__(module.ProwlarrExtend)

            with site_oper_modules(FakeSiteOper, EventManager(), event_type):
                old_plugin.stop_service()
                new_plugin.init_plugin({
                    "enabled": True,
                    "host": "https://prowlarr.invalid",
                    "api_key": "key",
                })
                services = new_plugin.get_service()
                self.assertEqual(services[0]["trigger"], "date")
                self.assertEqual(
                    services[0]["func_kwargs"],
                    {"generation": new_plugin._sync_generation},
                )
                self.assertNotIn("_sync_thread", new_plugin.__dict__)
                self.assertTrue(new_plugin._ProwlarrExtend__register_site({
                    "name": "New name",
                    "domain": "prowlarr_extend.7",
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
                    "url": "https://prowlarr_extend.7/",
                    "public": 1,
                },
            )])


if __name__ == "__main__":
    unittest.main()
