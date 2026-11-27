# 认证绕过（AuthBypass）

把 MoviePilot 的站点用户认证等级在进程内固定为「已认证」，跳过用户认证与站点认证校验，
让依赖 `auth_level >= 2` 的插件与站点功能直接可用。

- 插件 ID：`AuthBypass`
- 版本：`1.0.0`
- 作者：`oexi`
- 自身认证等级：`0`（未认证状态也能加载）
- 修改范围：仅运行时对象，不改宿主代码、不改资源包 `.so` / `.bin`，停用即恢复
- 图标：`icons/AuthBypass.png`。插件类与 `package.v3.json` 都使用 raw 完整 URL——
  MoviePilot 前端只对 `http(s)` 开头的图标走图片代理，文件名形式会去前端内置的
  `plugin_icon/` 目录找，而该目录只打包官方插件仓的图标

## 原理

MoviePilot 的认证门禁都读取资源扩展 `app.application.site.sites.SitesHelper`：

| 位置 | 用途 |
| --- | --- |
| `app/api/endpoints/login.py` | 登录令牌里的 `level` |
| `app/application/security/auth.py` | 登录响应与令牌载荷 |
| `app/runtime/extensions/plugin/access.py` | 插件准入 `level >= plugin.auth_level` |
| `app/application/site/mutation.py` | 站点新增/修改/删除 `auth_level < 2` 拒绝 |
| `app/startup/initializers/modules.py` | 启动自检与自动认证 |
| `app/scheduler/reconcile.py` | 定时重试认证 |

插件在 `init_plugin()` 中执行两个替换，并可选地放行 `auth_level=99` 的公钥插件策略：

```python
SitesHelper.auth_level = property(lambda self: 2)
SitesHelper.check_user = lambda self, site=None, params=None: (True, site or "auth-bypass")
PluginAccessPolicy.check = lambda self, plugin, source=None: True
```

`auth_level` 在资源扩展里是普通 `property`、`check_user` 是普通方法，二者都可原位替换并原样还原；
其余方法（`auth_version`、`indexer_version`、`get_authsites`、`get_indexsites`、`get_indexer`、流控）
保持原实现，站点清单与索引器逻辑不受影响。

## 安装

### 方式一：本地插件仓库（推荐）

1. 把本目录放到插件仓库的 `plugins.v3/authbypass/`，并在 `package.v3.json` 中登记 `AuthBypass`（本仓库已包含）。
2. MoviePilot 配置 `PLUGIN_LOCAL_REPO_PATHS` 指向该仓库根目录（多个用 `,` 分隔）。
3. 重启后进入「插件」页面，在市场中安装「认证绕过」。
4. 打开插件，勾选「启用认证绕过」并保存。

### 方式二：直接放入运行目录

```bash
cp -r plugins.v3/authbypass /path/to/MoviePilot/app/plugins/authbypass
```

再通过插件页面安装/启用（需要在实例表里登记本体，正常安装流程会完成这一步）。
只复制目录不登记实例时，宿主不会装载该插件。

## 使用

- 保存配置后补丁立即生效；勾选「自动重载受策略拦截的插件」时，插件会在约 3 秒后调用一次
  `init_config()`，让此前 `BLOCKED_BY_POLICY` 的插件重新加载。
- 详情页显示当前等级与策略状态；`GET /api/v1/plugin/AuthBypass/status`（bear 认证）返回同一份快照。
- 停用插件或重启宿主后，补丁由插件重新安装；卸载后完全恢复原始认证行为。

## 已验证

在 MoviePilot v3 宿主 + `MoviePilot-Resources` v3（`auth_version 3.0.10`、`indexer_version 3.0.36`）上验证：

```text
before auth_level: 1
apply: 认证等级已固定为 2；插件等级策略已放行
after auth_level: 2
check_user(): (True, 'auth-bypass')
check_user(site='mteam'): (True, 'mteam')
get_indexsites(): 200 条，未受影响
revert: auth_level 恢复为 1
```
