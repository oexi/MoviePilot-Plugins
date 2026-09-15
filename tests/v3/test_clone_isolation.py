"""两个 V3 索引插件在生产插件命名空间中的分身隔离合同。"""

from __future__ import annotations

import asyncio
import sys
import threading
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.extensions.plugin.loader import PluginLoader
from app.schemas.plugin import PluginInstance


REPO_ROOT = Path(__file__).resolve().parents[2]
ChainBase = import_module("app.chain").ChainBase


class _NoopLogger:
    """满足生产加载器日志端口的最小测试替身。"""

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _SiteRegistry:
    """在内存中复现站点 Oper 的最小读写边界。"""

    def __init__(self, records):
        self.records = list(records)
        self.deleted = []
        self.updates = []
        self.events = []

    def list(self):
        return list(self.records)

    def get_by_domain(self, domain):
        return next(
            (record for record in self.records if record.domain == domain),
            None,
        )

    def update(self, site_id, payload):
        self.updates.append((site_id, dict(payload)))
        record = next(record for record in self.records if record.id == site_id)
        for key, value in payload.items():
            setattr(record, key, value)

    def add(self, **payload):
        site_id = max((record.id for record in self.records), default=0) + 1
        record = SimpleNamespace(id=site_id, **payload)
        self.records.append(record)
        return record

    def delete(self, site_id):
        self.deleted.append(site_id)
        self.records[:] = [record for record in self.records if record.id != site_id]

    def notify_deleted(self, site_id):
        self.events.append(("deleted", site_id))

    def notify_updated(self, domain):
        self.events.append(("updated", domain))


def _private_method(instance, suffix):
    """按行为后缀取得被源类名改写的私有方法。"""
    name = next(name for name in dir(instance) if name.endswith(suffix))
    return getattr(instance, name)


def _load_classes(plugin_dir, instance_ids):
    """使用生产 PluginLoader 在 app.plugins 下加载源类和两个分身类。"""
    loader = PluginLoader(
        plugins_root=REPO_ROOT / "plugins.v3",
        import_preparer=lambda **_kwargs: None,
        import_scanner=lambda **_kwargs: None,
        log=_NoopLogger(),
    )
    validator = lambda candidate: (
        hasattr(candidate, "init_plugin") and hasattr(candidate, "plugin_name")
    )
    source = loader.load(plugin_dir, [plugin_dir], validator)[0]
    clones = [
        loader.load_instance(
            PluginInstance(
                instance_id=instance_id,
                source_plugin_id=source.__name__,
            ),
            validator,
        )[0]
        for instance_id in instance_ids
    ]
    return loader, source, clones


def _raw_indexer(plugin_dir):
    if plugin_dir == "jackettextend":
        return [{"id": "nyaa", "name": "Nyaa", "type": "public"}]
    return [{
        "id": 7,
        "name": "Seven",
        "enable": True,
        "protocol": "torrent",
        "supportsSearch": True,
        "privacy": "public",
    }]


def _profile(plugin_dir, plugin_class):
    module = import_module(plugin_class.__module__)
    return module.build_indexer_profiles(
        _raw_indexer(plugin_dir),
        "https://service.invalid",
        False,
        plugin_name=plugin_class.plugin_name,
        domain_prefix=plugin_class._site_domain_prefix(),
        owner_id=plugin_class._runtime_instance_id(),
    )[0]


def _install_owner(plugin_class, label):
    """安装一个只返回标签的实例 owner，避免进入真实网络请求。"""
    instance = object.__new__(plugin_class)

    def search(site, keyword=None, mtype=None, cat=None, page=0, **_kwargs):
        del site, keyword, mtype, cat, page
        return [label]

    async def async_search(
            site,
            keyword=None,
            mtype=None,
            cat=None,
            page=0,
            **_kwargs):
        del site, keyword, mtype, cat, page
        return [label]

    instance.search_torrents = search
    instance.async_search_torrents = async_search
    instance._bridge_owner_key = plugin_class._runtime_instance_id()
    module = import_module(plugin_class.__module__)
    assert module._host_compat.install(
        instance,
        predicate=instance._is_virtual_site,
        owner_key=instance._bridge_owner_key,
    )
    return instance, module._host_compat


