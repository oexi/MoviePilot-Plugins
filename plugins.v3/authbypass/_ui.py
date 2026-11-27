# _*_ coding: utf-8 _*_
"""认证绕过插件的纯 UI 构造器。

不导入宿主、不读插件状态，便于单元测试与复用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


def build_form(
    enabled: bool = False,
    auto_reload: bool = True,
    bypass_plugin_policy: bool = True,
) -> Tuple[List[dict], Dict[str, Any]]:
    """构造配置表单及默认值。"""
    schema = [
        {
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enabled",
                                        "label": "启用认证绕过",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "auto_reload",
                                        "label": "自动重载受策略拦截的插件",
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "bypass_plugin_policy",
                                        "label": "放行 auth_level=99 的特殊插件",
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "title": "工作原理",
                                        "text": (
                                            "把资源扩展 SitesHelper.auth_level 固定为 2，并让 "
                                            "SitesHelper.check_user() 直接返回成功。登录令牌等级、"
                                            "插件准入、站点新增/修改/删除、启动与定时认证检查都读取"
                                            "这两个成员，因此无需修改宿主代码或资源文件。"
                                        ),
                                    },
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    ]
    return schema, {
        "enabled": enabled,
        "auto_reload": auto_reload,
        "bypass_plugin_policy": bypass_plugin_policy,
    }


def build_page(status: Dict[str, Any]) -> List[dict]:
    """构造详情页：当前补丁状态与生效范围。"""
    applied = bool(status.get("applied"))
    level = status.get("level")
    if applied:
        alert_type = "success"
        title = f"认证绕过已生效（auth_level = {level}）"
        text = (
            "站点认证、插件准入与站点写操作均已放行。资源扩展本身的版本与站点清单未做改动，"
            "停用插件后会恢复原始认证行为。"
        )
    else:
        alert_type = "warning"
        title = "认证绕过未生效"
        text = (
            "请确认站点资源已安装（SitesHelper 可导入）并勾选“启用认证绕过”后保存。"
            "若刚启用，等待几秒让插件管理器重载被拦截的插件。"
        )

    rows = [
        {"key": "认证等级", "value": str(level) if applied else "未接管"},
        {"key": "插件策略放行", "value": "是" if status.get("policy_patched") else "否"},
        {"key": "说明", "value": status.get("reason") or "补丁作用于当前进程，重启后由插件重新安装"},
    ]
    return [
        {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VAlert",
                            "props": {
                                "type": alert_type,
                                "variant": "tonal",
                                "title": title,
                                "text": text,
                            },
                        },
                        {
                            "component": "VTable",
                            "props": {"hover": True},
                            "content": [
                                {
                                    "component": "tbody",
                                    "content": [
                                        {
                                            "component": "tr",
                                            "content": [
                                                {
                                                    "component": "td",
                                                    "props": {"class": "text-start ps-4"},
                                                    "text": item["key"],
                                                },
                                                {
                                                    "component": "td",
                                                    "props": {"class": "text-start ps-4"},
                                                    "text": item["value"],
                                                },
                                            ],
                                        }
                                        for item in rows
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    ]
