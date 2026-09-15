"""V3 插件测试引导：复用 MoviePilot 宿主的隔离与网络守卫。"""

from __future__ import annotations

import os
import sys
import inspect
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
_default_backend = REPO_ROOT.parent / "MoviePilot"
BACKEND_ROOT = Path(
    os.environ.get("MOVIEPILOT_BACKEND_PATH", str(_default_backend))
).resolve()

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import mark_plugin_generation, prepare_v3_backend


# 必须在收集任何插件模块之前调用：该共享引导会隔离 CONFIG_DIR、建表，并把
# plugins.v3 暴露为生产运行时使用的 app.plugins.<plugin_id> 命名空间。
# 即使调用 pytest 的宿主进程继承了真实 CONFIG_DIR，也强制交给共享引导创建
# 本次测试专属临时目录，避免意外连接用户数据库。
os.environ.pop("CONFIG_DIR", None)
prepare_v3_backend(REPO_ROOT)

# 复用宿主同源的 autouse 零真实网络守卫。
from app.testing.network import block_real_network  # noqa: F401,E402


def _configure_plugin_runtime() -> None:
    """用隔离插件源码根装配宿主 PluginManager 的测试运行时。"""
    from app.runtime.config import settings
    from app.runtime.extensions.plugin import manager as plugin_manager_module
    from app.runtime.extensions.plugin.database import get_plugin_database
    from app.runtime.extensions.plugin.manager import PluginManager
    from app.runtime.extensions.plugin.runtime import (
        PluginRuntimeEnvironment,
        build_plugin_runtime,
    )
    from app.runtime.extensions.plugin.storage import get_plugin_storage
    from app.runtime.extensions.plugin.system import get_plugin_system

    environment_parameters = inspect.signature(PluginRuntimeEnvironment).parameters

    # MoviePilot 新版把源插件本体也纳入实例目录，由 is_enabled 决定是否装载。
    # 测试仓不应依赖用户数据库中的实例行，因此仅在宿主声明该端口时提供一个
    # 进程内目录，并把 package.v3.json 中的物理插件本体初始化为启用状态。
    instance_directory = None

    def _set_default_target(_source_plugin_id: str, _instance_id: str) -> bool:
        return False

    def _clear_default_target(_source_plugin_id: str) -> None:
        return None

    if "instance_directory" in environment_parameters:
        from app.runtime.extensions.plugin.storage import PluginInstanceDirectory
        from app.schemas.plugin import PluginInstance

        manifest = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
        instance_rows = {
            plugin_id: PluginInstance(
                instance_id=plugin_id,
                source_plugin_id=plugin_id,
                is_enabled=True,
            )
            for plugin_id in manifest
        }

        def _instance_get(instance_id: str):
            return instance_rows.get(instance_id)

        def _instance_list_all():
            return list(instance_rows.values())

        def _instance_list_by_source(source_plugin_id: str):
            return [
                row
                for row in instance_rows.values()
                if row.source_plugin_id == source_plugin_id
            ]

        def _instance_save(instance):
            instance_rows[instance.instance_id] = instance

        def _instance_delete(instance_id: str) -> bool:
            return instance_rows.pop(instance_id, None) is not None

        def _instance_list_enabled():
            return [row for row in instance_rows.values() if row.is_enabled]

        def _instance_set_enabled(instance_id: str, is_enabled: bool) -> bool:
            current = instance_rows.get(instance_id)
            if current is None:
                return False
            instance_rows[instance_id] = current.model_copy(
                update={"is_enabled": is_enabled}
            )
            return True

        instance_directory = PluginInstanceDirectory(
            get=_instance_get,
            list_all=_instance_list_all,
            list_by_source=_instance_list_by_source,
            save=_instance_save,
            delete=_instance_delete,
            list_enabled=_instance_list_enabled,
            set_enabled=_instance_set_enabled,
        )

        def _set_default_target(source_plugin_id: str, instance_id: str) -> bool:
            selected = instance_rows.get(instance_id)
            if selected is None or selected.source_plugin_id != source_plugin_id:
                return False
            for row_id, row in tuple(instance_rows.items()):
                if row.source_plugin_id == source_plugin_id:
                    instance_rows[row_id] = row.model_copy(
                        update={"is_default_target": row_id == instance_id}
                    )
            return True

        def _clear_default_target(source_plugin_id: str) -> None:
            for row_id, row in tuple(instance_rows.items()):
                if row.source_plugin_id == source_plugin_id and row.is_default_target:
                    instance_rows[row_id] = row.model_copy(
                        update={"is_default_target": False}
                    )

    def build_test_plugin_runtime(host):
        """把真实宿主运行时组件指向当前仓库的 V3 源码目录。"""
        environment_kwargs = {
            "plugins_root": REPO_ROOT / "plugins.v3",
            "storage": get_plugin_storage,
            "system": get_plugin_system,
            "database": get_plugin_database,
            "catalog_factory": lambda _mapper: None,
            "import_preparer": lambda **_kwargs: None,
            "import_scanner": lambda **_kwargs: None,
            "auth_level": lambda: 1,
            "remote_entry": host.get_plugin_remote_entry,
            "development": lambda: bool(getattr(settings, "DEV", False)),
            "logger": plugin_manager_module.logger,
        }
        if "instance_directory" in environment_parameters:
            environment_kwargs["instance_directory"] = lambda: instance_directory
        if "set_default_target" in environment_parameters:
            environment_kwargs["set_default_target"] = _set_default_target
        if "clear_default_target" in environment_parameters:
            environment_kwargs["clear_default_target"] = _clear_default_target

        return build_plugin_runtime(
            host,
            PluginRuntimeEnvironment(**environment_kwargs),
            tool_build_max_attempts=PluginManager.AGENT_TOOLS_BUILD_MAX_ATTEMPTS,
        )

    plugin_manager_module.configure_plugin_runtime_factory(build_test_plugin_runtime)


