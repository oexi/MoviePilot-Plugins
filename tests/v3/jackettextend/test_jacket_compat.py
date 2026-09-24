import asyncio
import importlib
import inspect
import unittest
from types import MappingProxyType

from importlib import import_module


INDEXERS = import_module("app.plugins.jackettextend._indexers")
COMPAT = import_module("app.plugins.jackettextend._host_compat")


class Owner:
    plugin_name = "JackettExtend"

    def __init__(self, name="plugin"):
        self.name = name
        self.sync_calls = []
        self.async_calls = []
        self.page_calls = []

    def search_torrents(self, *args, **kwargs):
        self.sync_calls.append((args, kwargs))
        return [f"{self.name}-sync"]

    async def async_search_torrents(self, *args, **kwargs):
        self.async_calls.append((args, kwargs))
        return [f"{self.name}-async"]

    def get_search_page_size(self, *args, **kwargs):
        self.page_calls.append((args, kwargs))
        return None

    @staticmethod
    def _is_virtual_site(site, domain=""):
        return INDEXERS.is_virtual_site(site, domain)


class ExplodingOwner(Owner):
    def search_torrents(self, *args, **kwargs):
        self.sync_calls.append((args, kwargs))
        raise RuntimeError("sync owner failure")

    async def async_search_torrents(self, *args, **kwargs):
        self.async_calls.append((args, kwargs))
        raise RuntimeError("async owner failure")


class ExactOwner(Owner):
    def __init__(self, name="exact"):
        super().__init__(name)
        self.bound_sync_calls = []
        self.bound_async_calls = []

    def search_torrents(self, site, keyword=None, mtype=None, cat=None, page=0):
        self.bound_sync_calls.append((site, keyword, mtype, cat, page))
        return [f"{self.name}-sync"]

    async def async_search_torrents(
        self, site, keyword=None, mtype=None, cat=None, page=0
    ):
        self.bound_async_calls.append((site, keyword, mtype, cat, page))
        return [f"{self.name}-async"]


def make_chain():
    calls = []

    class ChainBase:
        def search_site_torrents(self, site, keyword, mtype=None, page=0):
            calls.append(("sync", site, keyword, page))
            return ["host-sync"]

        async def async_search_site_torrents(self, site, keyword, mtype=None, page=0):
            calls.append(("async", site, keyword, page))
            return ["host-async"]

        def get_search_page_size(self, site, keyword=None):
            calls.append(("page", site, keyword))
            return 50

    return ChainBase, calls