@pytest.mark.parametrize(
    ("plugin_dir", "instance_ids", "historical_prefix"),
    (
        (
            "jackettextend",
            ("JackettExtendIsolationHome", "JackettExtendIsolationWork"),
            "jackett_extend.",
        ),
        (
            "prowlarrextend",
            ("ProwlarrExtendIsolationHome", "ProwlarrExtendIsolationWork"),
            "prowlarr_extend.",
        ),
    ),
)
def test_same_plugin_clones_keep_bridge_sites_and_services_isolated(
        plugin_dir,
        instance_ids,
        historical_prefix,
        monkeypatch):
    """源实例与两个分身可共存，重载/清理任一实例不越界。"""
    loader, source_class, clone_classes = _load_classes(plugin_dir, instance_ids)
    classes = [source_class, *clone_classes]
    owners = []
    registry = _SiteRegistry([
        SimpleNamespace(
            id=101,
            domain=historical_prefix + ("nyaa" if plugin_dir == "jackettextend" else "7"),
            pri=7,
            is_active=False,
            proxy=1,
            references={"search": [101]},
        ),
        SimpleNamespace(
            id=201,
            domain=_profile(plugin_dir, clone_classes[0])["domain"],
            pri=8,
            is_active=False,
            proxy=1,
            references={"search": [201]},
        ),
        SimpleNamespace(
            id=202,
            domain=_profile(plugin_dir, clone_classes[1])["domain"],
            pri=9,
            is_active=True,
            proxy=0,
            references={"search": [202]},
        ),
    ])

    try:
        profiles = [_profile(plugin_dir, plugin_class) for plugin_class in classes]
        domains = [profile["domain"] for profile in profiles]
        owners = [
            _install_owner(plugin_class, label)
            for plugin_class, label in zip(classes, ("source", "home", "work"))
        ]

        assert source_class._domain_prefix_set() == (historical_prefix,)
        assert len(set(domains)) == 3
        assert domains[0] == historical_prefix + (
            "nyaa" if plugin_dir == "jackettextend" else "7"
        )
        assert all(
            profile["plugin"] == plugin_class._runtime_instance_id()
            and profile["parser"] == plugin_class._runtime_instance_id()
            for profile, plugin_class in zip(profiles, classes)
        )
        assert profiles[0]["domain"].startswith(historical_prefix)
        assert all(
            not profile["domain"].startswith(historical_prefix)
            for profile in profiles[1:]
        )
        assert source_class._indexer_id_from_domain(profiles[1]["domain"]) == ""
        assert clone_classes[0]._indexer_id_from_domain(profiles[1]["domain"])
        assert clone_classes[0]._domain_prefix_set() != source_class._domain_prefix_set()
        assert clone_classes[0]._domain_prefix_set() != clone_classes[1]._domain_prefix_set()

        source_instance, bridge = owners[0]
        assert set(bridge.status()["owner_keys"]) == {
            plugin_class._runtime_instance_id() for plugin_class in classes
        }
        chain = object.__new__(ChainBase)
        for profile, label in zip(profiles, ("source", "home", "work")):
            assert chain.search_site_torrents(profile, "title") == [label]
            assert asyncio.run(
                chain.async_search_site_torrents(profile, "title")
            ) == [label]

        initial_service_ids = []
        recurring_service_ids = []
        for plugin_class in classes:
            service_instance = object.__new__(plugin_class)
            service_instance._enabled = True
            service_instance._cron = "0 0 * * *"
            initial, recurring = service_instance.get_service()
            assert initial["trigger"] == "date"
            assert initial["id"] == service_instance.get_service()[0]["id"]
            assert recurring["id"] == service_instance.get_service()[1]["id"]
            initial_service_ids.append(initial["id"])
            recurring_service_ids.append(recurring["id"])
        assert len(set(initial_service_ids)) == 3
        assert len(set(recurring_service_ids)) == 3
        assert initial_service_ids[1:] == [
            f"{plugin_class._runtime_instance_id().lower()}_sync_initial"
            for plugin_class in clone_classes
        ]
        assert recurring_service_ids[1:] == [
            f"{plugin_class._runtime_instance_id().lower()}_sync"
            for plugin_class in clone_classes
        ]

        for plugin_class in classes:
            module = import_module(plugin_class.__module__)
            monkeypatch.setattr(module, "open_site_registry", lambda: registry)

        register_source = _private_method(source_instance, "__register_site")
        assert register_source(profiles[0])
        assert registry.updates[0][0] == 101
        assert registry.records[0].pri == 7
        assert registry.records[0].is_active is False
        assert registry.records[0].proxy == 1

        owners[1][0]._stop_runtime()
        reload_class = loader.load_instance(
            PluginInstance(
                instance_id=instance_ids[0],
                source_plugin_id=source_class.__name__,
            ),
            lambda candidate: (
                hasattr(candidate, "init_plugin")
                and hasattr(candidate, "plugin_name")
            ),
            )[0]
        assert reload_class._site_domain_prefix() == clone_classes[0]._site_domain_prefix()
        reloaded_profile = _profile(plugin_dir, reload_class)
        assert reloaded_profile["domain"] == profiles[1]["domain"]
        assert reloaded_profile["plugin"] == instance_ids[0]

        monkeypatch.setattr(
            import_module(reload_class.__module__),
            "open_site_registry",
            lambda: registry,
        )
        reloaded_instance, reloaded_bridge = _install_owner(reload_class, "home-reloaded")
        owners[1] = (reloaded_instance, reloaded_bridge)
        assert chain.search_site_torrents(reloaded_profile, "title") == ["home-reloaded"]
        register_clone = _private_method(reloaded_instance, "__register_site")
        assert register_clone(reloaded_profile)
        assert registry.updates[-1][0] == 201
        assert registry.records[1].pri == 8
        assert registry.records[1].is_active is False
        assert registry.records[1].proxy == 1

        owners[2][1].uninstall(owners[2][0], owner_key=owners[2][0]._bridge_owner_key)
        assert bridge.status()["owner_keys"] == (
            source_class._runtime_instance_id(),
            clone_classes[0]._runtime_instance_id(),
        )
        assert chain.search_site_torrents(profiles[0], "title") == ["source"]
        assert chain.search_site_torrents(profiles[1], "title") == ["home-reloaded"]

        cleanup_home = _private_method(owners[1][0], "__remove_managed_sites")
        assert cleanup_home()
        assert registry.deleted == [201]
        assert {record.id for record in registry.records} == {101, 202}
        assert registry.records[0].domain == profiles[0]["domain"]

        owners[1][1].uninstall(owners[1][0], owner_key=owners[1][0]._bridge_owner_key)
        owners[0][1].uninstall(owners[0][0], owner_key=owners[0][0]._bridge_owner_key)
        assert bridge.status()["installed"] is False
    finally:
        for instance, compat in owners:
            compat.uninstall(
                instance,
                owner_key=getattr(instance, "_bridge_owner_key", None),
            )
        for instance_id in instance_ids:
            loader.clear_modules(instance_id)
        for module_name in list(sys.modules):
            if any(
                    module_name == f"app.plugins.{instance_id.lower()}"
                    or module_name.startswith(f"app.plugins.{instance_id.lower()}.")
                    for instance_id in instance_ids):
                sys.modules.pop(module_name, None)


