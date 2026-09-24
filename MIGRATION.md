# JackettExtend V3-only 迁移说明

JackettExtend 现仅支持当前 MoviePilot V3，插件版本为 `3.2.22`，由 oexi 独立维护。原始项目作者为 jtcymc，原始项目地址为 <https://github.com/jtcymc/MoviePilot-PluginsV2>。

V2 版 `ProwlarrExtend` 已删除，仓库不再提供 MoviePilot V2 插件；`package.json` 和 `package.v2.json` 也已移除。

V3 实现优先使用当前公开入口：网络适配器来自 `app.sdk.network`，媒体模型来自 `app.sdk.media`，日志、配置、工具和事件来自 `app.sdk.*`，插件基类来自 `app.plugins`，枚举来自当前 `app.schemas`/`app.schemas.types` 合同。站点持久化通过 `_site_registry.py` 使用官方允许的 `app.db.oper.site.SiteOper` 公开方法，不直接操作宿主 Model 或裸 Session；该适配器用于集中管理持久化与事件边界。

当前 V3 的按站点同步/异步搜索及刷新入口仍将系统模块作为调用边界，因此插件保留最小的生命周期管理桥接（`ChainBase` 从稳定 SDK `app.sdk.chain` 获取，不经过 `app.chain` 包根兼容符号），覆盖 `ChainBase.search_site_torrents`、`async_search_site_torrents`、`refresh_torrents` 和 `async_refresh_torrents`（宿主未提供的刷新入口会跳过），且只接管非空 Mapping 中命中对应虚拟站点 predicate 的站点。普通站点、`site={}` 的全局插件调用以及 `get_search_page_size` 继续由宿主/当前 `run_module(..., public_to_plugins=True)` 分发处理；page-size provider 由 `JackettExtend.get_module()` 注册。启停、重载和多实例切换会恢复或更新唯一桥接所有者。

已注册站点使用既有 `jackett_extend.<indexer>` 持久化域名前缀，以便现有 V3 数据在升级后继续识别；这不是对 V2 实现的支持。

虚拟分身使用运行实例 ID 隔离桥接 owner、站点归属与注册标识。源实例保留既有域名前缀和站点记录；分身不接管源实例记录，修改显示名称也不会改变持久化身份。升级前已因多实例共用旧前缀而覆盖的站点，无法仅从旧域名确定原归属，不自动将这些记录转移给分身。

诊断 API 保留原有 API Key 认证及裸 JSON 字段，新增具体 Pydantic 响应模型供 FastAPI 校验与生成 OpenAPI。没有改为 `success/message/data` envelope，原客户端无需增加解包步骤。

普通停止、宿主关闭和热重载仅停止插件运行资源，保留站点 ID、用户设置以及搜索和订阅中的站点引用。明确禁用并保存配置时，才按各自保留的虚拟域名前缀清理已注册站点并发送 `SiteDeleted` 事件；JackettExtend 与 ProwlarrExtend 的站点命名空间彼此隔离。

当前宿主的卸载和热重载共用停止入口，没有独立的卸载回调。需要清理站点时，请先禁用并保存，确认清理完成后再卸载；直接卸载会保留虚拟站点记录。
