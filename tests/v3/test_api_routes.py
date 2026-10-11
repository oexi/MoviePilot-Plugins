"""Regression tests for the V3 plugin API route contracts."""

import inspect
from dataclasses import dataclass
from typing import Any

import pytest
from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.plugins.jackettextend import JackettExtend
from app.plugins.jackettextend._api_models import (
    JackettStatusResponse,
    JackettSyncStatus,
    JackettTestResponse,
)
from app.plugins.prowlarrextend import ProwlarrExtend
from app.plugins.prowlarrextend._api_models import (
    ProwlarrStatusResponse,
    ProwlarrSyncStatus,
    ProwlarrTestResponse,
)
from app.runtime.extensions.plugin.projection import PluginProjection
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

# Older V3 hosts require ``protected_routes``; newer hosts removed the argument.
_REGISTRY_COMPAT_KWARGS = (
    {"protected_routes": set()}
    if "protected_routes" in inspect.signature(FastAPIDynamicRouteRegistry.__init__).parameters
    else {}
)


@dataclass(frozen=True)
class _PluginCase:
    """Production plugin namespace and uppercase runtime ID under test."""

    plugin_id: str
    plugin_class: type
    status_model: type
    test_model: type
    sync_model: type


_PLUGIN_CASES = (
    _PluginCase(
        plugin_id="JackettExtend",
        plugin_class=JackettExtend,
        status_model=JackettStatusResponse,
        test_model=JackettTestResponse,
        sync_model=JackettSyncStatus,
    ),
    _PluginCase(
        plugin_id="ProwlarrExtend",
        plugin_class=ProwlarrExtend,
        status_model=ProwlarrStatusResponse,
        test_model=ProwlarrTestResponse,
        sync_model=ProwlarrSyncStatus,
    ),
)

_STATUS_FIELDS = {
    "enabled",
    "configured",
    "connected",
    "sync",
    "indexer_count",
    "selected_count",
    "last_error",
    "last_error_at",
    "last_search_error",
    "last_search_error_at",
    "probe_error",
    "probe_error_at",
}
_REQUIRED_STATUS_FIELDS = {
    "enabled",
    "configured",
    "connected",
    "sync",
    "indexer_count",
    "selected_count",
}


@dataclass
class _RouteContext:
    """Isolated app, registry, and mocked plugin instances."""

    app: FastAPI
    registry: FastAPIDynamicRouteRegistry
    plugins: dict[str, Any]
    token_calls: int = 0


def _build_plugin(plugin_class: type) -> Any:
    """Build a plugin object without invoking host persistence or lifecycle setup."""
    plugin = object.__new__(plugin_class)
    plugin._state_lock = plugin_class._state_lock
    plugin._enabled = True
    plugin._host = "https://example.invalid"
    plugin._api_key = "test-only-key"
    plugin._indexers = [{"indexer_id": "selected"}]
    plugin._authoritative_indexers = [
        {"indexer_id": "one"},
        {"indexer_id": "two"},
    ]
    plugin._fetch_ok = True
    plugin._sync_ready = True
    plugin._last_sync_ok = True
    plugin._last_sync_at = 1720000000.25
    plugin._last_error = None
    plugin._last_error_at = 0.0
    plugin._last_search_error = "torznab_error"
    plugin._last_search_error_at = 1720000001.5

    def fake_fetch(**_kwargs: Any) -> list[dict[str, str]]:
        """Return a deterministic probe result without making an HTTP request."""
        return [{"indexer_id": "probe"}]

    setattr(plugin, f"_{plugin_class.__name__}__fetch_indexers", fake_fetch)
    return plugin


