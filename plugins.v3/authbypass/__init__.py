# _*_ coding: utf-8 _*_
"""认证绕过插件。

MoviePilot 的用户认证等级来自闭源资源扩展 ``app.application.site.sites``：
登录令牌、插件准入、站点写操作和启动/定时自检都在读 ``SitesHelper.auth_level``。
本插件在进程内替换该属性与 ``check_user()``，把等级固定为已认证（2），
从而跳过认证；停用插件时恢复原始行为。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from app.sdk.plugin import _PluginBase
except (ImportError, AttributeError):  # MoviePilot v3.0.3 之前的旧命名空间
    from app.plugins import _PluginBase

from app.sdk.logging import logger

from . import _patch
from ._ui import build_form, build_page


class AuthBypass(_PluginBase):
    """把站点认证等级固定为已认证的插件。"""

    # 插件名称
    plugin_name = "认证绕过"
    # 插件描述
    plugin_desc = "把站点用户认证等级固定为已认证（2），跳过用户认证与站点认证校验"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/oexi/MoviePilot-Plugins/main/icons/AuthBypass.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "oexi"
    # 作者主页
    author_url = "https://github.com/oexi"
    # 插件配置项ID前缀
    plugin_config_prefix = "authbypass_"
    # 加载顺序，必须早于需要认证的插件，让它们在准入判断前看到已认证等级
    plugin_order = 0
    # 可使用的用户级别：0 表示未认证也能加载
    auth_level = 0

    def __init__(self) -> None:
        """初始化运行状态。"""
        super().__init__()
        self._enabled: bool = False
        self._auto_reload: bool = True
        self._bypass_plugin_policy: bool = True
        self._applied: bool = False
        self._message: str = "未启用"

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """按配置安装或撤销补丁。"""
        conf = dict(config or {})
        self._enabled = bool(conf.get("enabled", False))
        self._auto_reload = bool(conf.get("auto_reload", True))
        self._bypass_plugin_policy = bool(conf.get("bypass_plugin_policy", True))

        if self._enabled:
            ok, message = _patch.apply(bypass_plugin_policy=self._bypass_plugin_policy)
            self._applied = ok
            self._message = message
            logger.info(f"【{self.plugin_name}】{message}")
            if ok and self._auto_reload:
                _patch.reload_plugins_once()
        else:
            ok, message = _patch.revert()
            self._applied = False
            self._message = message
            logger.info(f"【{self.plugin_name}】{message}")

        self.update_config(
            {
                "enabled": self._enabled,
                "auto_reload": self._auto_reload,
                "bypass_plugin_policy": self._bypass_plugin_policy,
            }
        )

    def get_state(self) -> bool:
        """返回补丁是否处于生效状态。"""
        return bool(self._enabled and self._applied)

    def stop_service(self) -> None:
        """停用插件时撤销补丁，避免认证门禁一直处于关闭状态。"""
        try:
            _patch.revert()
        except Exception as error:  # noqa: BLE001 - 停用流程不允许抛异常
            logger.warning(f"【{self.plugin_name}】撤销补丁失败：{type(error).__name__}: {error}")
        finally:
            self._applied = False

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单。"""
        return build_form(
            enabled=self._enabled,
            auto_reload=self._auto_reload,
            bypass_plugin_policy=self._bypass_plugin_policy,
        )

    def get_page(self) -> Optional[List[dict]]:
        """返回状态详情页。"""
        snapshot = _patch.status()
        if self._message and not snapshot.get("reason"):
            snapshot["reason"] = self._message
        return build_page(snapshot)

    def get_api(self) -> List[Dict[str, Any]]:
        """注册只读状态接口。"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "认证绕过状态",
            }
        ]

    def api_status(self) -> Dict[str, Any]:
        """返回补丁状态。"""
        snapshot = _patch.status()
        snapshot["enabled"] = self._enabled
        snapshot["message"] = self._message
        return snapshot