@pytest.mark.parametrize(
    ("plugin_dir", "instance_id"),
    (
        ("jackettextend", "JackettExtendInitProbe"),
        ("prowlarrextend", "ProwlarrExtendInitProbe"),
    ),
)
def test_clone_init_binds_runtime_owner_without_starting_network(
        plugin_dir,
        instance_id,
        monkeypatch):
    """分身初始化把 bridge 与宿主调度服务标识绑定到运行类名。"""
    loader, source_class, clone_classes = _load_classes(plugin_dir, (instance_id,))
    del source_class
    clone_class = clone_classes[0]
    module = import_module(clone_class.__module__)
    calls = []


    try:
        monkeypatch.setattr(module, "SitesHelper", lambda: object())
        monkeypatch.setattr(
            module._host_compat,
            "install",
            lambda *args, **kwargs: calls.append(("install", args, kwargs)) or True,
        )
        monkeypatch.setattr(
            module._host_compat,
            "uninstall",
            lambda *args, **kwargs: calls.append(("uninstall", args, kwargs)) or True,
        )
        plugin = object.__new__(clone_class)
        plugin.init_plugin({
            "enabled": True,
            "host": "https://service.invalid",
            "api_key": "test-key",
        })

        assert plugin._bridge_owner_key == instance_id
        install_call = next(call for call in calls if call[0] == "install")
        assert install_call[2]["owner_key"] == instance_id
        assert "_sync_thread" not in plugin.__dict__
        initial, recurring = plugin.get_service()
        assert initial["trigger"] == "date"
        assert initial["id"] == f"{instance_id.lower()}_sync_initial"
        assert recurring["id"] == f"{instance_id.lower()}_sync"

        assert plugin.stop_service()
        uninstall_call = next(call for call in calls if call[0] == "uninstall")
        assert uninstall_call[2]["owner_key"] == instance_id
    finally:
        loader.clear_modules(instance_id)
        for module_name in list(sys.modules):
            if (
                module_name == f"app.plugins.{instance_id.lower()}"
                or module_name.startswith(f"app.plugins.{instance_id.lower()}.")
            ):
                sys.modules.pop(module_name, None)