@pytest.fixture
def route_context() -> _RouteContext:
    """Create the production route registry with isolated plugin objects."""
    plugins = {
        case.plugin_id: _build_plugin(case.plugin_class)
        for case in _PLUGIN_CASES
    }
    projection = PluginProjection(plugins)
    app = FastAPI()
    context = _RouteContext(app=app, registry=None, plugins=plugins)

    def verify_token() -> None:
        context.token_calls += 1
        raise HTTPException(status_code=401, detail="token authentication is not expected")

    def verify_apikey(
        api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> bool:
        if api_key != "review-key":
            raise HTTPException(status_code=401, detail="API key required")
        return True

    logger = type(
        "Logger",
        (),
        {
            "debug": staticmethod(lambda *_args, **_kwargs: None),
            "error": staticmethod(lambda *_args, **_kwargs: None),
        },
    )()
    context.registry = FastAPIDynamicRouteRegistry(
        app=app,
        plugin_ids=lambda: [case.plugin_id for case in _PLUGIN_CASES],
        plugin_apis=projection.apis,
        verify_token=verify_token,
        verify_apikey=verify_apikey,
        prefix="/api/v1/plugin",
        **_REGISTRY_COMPAT_KWARGS,
        log=logger,
    )
    return context


def _route_paths(context: _RouteContext) -> set[str]:
    """Return only the four dynamic plugin paths under test."""
    return {
        f"/api/v1/plugin/{case.plugin_id}/{suffix}"
        for case in _PLUGIN_CASES
        for suffix in ("status", "test")
    }


def _routes(context: _RouteContext) -> list[Any]:
    """Return dynamic plugin routes currently installed in the test app."""
    paths = _route_paths(context)
    return [route for route in context.app.routes if route.path in paths]


def _assert_openapi_models(context: _RouteContext, expected_paths: set[str]) -> None:
    """Assert direct response schemas and concrete nested properties."""
    document = context.app.openapi()
    assert expected_paths <= set(document["paths"])
    operation_ids = []

    for case in _PLUGIN_CASES:
        for suffix, model, fields in (
            ("status", case.status_model, _STATUS_FIELDS),
            ("test", case.test_model, _STATUS_FIELDS | {"ok"}),
        ):
            path = f"/api/v1/plugin/{case.plugin_id}/{suffix}"
            if path not in expected_paths:
                continue
            route = next(route for route in _routes(context) if route.path == path)
            assert route.response_model is model
            assert [dependency.call for dependency in route.dependant.dependencies] == [
                context.registry._verify_apikey
            ]

            operation = document["paths"][path]["get"]
            operation_ids.append(operation["operationId"])
            assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
                "$ref": f"#/components/schemas/{model.__name__}"
            }
            component = document["components"]["schemas"][model.__name__]
            assert set(component["properties"]) == fields
            assert set(component["required"]) == _REQUIRED_STATUS_FIELDS | (
                {"ok"} if suffix == "test" else set()
            )
            assert component["properties"]["sync"] == {
                "$ref": f"#/components/schemas/{case.sync_model.__name__}"
            }
            assert component["properties"]["enabled"]["type"] == "boolean"
            assert component["properties"]["indexer_count"]["type"] == "integer"
            assert component["properties"]["last_error"]["anyOf"] == [
                {"type": "string"},
                {"type": "null"},
            ]
            if suffix == "test":
                assert component["properties"]["ok"]["type"] == "boolean"
            assert not {"success", "message", "data"} & set(component["properties"])

    assert len(operation_ids) == len(set(operation_ids)) == len(expected_paths)


def _raw_payloads(plugin: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the legacy raw dictionaries represented by both endpoints."""
    status = plugin._diagnostic_payload(probe=False)
    probe = plugin._diagnostic_payload(probe=True)
    probe["ok"] = bool(probe["connected"])
    return status, probe


def test_plugin_routes_expose_direct_models_and_preserve_json_contract(
    route_context: _RouteContext,
) -> None:
    """All four production plugin routes expose concrete direct JSON models."""
    route_context.registry.update(None, "add")
    assert len(_routes(route_context)) == 4
    expected_paths = _route_paths(route_context)
    _assert_openapi_models(route_context, expected_paths)

    with TestClient(route_context.app) as client:
        for case in _PLUGIN_CASES:
            plugin = route_context.plugins[case.plugin_id]
            raw_status, raw_test = _raw_payloads(plugin)
            assert isinstance(plugin.api_status(), case.status_model)
            assert isinstance(plugin.api_test(), case.test_model)
            assert plugin.api_status().model_dump(mode="json") == raw_status
            assert plugin.api_test().model_dump(mode="json") == raw_test

            for suffix, expected in (("status", raw_status), ("test", raw_test)):
                path = f"/api/v1/plugin/{case.plugin_id}/{suffix}"
                assert client.get(path).status_code == 401
                response = client.get(path, headers={"X-API-Key": "review-key"})
                assert response.status_code == 200
                assert response.json() == expected
                assert set(response.json()) == set(expected)
                assert not {
                    "success",
                    "message",
                    "data",
                    "host",
                    "api_key",
                    "password",
                } & set(response.json())
                assert ("ok" in response.json()) is (suffix == "test")

    assert route_context.token_calls == 0


def test_plugin_route_registry_replaces_duplicates_and_deletes_by_instance(
    route_context: _RouteContext,
) -> None:
    """Repeated registration and instance-scoped/all-route removal stay consistent."""
    route_context.registry.update(None, "add")
    all_paths = _route_paths(route_context)
    first_ids = {route.path: id(route) for route in _routes(route_context)}

    route_context.registry.update(None, "add")
    assert len(_routes(route_context)) == 4
    assert {route.path for route in _routes(route_context)} == all_paths
    assert first_ids != {route.path: id(route) for route in _routes(route_context)}
    _assert_openapi_models(route_context, all_paths)

    route_context.registry.update("JackettExtend", "remove")
    remaining_paths = {
        "/api/v1/plugin/ProwlarrExtend/status",
        "/api/v1/plugin/ProwlarrExtend/test",
    }
    assert {route.path for route in _routes(route_context)} == remaining_paths
    _assert_openapi_models(route_context, remaining_paths)
    assert not {
        "/api/v1/plugin/JackettExtend/status",
        "/api/v1/plugin/JackettExtend/test",
    } & set(route_context.app.openapi()["paths"])
    assert route_context.registry.remove("JackettExtend") is False

    route_context.registry.update(None, "remove")
    assert _routes(route_context) == []
    assert not all_paths & set(route_context.app.openapi().get("paths", {}))
    route_context.registry.update(None, "remove")
    assert _routes(route_context) == []
