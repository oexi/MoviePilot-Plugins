# _*_ coding: utf-8 _*_
"""认证绕过补丁。

MoviePilot 的认证门禁全部读取资源扩展里的 ``SitesHelper``：

- ``auth_level`` 决定登录令牌等级、插件准入和站点写操作；
- ``check_user()`` 就是"用户认证"动作本身。

因此把这两个成员替换掉即可整体跳过认证，不需要改宿主代码，也不需要改资源文件。
补丁可逆：停用插件时恢复原始描述符。
"""
from __future__ import annotations

import importlib
import threading
import time
from typing import Any, Dict, Optional, Tuple

SITES_MODULE = "app.application.site.sites"
POLICY_MODULE = "app.runtime.extensions.plugin.access"
BYPASS_LEVEL = 2

_ORIGINAL_ATTR = "_authbypass_original"
_STATE_ATTR = "_authbypass_state"
_POLICY_ORIGINAL_ATTR = "_authbypass_policy_original"

_lock = threading.RLock()
_reload_started = False


def _log(message: str) -> None:
    """延迟导入日志，避免插件模块导入期依赖宿主日志实现。"""
    try:
        from app.sdk.logging import logger

        logger.info(message)
    except Exception:  # noqa: BLE001 - 日志失败不影响补丁
        pass


def _sites_helper() -> Any:
    """返回资源扩展的 SitesHelper 类。"""
    module = importlib.import_module(SITES_MODULE)
    helper = getattr(module, "SitesHelper", None)
    if helper is None:
        raise RuntimeError("站点资源未安装：SitesHelper 不存在")
    return helper


def _check_user(self: Any, site: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """{SitesHelper.check_user} 的替身：总是返回认证成功。

    保持原签名，宿主既可能用 ``check_user()``，也可能用
    ``check_user(site=..., params=...)`` 调用。
    """
    return True, (site or "auth-bypass")


def _patch_plugin_policy() -> Tuple[bool, str]:
    """放行 auth_level=99 的公钥插件，补上最后一类准入判断。"""
    try:
        module = importlib.import_module(POLICY_MODULE)
        policy = getattr(module, "PluginAccessPolicy", None)
        if policy is None:
            return False, "宿主没有 PluginAccessPolicy"
        if not hasattr(policy, _POLICY_ORIGINAL_ATTR):
            setattr(policy, _POLICY_ORIGINAL_ATTR, policy.check)
        policy.check = lambda self, plugin, source=None: True  # noqa: ARG005
        return True, "插件等级策略已放行"
    except Exception as error:  # noqa: BLE001 - 不同宿主版本可能没有该模块
        return False, f"插件等级策略放行失败：{type(error).__name__}: {error}"


def _restore_plugin_policy() -> None:
    """恢复插件等级策略。"""
    try:
        module = importlib.import_module(POLICY_MODULE)
        policy = getattr(module, "PluginAccessPolicy", None)
        original = getattr(policy, _POLICY_ORIGINAL_ATTR, None) if policy else None
        if policy is not None and original is not None:
            policy.check = original
            delattr(policy, _POLICY_ORIGINAL_ATTR)
    except Exception:  # noqa: BLE001 - 恢复失败不应阻断停用流程
        pass


def apply(bypass_plugin_policy: bool = True) -> Tuple[bool, str]:
    """安装补丁，返回 (是否成功, 说明)。"""
    with _lock:
        try:
            helper = _sites_helper()
        except Exception as error:  # noqa: BLE001 - 资源缺失时给出可读原因
            return False, f"站点资源不可用：{type(error).__name__}: {error}"

        original = getattr(helper, _ORIGINAL_ATTR, None)
        if original is None:
            # 类上的 auth_level 是 property 对象，check_user 是 cyfunction，直接留引用即可还原
            setattr(helper, _ORIGINAL_ATTR, (helper.auth_level, helper.check_user))

        helper.auth_level = property(lambda self: BYPASS_LEVEL)
        helper.check_user = _check_user
        setattr(helper, _STATE_ATTR, {"applied": True, "level": BYPASS_LEVEL})

        message = f"认证等级已固定为 {BYPASS_LEVEL}"
        if bypass_plugin_policy:
            _, detail = _patch_plugin_policy()
            message = f"{message}；{detail}"
        _log(f"【认证绕过】{message}")
        return True, message


def revert() -> Tuple[bool, str]:
    """卸载补丁，返回 (是否成功, 说明)。"""
    with _lock:
        try:
            helper = _sites_helper()
        except Exception as error:  # noqa: BLE001
            return False, f"站点资源不可用：{type(error).__name__}: {error}"

        original = getattr(helper, _ORIGINAL_ATTR, None)
        if original is None:
            _restore_plugin_policy()
            return True, "补丁原本未生效"
        level, check_user = original
        helper.auth_level = level
        helper.check_user = check_user
        try:
            delattr(helper, _ORIGINAL_ATTR)
        except Exception:  # noqa: BLE001
            pass
        setattr(helper, _STATE_ATTR, {"applied": False, "level": BYPASS_LEVEL})
        _restore_plugin_policy()
        _log("【认证绕过】补丁已撤销")
        return True, "补丁已撤销"


def status() -> Dict[str, Any]:
    """返回补丁状态快照。"""
    with _lock:
        try:
            helper = _sites_helper()
        except Exception as error:  # noqa: BLE001
            return {"applied": False, "level": None, "reason": f"{type(error).__name__}: {error}"}
        state = getattr(helper, _STATE_ATTR, None) or {}
        return {
            "applied": bool(state.get("applied")),
            "level": BYPASS_LEVEL if state.get("applied") else None,
            "policy_patched": _policy_patched(),
        }


def _policy_patched() -> bool:
    """判断插件等级策略当前是否被本补丁放行。"""
    try:
        module = importlib.import_module(POLICY_MODULE)
        policy = getattr(module, "PluginAccessPolicy", None)
        return bool(policy is not None and hasattr(policy, _POLICY_ORIGINAL_ATTR))
    except Exception:  # noqa: BLE001
        return False


def reload_plugins_once(delay: float = 3.0) -> None:
    """进程内只安排一次插件重载。

    认证等级是在插件加载时判定的：补丁生效后必须让插件管理器重新走一遍
    start()，此前被 BLOCKED_BY_POLICY 的插件才会真正加载。只重载一次，
    避免 init_config() -> init_plugin() -> init_config() 的递归。
    """
    global _reload_started
    with _lock:
        if _reload_started:
            return
        _reload_started = True

    def worker() -> None:
        try:
            time.sleep(max(0.0, delay))
            from app.application.plugin.runtime import get_plugin_manager

            manager = get_plugin_manager()
            try:
                statuses = manager.get_plugin_runtime_statuses() or {}
                blocked = [
                    pid
                    for pid, value in statuses.items()
                    if str(getattr(value, "value", value)).lower() == "blocked_by_policy"
                ]
            except Exception:  # noqa: BLE001 - 状态接口不可用时直接重载
                blocked = []
            if blocked:
                _log(f"【认证绕过】重载被策略拦截的插件：{', '.join(blocked)}")
                manager.init_config()
        except Exception as error:  # noqa: BLE001 - 后台线程不允许把异常抛给宿主
            _log(f"【认证绕过】插件重载失败：{type(error).__name__}: {error}")

    threading.Thread(target=worker, name="authbypass-reload", daemon=True).start()
