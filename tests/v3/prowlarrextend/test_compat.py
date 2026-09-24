import asyncio
import importlib
import inspect
import unittest

from importlib import import_module


JACKETT_COMPAT = import_module("app.plugins.jackettextend._host_compat")
PROWLARR_COMPAT = import_module("app.plugins.prowlarrextend._host_compat")


class Owner:
    def __init__(self, value, plugin_name=None):
        self.value = value
        self.plugin_name = plugin_name or value
        self.sync_calls = 0
        self.async_calls = 0
        self.async_refresh_calls = 0
        self.error_factory = None

    def _raise_if_configured(self):
        if self.error_factory is not None:
            raise self.error_factory()

    def search_torrents(self, *args, **kwargs):
        self.sync_calls += 1
        self._raise_if_configured()
        return [self.value]

    async def async_search_torrents(self, *args, **kwargs):
        self.async_calls += 1
        self._raise_if_configured()
        return [self.value]

    async def async_refresh_torrents(self, *args, **kwargs):
        self.async_refresh_calls += 1
        self._raise_if_configured()
        return [self.value]


class ProwlarrCompatContractTest(unittest.TestCase):
    @staticmethod
    def _predicate(plugin_name, prefix):
        def owns(site, domain):
            markers = {
                str(site.get("plugin") or "").lower(),
                str(site.get("parser") or "").lower(),
            }
            if any(markers):
                return plugin_name.lower() in markers
            return domain.lower().startswith(prefix)
        return owns

    def _exercise_load_order(self, first_name):
        jackett_compat = JACKETT_COMPAT
        prowlarr_compat = PROWLARR_COMPAT
        calls = []

        class ChainBase:
            def search_site_torrents(self, site, keyword, *args, **kwargs):
                calls.append(("sync", site, keyword))
                return ["host"]

            async def async_search_site_torrents(self, site, keyword, *args, **kwargs):
                calls.append(("async", site, keyword))
                return ["host"]

            def refresh_torrents(self, site, keyword, *args, **kwargs):
                calls.append(("refresh", site, keyword))
                return ["host"]

            async def async_refresh_torrents(self, site, keyword, *args, **kwargs):
                calls.append(("async-refresh", site, keyword))
                return ["host"]

        host_chain = import_module("app.sdk.chain")
        previous_chain_base = host_chain.ChainBase
        host_chain.ChainBase = ChainBase
        try:
            jackett = Owner("jackett", "JackettExtend")
            prowlarr = Owner("prowlarr", "ProwlarrExtend")
            installs = {
                "jackett": lambda: jackett_compat.install(
                    jackett,
                    predicate=self._predicate("JackettExtend", "jackett_extend."),
                ),
                "prowlarr": lambda: prowlarr_compat.install(
                    prowlarr,
                    predicate=self._predicate("ProwlarrExtend", "prowlarr_extend."),
                    owner_key="prowlarrextend",
                ),
            }
            second_name = "prowlarr" if first_name == "jackett" else "jackett"
            originals = {
                name: inspect.getattr_static(ChainBase, name)
                for name in (
                    "search_site_torrents",
                    "async_search_site_torrents",
                    "refresh_torrents",
                    "async_refresh_torrents",
                )
            }
            self.assertTrue(installs[first_name]())
            wrapped = {
                name: inspect.getattr_static(ChainBase, name) for name in originals
            }
            self.assertTrue(installs[second_name]())
            # Independently loaded helpers must share, never stack, wrappers.
            for name in wrapped:
                self.assertIs(inspect.getattr_static(ChainBase, name), wrapped[name])

            chain_instance = ChainBase()
            self.assertEqual(
                chain_instance.search_site_torrents({"domain": "jackett_extend.nyaa"}, "x"),
                ["jackett"],
            )
            self.assertEqual(
                chain_instance.search_site_torrents({"domain": "prowlarr_extend.7"}, "x"),
                ["prowlarr"],
            )
            # Refresh is a separate host boundary.  The plugin's synchronous
            # refresh module aliases search_torrents, while its async refresh
            # implementation is preferred by the shared bridge.
            self.assertEqual(
                chain_instance.refresh_torrents({"domain": "jackett_extend.nyaa"}, "x"),
                ["jackett"],
            )
            self.assertEqual(
                chain_instance.refresh_torrents({"domain": "prowlarr_extend.7"}, "x"),
                ["prowlarr"],
            )
            # Explicit ownership wins over a conflicting historical domain.
            self.assertEqual(
                chain_instance.search_site_torrents({
                    "domain": "jackett_extend.looks-like-jackett",
                    "plugin": "ProwlarrExtend",
                }, "x"),
                ["prowlarr"],
            )
            self.assertEqual(
                chain_instance.search_site_torrents({"domain": "ordinary.example"}, "x"),
                ["host"],
            )
            self.assertEqual(chain_instance.search_site_torrents({}, "global"), ["host"])
            self.assertEqual(
                chain_instance.refresh_torrents({"domain": "ordinary.example"}, "x"),
                ["host"],
            )
            self.assertEqual(chain_instance.refresh_torrents({}, "global"), ["host"])
            self.assertEqual(
                asyncio.run(chain_instance.async_search_site_torrents(
                    {"domain": "prowlarr_extend.7"}, "x"
                )),
                ["prowlarr"],
            )
            self.assertEqual(
                asyncio.run(chain_instance.async_refresh_torrents(
                    {"domain": "prowlarr_extend.7"}, "x"
                )),
                ["prowlarr"],
            )
            self.assertEqual(
                asyncio.run(chain_instance.async_refresh_torrents(
                    {"domain": "jackett_extend.nyaa"}, "x"
                )),
                ["jackett"],
            )
            self.assertEqual(
                asyncio.run(chain_instance.async_refresh_torrents(
                    {"domain": "ordinary.example"}, "x"
                )),
                ["host"],
            )
            self.assertEqual(
                asyncio.run(chain_instance.async_refresh_torrents({}, "global")),
                ["host"],
            )
            self.assertEqual(prowlarr.async_refresh_calls, 1)

            # A marked Prowlarr upstream failure is fail-closed at ordinary
            # search boundaries, but crosses the dedicated refresh boundary
            # even when the shared wrapper came from the other module copy.
            prowlarr.error_factory = lambda: prowlarr_compat.SanitizedUpstreamError(
                "http_429"
            )
            self.assertEqual(
                chain_instance.search_site_torrents(
                    {"domain": "prowlarr_extend.7"}, "title"
                ),
                ["host"],
            )
            self.assertEqual(
                asyncio.run(chain_instance.async_search_site_torrents(
                    {"domain": "prowlarr_extend.7"}, "title"
                )),
                ["host"],
            )
            with self.assertRaises(prowlarr_compat.SanitizedUpstreamError):
                chain_instance.refresh_torrents(
                    {"domain": "prowlarr_extend.7"}, None
                )
            with self.assertRaises(prowlarr_compat.SanitizedUpstreamError):
                asyncio.run(chain_instance.async_refresh_torrents(
                    {"domain": "prowlarr_extend.7"}, None
                ))
            # Other owners and ordinary/global host routing remain isolated.
            self.assertEqual(
                chain_instance.refresh_torrents(
                    {"domain": "jackett_extend.nyaa"}, None
                ),
                ["jackett"],
            )
            self.assertEqual(
                chain_instance.refresh_torrents(
                    {"domain": "ordinary.example"}, None
                ),
                ["host"],
            )
            self.assertEqual(chain_instance.refresh_torrents({}, None), ["host"])
            prowlarr.error_factory = None

            # Disabling either first leaves the other plugin active.
            if first_name == "jackett":
                self.assertTrue(jackett_compat.uninstall(jackett))
                remaining_site = {"domain": "prowlarr_extend.7"}
                remaining_result = ["prowlarr"]
                last_uninstall = lambda: prowlarr_compat.uninstall(
                    prowlarr, owner_key="prowlarrextend"
                )
            else:
                self.assertTrue(prowlarr_compat.uninstall(
                    prowlarr, owner_key="prowlarrextend"
                ))
                remaining_site = {"domain": "jackett_extend.nyaa"}
                remaining_result = ["jackett"]
                last_uninstall = lambda: jackett_compat.uninstall(jackett)
            self.assertEqual(
                chain_instance.search_site_torrents(remaining_site, "x"),
                remaining_result,
            )
            self.assertEqual(
                chain_instance.refresh_torrents(remaining_site, "x"),
                remaining_result,
            )
            self.assertTrue(last_uninstall())
            for name, original in originals.items():
                self.assertIs(inspect.getattr_static(ChainBase, name), original)
        finally:
            state = getattr(ChainBase, jackett_compat._STATE_ATTR, None)
            if isinstance(state, dict):
                for key, record in list((state.get("owners") or {}).items()):
                    owner = jackett_compat._owner_from_record(record)
                    if owner is not None:
                        jackett_compat.uninstall(owner, owner_key=key)
            host_chain.ChainBase = previous_chain_base

    def test_both_plugins_coexist_in_either_load_order_and_disable_order(self):
        for first_name in ("jackett", "prowlarr"):
            with self.subTest(first=first_name):
                self._exercise_load_order(first_name)

    def test_refresh_fallbacks_preserve_host_and_propagate_cancellation(self):
        compat = JACKETT_COMPAT

        class ChainBase:
            def search_site_torrents(self, site, keyword, *args, **kwargs):
                return ["host-search"]

            async def async_search_site_torrents(self, site, keyword, *args, **kwargs):
                return ["host-async-search"]

            def refresh_torrents(self, site, keyword, *args, **kwargs):
                return ["host-refresh"]

            async def async_refresh_torrents(self, site, keyword, *args, **kwargs):
                return ["host-async-refresh"]

        host_chain = import_module("app.sdk.chain")
        previous_chain_base = host_chain.ChainBase
        host_chain.ChainBase = ChainBase
        try:
            class ErrorOwner:
                @staticmethod
                def _is_virtual_site(site, domain=""):
                    return bool(site)

                def search_torrents(self, *args, **kwargs):
                    raise RuntimeError("sync refresh failed")

                async def async_search_torrents(self, *args, **kwargs):
                    raise RuntimeError("async refresh failed")

            owner = ErrorOwner()
            self.assertTrue(compat.install(owner, owner_key="errors"))
            instance = ChainBase()
            self.assertEqual(
                instance.refresh_torrents({"domain": "virtual"}, "x"),
                ["host-refresh"],
            )
            self.assertEqual(
                asyncio.run(instance.async_refresh_torrents({"domain": "virtual"}, "x")),
                ["host-async-refresh"],
            )

            class CancelOwner(ErrorOwner):
                def search_torrents(self, *args, **kwargs):
                    raise asyncio.CancelledError()

                async def async_search_torrents(self, *args, **kwargs):
                    raise asyncio.CancelledError()

            cancelled = CancelOwner()
            self.assertTrue(compat.install(cancelled, owner_key="errors"))
            with self.assertRaises(asyncio.CancelledError):
                instance.refresh_torrents({"domain": "virtual"}, "x")
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(instance.async_refresh_torrents({"domain": "virtual"}, "x"))
            self.assertTrue(compat.uninstall(cancelled, owner_key="errors"))
        finally:
            state = getattr(ChainBase, compat._STATE_ATTR, None)
            if isinstance(state, dict):
                for key, record in list((state.get("owners") or {}).items()):
                    owner = compat._owner_from_record(record)
                    if owner is not None:
                        compat.uninstall(owner, owner_key=key)
            host_chain.ChainBase = previous_chain_base

    def test_same_key_reload_replaces_only_prowlarr_and_stale_owner_is_safe(self):
        first_compat = PROWLARR_COMPAT
        second_compat = importlib.reload(PROWLARR_COMPAT)
        jackett_compat = JACKETT_COMPAT

        class ChainBase:
            def search_site_torrents(self, site, keyword, *args, **kwargs):
                return ["host"]

            async def async_search_site_torrents(self, site, keyword, *args, **kwargs):
                return ["host"]

            def refresh_torrents(self, site, keyword, *args, **kwargs):
                return ["host"]

            async def async_refresh_torrents(self, site, keyword, *args, **kwargs):
                return ["host"]

        host_chain = import_module("app.sdk.chain")
        previous_chain_base = host_chain.ChainBase
        host_chain.ChainBase = ChainBase
        try:
            jackett = Owner("jackett", "JackettExtend")
            old = Owner("old-prowlarr", "ProwlarrExtend")
            new = Owner("new-prowlarr", "ProwlarrExtend")
            self.assertTrue(jackett_compat.install(
                jackett, predicate=self._predicate("JackettExtend", "jackett_extend.")
            ))
            self.assertTrue(first_compat.install(
                old,
                predicate=self._predicate("ProwlarrExtend", "prowlarr_extend."),
                owner_key="prowlarrextend",
            ))
            wrapped = {
                name: inspect.getattr_static(ChainBase, name)
                for name in (
                    "search_site_torrents",
                    "async_search_site_torrents",
                    "refresh_torrents",
                    "async_refresh_torrents",
                )
            }
            self.assertTrue(second_compat.install(
                new,
                predicate=self._predicate("ProwlarrExtend", "prowlarr_extend."),
                owner_key="prowlarrextend",
            ))
            for name, wrapper in wrapped.items():
                self.assertIs(inspect.getattr_static(ChainBase, name), wrapper)
            self.assertFalse(first_compat.uninstall(old, owner_key="prowlarrextend"))
            instance = ChainBase()
            self.assertEqual(
                instance.search_site_torrents({"domain": "prowlarr_extend.7"}, "x"),
                ["new-prowlarr"],
            )
            self.assertEqual(
                instance.search_site_torrents({"domain": "jackett_extend.nyaa"}, "x"),
                ["jackett"],
            )
            self.assertEqual(
                instance.refresh_torrents({"domain": "prowlarr_extend.7"}, "x"),
                ["new-prowlarr"],
            )
            self.assertTrue(second_compat.uninstall(new, owner_key="prowlarrextend"))
            self.assertEqual(
                instance.search_site_torrents({"domain": "jackett_extend.nyaa"}, "x"),
                ["jackett"],
            )
            self.assertTrue(jackett_compat.uninstall(jackett))
        finally:
            state = getattr(ChainBase, jackett_compat._STATE_ATTR, None)
            if isinstance(state, dict):
                for key, record in list((state.get("owners") or {}).items()):
                    owner = jackett_compat._owner_from_record(record)
                    if owner is not None:
                        jackett_compat.uninstall(owner, owner_key=key)
            host_chain.ChainBase = previous_chain_base


if __name__ == "__main__":
    unittest.main()