class HostCompatTest(unittest.TestCase):
    def setUp(self):
        self.host_chain = import_module("app.sdk.chain")
        self.previous_chain_base = self.host_chain.ChainBase
        self.ChainBase, self.host_calls = make_chain()
        self.host_chain.ChainBase = self.ChainBase

    def tearDown(self):
        # Restore an owner if a test intentionally left one installed.
        state = getattr(self.ChainBase, COMPAT._STATE_ATTR, None)
        if isinstance(state, dict):
            owner = COMPAT._owner_from_state(state)
            if owner is not None:
                COMPAT.uninstall(owner)
        self.host_chain.ChainBase = self.previous_chain_base

    def test_predicate_routes_only_jackett_sites_and_leaves_page_size_to_host(self):
        owner = Owner()
        originals = {
            name: inspect.getattr_static(self.ChainBase, name)
            for name in ("search_site_torrents", "async_search_site_torrents")
        }
        page_original = inspect.getattr_static(self.ChainBase, "get_search_page_size")

        self.assertEqual(
            COMPAT._METHODS,
            ("search_site_torrents", "async_search_site_torrents"),
        )
        self.assertTrue(COMPAT.install(owner))
        chain = self.ChainBase()

        marked = {"domain": "jackett_extend.nyaa"}
        marked_proxy = MappingProxyType({"domain": "jackett_extend.proxy"})
        ordinary = {"domain": "ordinary.example", "owned": True}
        self.assertEqual(chain.search_site_torrents(marked, "title"), ["plugin-sync"])
        self.assertEqual(chain.search_site_torrents(marked_proxy, "title"), ["plugin-sync"])
        self.assertEqual(chain.search_site_torrents(ordinary, "title"), ["host-sync"])
        self.assertEqual(chain.search_site_torrents({}, "global"), ["host-sync"])
        self.assertEqual(chain.search_site_torrents([], "invalid-site"), ["host-sync"])
        self.assertEqual(asyncio.run(chain.async_search_site_torrents(marked, "title")), ["plugin-async"])
        self.assertEqual(asyncio.run(chain.async_search_site_torrents(ordinary, "title")), ["host-async"])
        self.assertEqual(asyncio.run(chain.async_search_site_torrents({}, "global")), ["host-async"])
        self.assertEqual(COMPAT.status()["methods"], COMPAT._METHODS)
        self.assertNotIn("get_search_page_size", COMPAT.status()["methods"])
        self.assertIs(inspect.getattr_static(self.ChainBase, "get_search_page_size"), page_original)
        self.assertEqual(chain.get_search_page_size(marked, "title"), 50)
        self.assertEqual(chain.get_search_page_size(ordinary, "title"), 50)
        self.assertEqual(chain.get_search_page_size({}, "global"), 50)
        self.assertEqual(len(owner.sync_calls), 2)
        self.assertEqual(len(owner.async_calls), 1)
        self.assertEqual(len(owner.page_calls), 0)
        self.assertTrue(COMPAT.uninstall(owner))
        for name, original in originals.items():
            self.assertIs(getattr(self.ChainBase, name), original)
        self.assertIs(inspect.getattr_static(self.ChainBase, "get_search_page_size"), page_original)

    def test_install_is_idempotent_reload_safe_and_old_owner_cannot_uninstall(self):
        first = Owner()
        self.assertTrue(COMPAT.install(first))
        wrapped = self.ChainBase.search_site_torrents
        self.assertTrue(COMPAT.install(first))
        self.assertIs(self.ChainBase.search_site_torrents, wrapped)
        second = Owner()
        self.assertTrue(COMPAT.install(second))
        self.assertIs(self.ChainBase.search_site_torrents, wrapped)
        self.assertEqual(
            self.ChainBase().search_site_torrents(
                {"domain": "jackett_extend.a", "owned": True}, "x"
            ),
            ["plugin-sync"],
        )
        self.assertEqual(first.sync_calls, [])
        self.assertEqual(len(second.sync_calls), 1)
        self.assertFalse(COMPAT.uninstall(first))
        self.assertTrue(COMPAT.status()["installed"])
        self.assertTrue(COMPAT.uninstall(second))
        self.assertFalse(COMPAT.status()["installed"])

    def test_host_arguments_are_named_for_search_and_refresh_fallback(self):
        host_calls = self.host_calls

        def refresh_torrents(self, site, keyword=None, cat=None, page=0, mtype=None):
            host_calls.append(("refresh", site, keyword, cat, page, mtype))
            return ["host-refresh"]

        async def async_refresh_torrents(
            self, site, keyword=None, cat=None, page=0, mtype=None
        ):
            host_calls.append(("async-refresh", site, keyword, cat, page, mtype))
            return ["host-async-refresh"]

        self.ChainBase.refresh_torrents = refresh_torrents
        self.ChainBase.async_refresh_torrents = async_refresh_torrents
        owner = ExactOwner()
        self.assertTrue(COMPAT.install(owner))
        chain = self.ChainBase()
        site = {"domain": "jackett_extend.exact"}

        self.assertEqual(
            chain.search_site_torrents(site, "positional", "movie", 1),
            ["exact-sync"],
        )
        self.assertEqual(
            chain.search_site_torrents(site, keyword="mixed", page=2),
            ["exact-sync"],
        )
        self.assertEqual(
            chain.search_site_torrents(
                site=site, keyword="keyword", mtype="music", page=3
            ),
            ["exact-sync"],
        )
        self.assertEqual(
            owner.bound_sync_calls,
            [
                (site, "positional", "movie", None, 1),
                (site, "mixed", None, None, 2),
                (site, "keyword", "music", None, 3),
            ],
        )

        self.assertEqual(
            asyncio.run(
                chain.async_search_site_torrents(site, "async-positional", "tv", 4)
            ),
            ["exact-async"],
        )
        self.assertEqual(
            asyncio.run(
                chain.async_search_site_torrents(site, keyword="async-mixed", page=5)
            ),
            ["exact-async"],
        )
        self.assertEqual(
            asyncio.run(
                chain.async_search_site_torrents(
                    site=site, keyword="async-keyword", mtype="music", page=6
                )
            ),
            ["exact-async"],
        )
        self.assertEqual(
            owner.bound_async_calls,
            [
                (site, "async-positional", "tv", None, 4),
                (site, "async-mixed", None, None, 5),
                (site, "async-keyword", "music", None, 6),
            ],
        )

        # Exact host refresh order is cat/page/mtype.  ExactOwner has no
        # refresh method, so both routes deliberately exercise search fallback.
        self.assertEqual(
            chain.refresh_torrents(site, "refresh-positional", "3010", 7, "music"),
            ["exact-sync"],
        )
        self.assertEqual(
            chain.refresh_torrents(
                site, keyword="refresh-mixed", cat="2010", page=8, mtype="movie"
            ),
            ["exact-sync"],
        )
        self.assertEqual(
            chain.refresh_torrents(site=site, cat="5000", mtype="tv"),
            ["exact-sync"],
        )
        self.assertEqual(
            owner.bound_sync_calls[-3:],
            [
                (site, "refresh-positional", "music", "3010", 7),
                (site, "refresh-mixed", "movie", "2010", 8),
                (site, None, "tv", "5000", 0),
            ],
        )

        self.assertEqual(
            asyncio.run(
                chain.async_refresh_torrents(
                    site, "async-refresh-positional", "5010", 9, "tv"
                )
            ),
            ["exact-async"],
        )
        self.assertEqual(
            asyncio.run(
                chain.async_refresh_torrents(
                    site=site, keyword="async-refresh-keyword", cat="3010", mtype="music"
                )
            ),
            ["exact-async"],
        )
        self.assertEqual(
            owner.bound_async_calls[-2:],
            [
                (site, "async-refresh-positional", "tv", "5010", 9),
                (site, "async-refresh-keyword", "music", "3010", 0),
            ],
        )

    def test_host_capability_attributes_do_not_change_current_boundary(self):
        original = self.ChainBase.search_site_torrents
        self.ChainBase.supports_targeted_plugin_route = True
        owner = Owner()
        self.assertFalse(hasattr(COMPAT, "host_supports_targeted_route"))
        self.assertTrue(COMPAT.install(owner))
        self.assertIsNot(self.ChainBase.search_site_torrents, original)
        self.assertEqual(
            self.ChainBase().search_site_torrents({"domain": "jackett_extend.x"}, "x"),
            ["plugin-sync"],
        )

    def test_custom_predicate_can_claim_a_non_legacy_site(self):
        owner = Owner()
        self.assertTrue(COMPAT.install(owner, predicate=lambda site, _domain: site.get("owned") is True))
        result = self.ChainBase().search_site_torrents({"domain": "custom.example", "owned": True}, "x")
        self.assertEqual(result, ["plugin-sync"])

    def test_predicate_errors_fall_back_to_host_for_both_boundaries(self):
        owner = Owner()

        def broken_predicate(site, domain):
            raise RuntimeError(f"predicate failure: {domain}")

        self.assertTrue(COMPAT.install(owner, predicate=broken_predicate))
        chain = self.ChainBase()
        site = {"domain": "jackett_extend.broken"}
        self.assertEqual(chain.search_site_torrents(site, "title"), ["host-sync"])
        self.assertEqual(
            asyncio.run(chain.async_search_site_torrents(site, "title")),
            ["host-async"],
        )
        self.assertEqual(owner.sync_calls, [])
        self.assertEqual(owner.async_calls, [])

    def test_owner_search_errors_are_isolated_and_fall_back_to_host(self):
        owner = ExplodingOwner()
        self.assertTrue(COMPAT.install(owner))
        chain = self.ChainBase()
        site = {"domain": "jackett_extend.owner-error"}

        self.assertEqual(chain.search_site_torrents(site, "title"), ["host-sync"])
        self.assertEqual(
            asyncio.run(chain.async_search_site_torrents(site, "title")),
            ["host-async"],
        )
        self.assertEqual(len(owner.sync_calls), 1)
        self.assertEqual(len(owner.async_calls), 1)

    def test_owner_errors_emit_rate_limited_sanitized_diagnostic(self):
        owner = ExplodingOwner()
        messages = []
        original_emitter = COMPAT._emit_bridge_warning
        COMPAT._emit_bridge_warning = messages.append
        try:
            self.assertTrue(COMPAT.install(owner))
            chain = self.ChainBase()
            site = {"domain": "jackett_extend.private-site"}

            self.assertEqual(chain.search_site_torrents(site, "private-title"), ["host-sync"])
            self.assertEqual(chain.search_site_torrents(site, "private-title"), ["host-sync"])

            self.assertEqual(len(messages), 1)
            message = messages[0]
            self.assertIn("bridge_dispatch_error", message)
            self.assertIn("route=search_site_torrents", message)
            self.assertIn("phase=dispatch", message)
            self.assertIn("error_type=RuntimeError", message)
            self.assertNotIn("private-title", message)
            self.assertNotIn("private-site", message)
            self.assertNotIn("sync owner failure", message)
        finally:
            COMPAT._emit_bridge_warning = original_emitter

    def test_generation_race_never_calls_the_stale_owner(self):
        first = Owner("first")
        second = Owner("second")
        predicate_calls = []

        def switch_owner(site, domain):
            predicate_calls.append((site, domain))
            if len(predicate_calls) == 1:
                self.assertTrue(COMPAT.install(second))
            return True

        self.assertTrue(COMPAT.install(first, predicate=switch_owner))
        result = self.ChainBase().search_site_torrents(
            {"domain": "jackett_extend.race"}, "title"
        )

        # The predicate can race with reload, but an in-flight call must not
        # dispatch into the owner generation that was replaced mid-decision.
        self.assertEqual(first.sync_calls, [])
        self.assertIn(result, (["second-sync"], ["host-sync"]))
        self.assertLessEqual(len(second.sync_calls), 1)

    def test_reload_migrates_legacy_three_method_state_before_new_install(self):
        originals = {
            name: inspect.getattr_static(self.ChainBase, name)
            for name in (
                "search_site_torrents",
                "async_search_site_torrents",
                "get_search_page_size",
            )
        }

        def legacy_sync(*args, **kwargs):
            return ["legacy-sync"]

        async def legacy_async(*args, **kwargs):
            return ["legacy-async"]

        def legacy_page(*args, **kwargs):
            return None

        legacy_wrappers = {
            "search_site_torrents": legacy_sync,
            "async_search_site_torrents": legacy_async,
            "get_search_page_size": legacy_page,
        }
        legacy_state = {
            "version": 1,
            "owner_ref": None,
            "owner": None,
            "predicate_ref": None,
            "predicate": None,
            "originals": originals,
            "defined_here": {name: True for name in originals},
            "wrappers": legacy_wrappers,
        }
        for name, wrapper in legacy_wrappers.items():
            setattr(self.ChainBase, name, wrapper)
        setattr(self.ChainBase, COMPAT._STATE_ATTR, legacy_state)

        owner = Owner()
        self.assertTrue(COMPAT.install(owner))
        self.assertIs(
            inspect.getattr_static(self.ChainBase, "get_search_page_size"),
            originals["get_search_page_size"],
        )
        self.assertIsNot(self.ChainBase.search_site_torrents, legacy_sync)
        self.assertEqual(
            self.ChainBase().search_site_torrents(
                {"domain": "jackett_extend.reload"}, "title"
            ),
            ["plugin-sync"],
        )
        self.assertEqual(COMPAT.status()["methods"], COMPAT._METHODS)

    def test_module_reload_reuses_shared_bridge_wrappers_and_state(self):
        first = Owner("first")
        self.assertTrue(COMPAT.install(first))
        old_sync = self.ChainBase.search_site_torrents
        old_async = self.ChainBase.async_search_site_torrents
        old_state = getattr(self.ChainBase, COMPAT._STATE_ATTR)
        page_original = inspect.getattr_static(self.ChainBase, "get_search_page_size")

        reloaded = importlib.reload(COMPAT)
        second = Owner("second")
        self.assertTrue(reloaded.install(second))
        self.assertIs(self.ChainBase.search_site_torrents, old_sync)
        self.assertIs(self.ChainBase.async_search_site_torrents, old_async)
        self.assertIs(getattr(self.ChainBase, COMPAT._STATE_ATTR), old_state)
        self.assertIs(
            inspect.getattr_static(self.ChainBase, "get_search_page_size"),
            page_original,
        )
        self.assertFalse(COMPAT.uninstall(first))
        self.assertEqual(
            self.ChainBase().search_site_torrents(
                {"domain": "jackett_extend.reloaded"}, "title"
            ),
            ["second-sync"],
        )
        self.assertTrue(reloaded.uninstall(second))

    def test_uninstall_does_not_overwrite_third_party_method_replacement(self):
        owner = Owner()
        original_sync = inspect.getattr_static(self.ChainBase, "search_site_torrents")
        original_async = inspect.getattr_static(self.ChainBase, "async_search_site_torrents")
        original_page = inspect.getattr_static(self.ChainBase, "get_search_page_size")
        self.assertTrue(COMPAT.install(owner))

        def third_party_sync(*args, **kwargs):
            return ["third-party"]

        setattr(self.ChainBase, "search_site_torrents", third_party_sync)
        self.assertTrue(COMPAT.uninstall(owner))
        self.assertIs(
            inspect.getattr_static(self.ChainBase, "search_site_torrents"),
            third_party_sync,
        )
        self.assertIs(inspect.getattr_static(self.ChainBase, "async_search_site_torrents"), original_async)
        self.assertIs(inspect.getattr_static(self.ChainBase, "get_search_page_size"), original_page)
        self.assertIsNot(original_sync, third_party_sync)


