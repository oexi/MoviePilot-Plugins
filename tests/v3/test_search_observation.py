"""Owned virtual-site pages must publish an outcome to the host's search observer.

MoviePilot v3.1+ discards any site page whose indexer reported nothing and
shows "本页请求未完成，请继续加载重试" in the resource search page.
"""

import threading
from importlib import import_module

import pytest
from app.application.site.observation import capture_site_search_observation

_CASES = (
    ("jackettextend", "JackettExtend", "jackett_extend.nyaa"),
    ("prowlarrextend", "ProwlarrExtend", "prowlarr_extend.7"),
)


def _plugin(module_id: str, class_name: str, parse):
    module = import_module(f"app.plugins.{module_id}")
    plugin = object.__new__(getattr(module, class_name))
    plugin._state_lock = threading.Lock()
    plugin._config_snapshot = {
        "host": "https://indexer.invalid",
        "api_key": "not-a-real-key",
        "proxy": False,
        "timeout": 12,
    }
    setattr(plugin, f"_{class_name}__parse_torznab_xml", lambda *_args, **_kwargs: parse(plugin))
    return plugin


@pytest.mark.parametrize("module_id, class_name, domain", _CASES)
@pytest.mark.parametrize("count", (0, 2))
def test_successful_page_is_reported_as_last_page(module_id, class_name, domain, count):
    plugin = _plugin(module_id, class_name, lambda _plugin: [object()] * count)
    with capture_site_search_observation() as observation:
        results = plugin.search_torrents({"id": 1, "name": "Site", "domain": domain}, keyword="x")

    assert len(results) == count
    assert (observation.attempted, observation.outcome, observation.error) == (True, "success", None)
    assert (observation.raw_count, observation.has_more) == (count, False)


@pytest.mark.parametrize("module_id, class_name, domain", _CASES)
def test_failed_request_is_reported_with_first_category(module_id, class_name, domain):
    def parse(plugin):
        plugin._record_error("torznab_error", source="search")
        plugin._record_error("ignored_later_error", source="search")
        return []

    plugin = _plugin(module_id, class_name, parse)
    with capture_site_search_observation() as observation:
        assert plugin.search_torrents({"id": 1, "name": "Site", "domain": domain}, keyword="x") == []

    assert (observation.attempted, observation.outcome, observation.error) == (True, "error", "torznab_error")
    assert plugin._last_search_error == "ignored_later_error"


@pytest.mark.parametrize("module_id, class_name, domain", _CASES)
def test_search_outside_host_observer_and_sync_errors_do_not_leak(module_id, class_name, domain):
    plugin = _plugin(module_id, class_name, lambda _plugin: [object()])
    assert len(plugin.search_torrents({"id": 1, "name": "Site", "domain": domain}, keyword="x")) == 1

    # Sync-source errors and errors outside a search call are not page outcomes.
    with capture_site_search_observation() as observation:
        plugin._record_error("request_error", source="sync")
        plugin._record_error("request_error", source="search")
    assert (observation.attempted, observation.outcome) == (False, "skipped")
