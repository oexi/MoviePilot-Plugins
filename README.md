<div align="center">

# MoviePilot Plugins

为 [MoviePilot V3](https://github.com/jxxghp/MoviePilot) 接入 Jackett / Prowlarr 索引器的插件仓库

[![CI](https://github.com/oexi/MoviePilot-Plugins/actions/workflows/ci.yml/badge.svg)](https://github.com/oexi/MoviePilot-Plugins/actions/workflows/ci.yml)
![MoviePilot](https://img.shields.io/badge/MoviePilot-V3-3b82f6)
[![License](https://img.shields.io/badge/license-GPL--3.0-green)](LICENSE)

</div>

## 插件

| | 插件 | 版本 | 说明 |
|:-:|---|:-:|---|
| <img src="icons/Jackett_A.png" width="40"> | **JackettExtend** | 3.2.23 | 同步 Jackett indexer 为 MoviePilot 站点，支持电影、剧集、音乐搜索 |
| <img src="icons/Prowlarr.png" width="40"> | **ProwlarrExtend** | 1.0.10 | 同步 Prowlarr Torrent indexer，通过 Torznab API 搜索 |

**共同特性**

- 🔄 indexer 白名单与定时同步，站点自动注册到 MoviePilot 站点管理
- 🌐 代理、搜索超时可配置；JackettExtend 额外支持 Web 登录密码
- 🧩 两个插件可同时启用，站点命名空间互相隔离；支持虚拟分身连接多个服务
- 🛡️ 热重载、重启不会丢失站点和设置；仅在明确禁用时清理自身站点
- 🩺 提供只读诊断接口 `/status`、`/test`

## 安装

1. 在 MoviePilot 插件市场中添加仓库：

   ```text
   https://github.com/Oexi/MoviePilot-Plugins
   ```

2. 刷新插件市场，安装 `JackettExtend` 或 `ProwlarrExtend`。
3. 填写服务地址与 API Key，按需设置代理、白名单和同步周期，保存即可。

> [!TIP]
> 请先在 Jackett / Prowlarr 中添加 indexer。ProwlarrExtend 只同步已启用、支持搜索且协议为 Torrent 的 indexer。

> [!IMPORTANT]
> 宿主没有独立的卸载回调。如需清理虚拟站点，请**先关闭启用开关并保存**，确认清理完成后再卸载；直接卸载会保留站点记录。

## 诊断接口

```text
GET /api/v1/plugin/<实例ID>/status   # 读取缓存状态
GET /api/v1/plugin/<实例ID>/test     # 向服务发起连接探测
```

需 API Key 认证；响应不包含服务地址、API Key 或密码，字段可在宿主 OpenAPI 中查看。

## 开发

将 MoviePilot V3 宿主放在同级目录 `../MoviePilot/`（或设置 `MOVIEPILOT_BACKEND_PATH`）：

```bash
../MoviePilot/.venv/bin/python tests/run.py -q
../MoviePilot/.venv/bin/python tools/sync_shared_modules.py --check
```

```text
plugins.v3/      插件源码
shared/          插件间共享模块
package.v3.json  插件市场元数据
tests/           插件与宿主集成测试
```

兼容性与迁移细节见 [MIGRATION.md](MIGRATION.md)。

## 许可

[GPL-3.0](LICENSE) · 仅供学习交流使用，请遵守当地法律法规。