class IndexerHelpersTest(unittest.TestCase):
    def test_selection_and_profiles_are_pure(self):
        self.assertEqual(
            INDEXERS.parse_indexer_sites("['Nyaa', ' AnimeTosho ']"),
            ["nyaa", "animetosho"],
        )
        raw = [
            {"id": "foo/bar", "name": "Foo", "privacy": "public", "caps": [
                {"ID": "3000", "Name": "Music"},
                {"ID": "5000", "Name": "TV"},
            ]},
            None,
        ]
        profiles = INDEXERS.build_indexer_profiles(raw, "https://jackett.invalid/", True)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["indexer_id"], "foo/bar")
        self.assertEqual(profiles[0]["privacy"], "public")
        self.assertTrue(profiles[0]["public"])
        self.assertIn("foo%2Fbar", profiles[0]["domain"])
        self.assertEqual(profiles[0]["category"]["music"][0]["id"], "3000")
        self.assertEqual(
            INDEXERS.apply_indexer_selection(profiles, ["missing"], explicit=True),
            [],
        )

    def test_category_ranges_preserve_subcategories_and_feed_host_music_selector(self):
        caps = [
            {"ID": "1999", "Name": "before movies"},
            {"ID": "2000", "Name": "Movies"},
            {"ID": "2010", "Name": "Foreign Movies"},
            {"ID": "2999", "Name": "last movie"},
            {"ID": "3000", "Name": "Music"},
            {"ID": "3010", "Name": "Albums"},
            {"ID": "3999", "Name": "last music"},
            {"ID": "4000", "Name": "outside music"},
            {"ID": "4999", "Name": "before tv"},
            {"ID": "5000", "Name": "TV"},
            {"ID": "5010", "Name": "Episodes"},
            {"ID": "5999", "Name": "last tv"},
            {"ID": "6000", "Name": "after tv"},
            {"ID": "2000x", "Name": "malformed"},
            {"ID": "+2010", "Name": "signed"},
            {"ID": "30.10", "Name": "decimal"},
            {"ID": True, "Name": "boolean"},
            {"ID": None, "Name": "missing"},
        ]

        category = INDEXERS._category_for_caps(caps)
        self.assertEqual(
            [entry["id"] for entry in category["movie"]],
            ["2000", "2010", "2999"],
        )
        self.assertEqual(
            [entry["id"] for entry in category["music"]],
            ["3000", "3010", "3999"],
        )
        self.assertEqual(
            [entry["id"] for entry in category["tv"]],
            ["5000", "5010", "5999"],
        )

        # Mirror the host's category-only music selector locally so this test
        # does not import the live MoviePilot application/startup graph.
        def host_supports_music(indexer):
            categories = indexer.get("category") or {}
            return isinstance(categories, dict) and bool(categories.get("music"))

        profile = INDEXERS.build_indexer_profiles(
            [{"id": "music-indexer", "name": "Music", "caps": caps}],
            "https://jackett.invalid",
            False,
        )[0]
        self.assertTrue(host_supports_music(profile))
        self.assertFalse(host_supports_music({"category": {"movie": category["movie"]}}))

    def test_jackett_type_maps_to_privacy_without_guessing_unknown_values(self):
        raw = [
            {"id": "pub", "name": "Public", "type": "public"},
            {"id": "semi", "name": "Semi", "type": "semi-private"},
            {"id": "priv", "name": "Private", "type": "private"},
            {"id": "legacy", "name": "Legacy", "privacy": "private"},
            {"id": "odd", "name": "Odd", "type": "not-a-jackett-type"},
            {"id": "explicit-unknown", "name": "Explicit unknown", "type": "unknown"},
            {"id": "missing", "name": "Missing", "public": True},
        ]

        profiles = INDEXERS.build_indexer_profiles(raw, "https://jackett.invalid", False)
        by_id = {profile["indexer_id"]: profile for profile in profiles}

        self.assertEqual(by_id["pub"]["privacy"], "public")
        self.assertTrue(by_id["pub"]["public"])
        self.assertEqual(by_id["semi"]["privacy"], "semi-private")
        self.assertFalse(by_id["semi"]["public"])
        self.assertEqual(by_id["priv"]["privacy"], "private")
        self.assertFalse(by_id["priv"]["public"])
        self.assertEqual(by_id["legacy"]["privacy"], "private")
        self.assertIsNone(by_id["odd"]["privacy"])
        self.assertFalse(by_id["odd"]["public"])
        self.assertEqual(by_id["explicit-unknown"]["privacy"], "unknown")
        self.assertFalse(by_id["explicit-unknown"]["public"])
        self.assertIsNone(by_id["missing"]["privacy"])
        self.assertTrue(by_id["missing"]["public"])


if __name__ == "__main__":
    unittest.main()
