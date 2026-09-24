# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a MoviePilot **V3** plugin repository (V2 is no longer shipped). It contains two indexer plugins: `plugins.v3/jackettextend` (JackettExtend) and `plugins.v3/prowlarrextend` (ProwlarrExtend). Plugin-market metadata lives in `package.v3.json`. The plugins have no environment of their own; development and tests run against the official host source, `jxxghp/MoviePilot` branch `v3`.

## Commands

The host is expected at the sibling directory `../MoviePilot`. For any other location, set `MOVIEPILOT_BACKEND_PATH` and use that host's interpreter.

```bash
# Prepare/update the host environment (run in the host dir; default groups include dev, i.e. pytest)
cd ../MoviePilot && git pull --ff-only origin v3 && uv sync --locked

# Full suite: tests/ci and tests/v3 run in separate pytest processes
../MoviePilot/.venv/bin/python tests/run.py -q

# Single file / single test (must run from the repo root; conftest depends on it)
../MoviePilot/.venv/bin/python -m pytest tests/v3/prowlarrextend/test_prowlarrextend.py -q -k refresh

# Mirror shared/ canonical sources into both plugins; --check only verifies (run by CI)
../MoviePilot/.venv/bin/python tools/sync_shared_modules.py
../MoviePilot/.venv/bin/python tools/sync_shared_modules.py --check

# Additional CI checks
../MoviePilot/.venv/bin/python -m json.tool package.v3.json >/dev/null
git diff --check
```

The repository has no lint configuration of its own and CI does not run ruff. The host's ruff reports a set of pre-existing findings; changes only need to avoid adding new ones.

## Architecture

### Shared module mirrors
`_host_compat.py`, `_site_registry.py`, `_torznab_core.py` and `_response.py` under `shared/` are the canonical sources. The same-named files inside each plugin directory are byte-for-byte mirrors, because plugins are packaged independently and cannot import each other at runtime. Edit only `shared/`, then run the sync script; `tests/ci/test_repository_contract.py` and CI verify the mirrors. Each plugin keeps its own `_indexers.py`, `_torznab.py`, `_ui.py` and `_api_models.py` for Jackett/Prowlarr-specific logic. The two `__init__.py` files are structurally parallel, so a fix in one usually needs checking in the other.

### Virtual sites
Each Jackett/Prowlarr indexer appears in MoviePilot as a "virtual site" that exists in two places:
- **In-memory profile**: injected via `SitesHelper.add_indexer`, built by `_indexers.build_indexer_profiles`. Profiles carry `indexer_id` and the `plugin`/`parser` owner markers.
- **DB site row**: written through `_site_registry.open_site_registry()`. The entry `__init__.py` must not import `app.db.oper.site` or `app.sdk.events` directly (enforced by the repository contract test); the host currently has no SDK for site persistence.

The domain prefixes `jackett_extend.` / `prowlarr_extend.` are the identity of persisted data and must not change. Virtual clone instances get an isolated hashed prefix from `build_instance_domain_prefix`. Sync updates only the three source fields `name/url/public`; user settings (active flag, priority, proxy) are never overwritten. `SiteUpdated` is sent only when a site is added or its source fields change: in the host this event only triggers icon fetching, site-config cleanup and userdata refresh — it does not refresh any site cache.

### Search routing bridge (`_host_compat.py`, must be kept)
The host's `ChainBase.search_site_torrents` calls system modules only; plugins are called once, globally, with `site={}`. The refresh path (`run_module`) uses `ORDERED_LIST_MERGE`, so even after a plugin returns results the system `IndexerModule` still spiders the virtual site. The bridge therefore patches `ChainBase.search_site_torrents`, `async_search_site_torrents`, `refresh_torrents` and `async_refresh_torrents`, routes only sites matched by the owner predicate to the plugin, and hands everything else back to the host unchanged. Key constraints:
- Bridge state is attached to `ChainBase`, with multiple owners keyed by `owner_key` (runtime instance ID), weak references and an ABI version. Jackett and Prowlarr share one wrapper layer, so changes must stay compatible across module copies and older plugin versions.
- `ChainBase` is resolved from `app.sdk.chain`, falling back to `app.chain.base`. Do not use the `app.chain` package root, which is the host's Compat layer. Tests inject a fake host by replacing `app.sdk.chain.ChainBase`.
- Plugin exceptions always fall back to the host's original method. The only exception is `SanitizedUpstreamError`, which may be raised only from Prowlarr's **async** refresh path so the site-browse page can distinguish "no resources" from "request failed". Synchronous `refresh_torrents` must return `[]`, because the host's subscription refresh loop `TorrentsChain.refresh` has no per-site exception isolation.
- `get_search_page_size` and global plugin calls stay with the host dispatcher; do not add them to the bridge.

