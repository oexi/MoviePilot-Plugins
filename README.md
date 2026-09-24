# MoviePilot Plugins

MoviePilot 插件仓库，当前仅提供 **MoviePilot V3** 插件。

## 当前插件

### JackettExtend

- **版本**：3.2.22
- **适用版本**：MoviePilot V3
- **功能**：
  - 同步 Jackett 中已配置的 indexer，并在 MoviePilot 中生成对应站点
  - 支持电影、电视剧和音乐资源搜索
  - 支持 Jackett 登录密码、代理和搜索超时配置
  - 支持 indexer 白名单和定时同步
  - 支持站点状态、索引器类型和搜索结果展示
  - 普通停止、宿主关闭和热重载保留虚拟站点及用户设置；明确禁用时清理自身站点

### ProwlarrExtend

- **版本**：1.0.9
- **适用版本**：MoviePilot V3
- **功能**：
  - 同步 Prowlarr 中已启用且支持搜索的 Torrent indexer，并在 MoviePilot 中生成对应站点
  - 通过 Prowlarr Torznab API 搜索电影、电视剧和音乐资源
  - 支持代理、搜索超时、indexer 白名单和定时同步
  - 可与 JackettExtend 同时启用，分别管理各自的虚拟站点
  - 普通停止、宿主关闭和热重载保留虚拟站点及用户设置；明确禁用时清理自身站点

## 安装与配置

1. 在 MoviePilot V3 的插件市场中添加仓库：
   `https://github.com/Oexi/MoviePilot-Plugins`
2. 刷新插件市场并安装 `JackettExtend` 或 `ProwlarrExtend`。
3. 打开对应插件配置，填写服务地址和 API Key。JackettExtend 还可填写 Web 登录密码；两个插件均可配置代理、搜索超时、indexer 白名单和同步周期。
4. 保存配置。插件会同步所选服务的 indexer，并将可用站点交给 MoviePilot 管理。

配置前应先在对应服务中添加 indexer。JackettExtend 可额外配置 Web 登录密码；ProwlarrExtend 只同步已启用、支持搜索且协议为 Torrent 的 indexer。站点同步完成后，可在 MoviePilot 的站点管理中查看和启用对应站点。

卸载前如需清理虚拟站点，请先关闭插件的启用开关并保存，确认清理完成后再卸载。当前宿主没有独立的卸载回调；直接卸载会保留站点记录，避免把热重载或宿主关闭误判为永久删除。明确禁用会发送 `SiteDeleted`，宿主将清理相应的搜索和订阅站点引用。

## 版本说明

- 本仓库仅维护 V3 实现，不再提供 MoviePilot V2 插件。
- `jackett_extend.<indexer>` 是 V3 已使用的虚拟站点标识，升级时会保留该前缀以兼容已有站点数据。
- `prowlarr_extend.<indexer>` 是 ProwlarrExtend 使用的独立虚拟站点标识，不会接管 JackettExtend 站点。

## 虚拟分身与诊断接口

同类插件的虚拟分身按运行实例 ID 隔离搜索桥接和虚拟站点，可分别连接不同的 Jackett 或 Prowlarr 服务。源实例保留已有站点标识；修改分身显示名称不会改变站点归属。禁用一个实例只清理该实例管理的站点。

两个插件均提供只读 `/status` 和 `/test` 接口，完整路径为 `/api/v1/plugin/<实例ID>/status` 和 `/api/v1/plugin/<实例ID>/test`。接口保留 API Key 认证和原有裸 JSON 结构，响应字段由具体模型校验，并可在宿主 OpenAPI 中查看。`/test` 会向对应服务发起连接探测；`/status` 只读取缓存状态。返回数据不包含服务地址、API Key 或登录密码。

## 目录结构

```text
plugins.v3/jackettextend/   # JackettExtend V3 插件
plugins.v3/prowlarrextend/  # ProwlarrExtend V3 插件
package.v3.json             # V3 插件市场信息
tests/v3/                   # V3 插件及宿主集成测试
tests/ci/                   # 仓库元数据与共享模块检查
```

## 开发验证

将官方 MoviePilot V3 宿主放在本仓库同级的 `MoviePilot/`，使用其锁定依赖环境运行：

```bash
../MoviePilot/.venv/bin/python tests/run.py -q
../MoviePilot/.venv/bin/python tools/sync_shared_modules.py --check
```

宿主位于其它目录时，设置 `MOVIEPILOT_BACKEND_PATH` 并使用该宿主的 Python 解释器。测试复用宿主共享引导，强制使用临时配置目录，并拦截真实网络请求；仓库检查与 V3 测试在独立进程中执行。

## 免责声明

本项目及其插件仅供学习和交流使用。请遵守当地法律法规，并自行承担使用本项目产生的责任。

## 许可证

本项目采用 GPL-3.0，详见 [LICENSE](LICENSE)。
