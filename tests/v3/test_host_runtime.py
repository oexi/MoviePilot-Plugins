"""在隔离配置中验收 V3 插件的真实宿主加载与动态路由生命周期。"""

from __future__ import annotations

import inspect
import sys
from importlib import import_module

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.application.plugin.routes import (
    configure_plugin_routes,
    register_plugin_api,
    remove_plugin_api,
)
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.log import logger as plugin_logger
from app.schemas.plugin import PluginRuntimeStatus


PLUGIN_CASES = (
    ("JackettExtend", "jackettextend"),
    ("ProwlarrExtend", "prowlarrextend"),
)


@pytest.mark.parametrize("plugin_id, module_id", PLUGIN_CASES)
def test_plugins_use_sdk_base_and_explicit_module_contracts(
    plugin_id: str,
    module_id: str,
) -> None:
    """插件优先使用 SDK 基类，模块 provider 不暴露动态 varargs。"""
    del plugin_id
    module = import_module(f"app.plugins.{module_id}")
    sdk_base = import_module("app.sdk.plugin")._PluginBase
    plugin_class = getattr(module, "JackettExtend" if module_id == "jackettextend" else "ProwlarrExtend")

    assert plugin_class.__bases__[0] is sdk_base

    plugin = object.__new__(plugin_class)
    module_methods = plugin.get_module()
    expected_parameters = {
        "search_torrents": ("site", "keyword", "mtype", "page"),
        "refresh_torrents": ("site", "keyword", "cat", "page", "mtype"),
        "get_search_page_size": ("site", "keyword"),
    }
    from app.runtime.extensions.module.contracts import diagnose_module_callable

    for method, required in expected_parameters.items():
        callback = module_methods[method]
        parameters = tuple(inspect.signature(callback).parameters.values())
        assert not any(
            parameter.kind
            in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for parameter in parameters
        )
        assert set(required).issubset({parameter.name for parameter in parameters})
        assert diagnose_module_callable(method, callback) == ()

    assert inspect.iscoroutinefunction(module_methods["async_search_torrents"])
    assert inspect.iscoroutinefunction(module_methods["async_refresh_torrents"])


def _route_paths(app: FastAPI, suffix: str) -> list[str]:
    """返回某个插件动态路由的公开路径。"""
    return [
        route.path
        for route in app.routes
        if getattr(route, "path", "").endswith(suffix)
    ]


@pytest.mark.parametrize("plugin_id, module_id", PLUGIN_CASES)
def test_real_host_load_registers_api_and_reloads_in_place(
    plugin_id: str,
    module_id: str,
) -> None:
    """真实宿主命名空间中加载、路由注册、重载和停止必须可收敛。"""
    manager = PluginManager()
    app = FastAPI()
    routes_module = sys.modules["app.application.plugin.routes"]
    previous_registry = getattr(routes_module, "_route_registry", None)
    registry = FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=manager.get_running_plugin_ids,
        plugin_apis=manager.get_plugin_apis,
        verify_token=lambda: None,
        verify_apikey=lambda: None,
        prefix="/api/v1/plugin",
        protected_routes=set(),
        log=plugin_logger,
    )
    configure_plugin_routes(registry)

    try:
        # Empty persisted config deliberately keeps the plugin disabled: this
        # exercises the real constructor and stop path without starting any
        # network worker or contacting an external service.
        manager.remove_plugin(plugin_id)
        status = manager.start(plugin_id)

        assert status[plugin_id] is PluginRuntimeStatus.ACTIVE
        instance = manager.running_plugins[plugin_id]
        assert type(instance).__module__ == f"app.plugins.{module_id}"
        assert f"app.plugins.{module_id}" in sys.modules
        assert module_id not in sys.modules

        register_plugin_api(plugin_id)
        status_path = f"/api/v1/plugin/{plugin_id}/status"
        assert _route_paths(app, status_path) == [status_path]

        response = TestClient(app).get(status_path)
        assert response.status_code == 200
        body = response.json()
        assert "success" not in body
        assert "message" not in body
        assert body["enabled"] is False
        assert body["configured"] is False

        # Host route refresh is replace-by-plugin, not append-only.
        register_plugin_api(plugin_id)
        assert _route_paths(app, status_path) == [status_path]

        reloaded_status = manager.reload_plugin(plugin_id)
        assert reloaded_status is PluginRuntimeStatus.ACTIVE
        assert manager.running_plugins[plugin_id] is not instance
        register_plugin_api(plugin_id)
        assert _route_paths(app, status_path) == [status_path]

        manager.stop(plugin_id)
        remove_plugin_api(plugin_id)
        assert plugin_id not in manager.running_plugins
        assert _route_paths(app, status_path) == []
    finally:
        # Keep the singleton manager and route registry clean for the next
        # parameter value and for the coordinator's tests in this process.
        remove_plugin_api(plugin_id)
        manager.stop(plugin_id)
        manager.remove_plugin(plugin_id)
        configure_plugin_routes(previous_registry)


@pytest.mark.parametrize("plugin_id, module_id", PLUGIN_CASES)
def test_bridge_resolves_chain_base_from_sdk_not_compat_root(
    plugin_id: str,
    module_id: str,
) -> None:
    """桥接直接使用 SDK 导出的 ChainBase，不经过 app.chain 包根兼容符号。"""
    del plugin_id
    from unittest import mock

    compat = import_module(f"app.plugins.{module_id}._host_compat")
    sdk_chain_base = import_module("app.sdk.chain").ChainBase
    assert sdk_chain_base is import_module("app.chain.base").ChainBase

    class ShadowChainBase:
        """模拟即将退场的包根兼容符号。"""

    with mock.patch.object(
        import_module("app.chain"),
        "ChainBase",
        ShadowChainBase,
        create=True,
    ):
        assert compat._find_chain_base() is sdk_chain_base