### Lifecycle and concurrency
- Configuration is published as an immutable snapshot, `_config_snapshot`. Every `init_plugin` or stop increments `_sync_generation`. Network requests run outside locks; each DB write, config write or event send first checks `_sync_is_current(generation)`, so tasks from an older generation cannot commit results. There are two locks: `_sync_lock` guards the short commit phase and `_state_lock` guards state fields.
- Plugins never create their own threads. The initial sync (a one-shot `date` job) and the periodic sync (cron) are both handed to the host scheduler via `get_service()`.
- `stop_service()` (stop, reload, host shutdown) keeps sites. Only initializing with a disabled config deletes the plugin's own-prefix sites and sends `SiteDeleted`. The host has no separate uninstall callback.
- Failed fetches, empty results and cached snapshots never authorize destructive cleanup. If every entry of a finite whitelist becomes stale, the original config is kept rather than degrading to "register all".

### Network and parsing
- Use `app.sdk.network.RequestUtils` and pass proxies through the **constructor** `proxies=`; the host's "retry proxied HTTPS with TLS 1.2" only reads it there. Responses are always streamed and bounded with `read_limited_response`.
- Torznab is parsed with stdlib minidom, rejecting any DTD before parsing. Logs record only error categories and use `redact_url`; never log raw keywords, API keys or host addresses. Prowlarr authenticates with the `X-Api-Key` header; Jackett's torznab uses the `apikey` query parameter.
- The diagnostic APIs `/status` and `/test` return bare JSON without a `success/message/data` envelope, validated by the Pydantic models in `_api_models.py`.

## Testing conventions

- `tests/conftest.py` calls the host's `app.testing.bootstrap.prepare_v3_backend`: it isolates `CONFIG_DIR`, exposes `plugins.v3` as `app.plugins.<id>` and enables the host's network guard. It passes `PluginRuntimeEnvironment` arguments conditionally based on the host signature; when the host adds a new required port (e.g. `runtime_declaration`), add a test implementation here.
- Plugin tests use `loaded_module()` to temporarily replace module globals (`RequestUtils`, `settings`, `TorrentInfo`, ...), restoring them on exit and reclaiming bridge owners. Regression tests that cross the host boundary should prefer real host objects such as `TorrentsChain` and `PluginManager`.

## Release conventions

When plugin behavior changes, update together:
- the `plugin_version` class attribute;
- `version` in `package.v3.json`, plus a new Chinese entry in `history`;
- the version numbers in README and MIGRATION;
- the JackettExtend version hard-coded in `tests/v3/prowlarrextend/test_prowlarrextend.py`.

`tests/ci/test_metadata_contract.py` checks that the manifest matches the class attributes and requires `author == "oexi"` and `system_version == ">=3.0.0"`. Keep the `from app.plugins import _PluginBase` fallback import until the minimum host version is raised to v3.0.3 or later (`app.sdk.plugin` has existed since v3.0.3).

## Local host integration

In this workspace, the host's `../MoviePilot/config/app.env` sets `PLUGIN_LOCAL_REPO_PATHS` to this repository and enables `PLUGIN_AUTO_RELOAD`. While the host is running, file changes here are automatically reinstalled from the `local://` source into `../MoviePilot/app/plugins/` and hot-reloaded. On startup the host only installs missing plugins; version updates require clicking "update" on the plugin page. Do not copy files into `app/plugins/` by hand.