_configure_plugin_runtime()


def _configure_chain_runtime() -> None:
    """为真实插件构造提供隔离、无外部副作用的宿主 Chain 上下文。"""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.application.chain.context import (
        ChainRuntimeContext,
        configure_chain_runtime_context_provider,
    )
    from app.application.configuration import ChainRuntimeConfig
    from app.runtime.stop import runtime_stop_state

    class _MessageQueue:
        """满足 ChainBase 构造所需 bind 接口的进程内测试队列。"""

        def bind(self, _callback):
            return SimpleNamespace()

    configure_chain_runtime_context_provider(
        lambda: ChainRuntimeContext(
            module_manager=SimpleNamespace(),
            plugin_manager=SimpleNamespace(),
            event_manager=SimpleNamespace(),
            message_oper=SimpleNamespace(),
            message_helper=SimpleNamespace(),
            file_cache=SimpleNamespace(),
            async_file_cache=SimpleNamespace(),
            message_queue=_MessageQueue(),
            module_dispatcher_factory=lambda **_kwargs: SimpleNamespace(),
            site_repository=SimpleNamespace(),
            subscription_repository=SimpleNamespace(),
            subscription_mutation_scope=nullcontext,
            sync_subscription_mutation_scope=nullcontext,
            subscription_delete_scope=nullcontext,
            sync_subscription_delete_scope=nullcontext,
            subscription_completion_scope=nullcontext,
            rule_group_mutation_scope=nullcontext,
            site_reference_mutation_scope=nullcontext,
            download_history_repository=SimpleNamespace(),
            transfer_history_repository=SimpleNamespace(),
            transfer_admission_repository=SimpleNamespace(),
            transfer_execution_repository=SimpleNamespace(),
            media_server_repository=SimpleNamespace(),
            download_failure_repository=SimpleNamespace(),
            user_repository=SimpleNamespace(),
            configuration=ChainRuntimeConfig(media_extensions=()),
            stop_state=runtime_stop_state,
        )
    )


_configure_chain_runtime()


@pytest.fixture(scope="session", autouse=True)
def restore_host_test_runtime():
    """会话结束时撤销本仓测试注入的宿主进程级 provider。"""
    yield
    from app.application.chain.context import configure_chain_runtime_context_provider
    from app.runtime.extensions.plugin.manager import reset_plugin_runtime_factory

    configure_chain_runtime_context_provider(None)
    reset_plugin_runtime_factory()


def pytest_configure(config) -> None:
    """注册代际标记并确保 V3 用例可按目录筛选。"""
    config.addinivalue_line("markers", "v3: MoviePilot V3 plugin tests")
    config.addinivalue_line("markers", "ci: repository and workflow contract tests")


def pytest_collection_modifyitems(session, config, items) -> None:
    """按官方共享引导给 V3 用例打标，便于 CI 和本地筛选。"""
    del session, config
    mark_plugin_generation(items, pytest)
