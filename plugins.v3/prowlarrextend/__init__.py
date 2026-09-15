# _*_ coding: utf-8 _*_
import asyncio
import datetime
import copy
import json
import math
import re
import threading
import time
import unicodedata
import xml.dom.minidom
from typing import List, Dict, Any, Tuple, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from apscheduler.triggers.cron import CronTrigger

from app.sdk.media import TorrentInfo
from app.sdk.logging import logger
try:
    from app.sdk.plugin import _PluginBase
except (ImportError, AttributeError):
    # MoviePilot V3 before the SDK namespace migration exposed the base class
    # from the legacy app.plugins package.
    from app.plugins import _PluginBase
from app.sdk.config import settings
from app.schemas import MediaType
from app.schemas.types import MediaSource
from app.sdk.network import RequestUtils, SitesHelper
from app.sdk.utilities import StringUtils

from ._torznab import (
    build_torznab_url,
    classify_torznab_response,
    contains_xml_dtd,
    extract_torznab_item,
    find_ambiguous_torznab_page_urls,
    redact_url,
    safe_count,
    safe_float,
    safe_float_none,
    safe_int,
    select_torznab_identity,
    select_torznab_enclosure,
    should_replace_torznab_duplicate,
)
from ._response import ResponseBodyTooLarge, ResponseReadTimeout, read_limited_response
from . import _host_compat
from ._indexers import (
    apply_indexer_selection,
    build_indexer_profiles,
    build_instance_domain_prefix,
    indexer_id_from_domain,
    is_virtual_site,
    normalize_indexer_id,
    parse_indexer_sites,
    selection_is_explicit,
)
from ._ui import build_form, build_page
from ._site_registry import open_site_registry
from ._api_models import ProwlarrStatusResponse, ProwlarrTestResponse

class ProwlarrExtend(_PluginBase):
    # 插件名称
    plugin_name = "ProwlarrExtend"
    # 插件描述
    plugin_desc = "扩展检索以支持Prowlarr站点资源"
    # 插件图标
    plugin_icon = "Prowlarr.png"
    # 插件版本
    plugin_version = "1.0.8"
    # 插件作者
    plugin_author = "oexi"
    # 作者主页
    author_url = "https://github.com/oexi"
    # 插件配置项ID前缀
    plugin_config_prefix = "prowlarr_extend_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # Search requests are user-facing network calls.  Keep the value
    # configurable but bounded so a malformed setting cannot pin a worker
    # forever (or turn a typo into an immediate retry storm).
    SEARCH_TIMEOUT_DEFAULT = 30
    SEARCH_TIMEOUT_MIN = 5
    SEARCH_TIMEOUT_MAX = 120
    # Prowlarr's official FlareSolverr integration defaults to a 60-second
    # solve budget and gives its own HTTP request another five seconds.  Site
    # management browses with an empty query, so allow that specific path to
    # finish while keeping ordinary title searches on the configured timeout.
    # The request remains bounded and runs in ``asyncio.to_thread``.
    BROWSE_TIMEOUT_MIN = 70
    REST_MAX_JSON_BYTES = 4 * 1024 * 1024
    REST_MAX_ITEMS = 5000
    TORZNAB_MAX_XML_BYTES = 8 * 1024 * 1024
    TORZNAB_MAX_ITEMS = 5000
    # Existing rows are identified by the historical virtual-domain prefix;
    # newly injected profiles also carry explicit plugin/parser markers.
    _domain_prefixes = ("prowlarr_extend.",)

    # 私有属性
    _cron = None
    _enabled = False
    _proxy = False
    _timeout = SEARCH_TIMEOUT_DEFAULT
    _host = ""
    _api_key = ""
    _indexer_sites = ""
    _indexer_sites_explicit = False
    _config_snapshot = {}
    _indexers = []
    _authoritative_indexers = None
    _fetch_ok = False
    _sync_ready = False
    _last_sync_ok = False
    _last_sync_at = 0.0
    _last_error = None
    _last_error_at = 0.0
    _last_search_error = None
    _last_search_error_at = 0.0
    sites_helper = None

    # E1: 索引器列表 TTL 缓存(内存 + 时间戳)
    _indexers_cache = None
    _indexers_cache_ts = 0.0
    _indexers_ttl = 600
    # H1: 保护 _indexers/_indexer_sites/_fetch_ok 共享状态的互斥锁
    _state_lock = threading.RLock()
    # This lock protects the short, side-effecting commit phase only.  HTTP
    # requests are deliberately performed before acquiring it.  RLock keeps
    # config persistence (which is itself a commit) safe when called from a
    # synchronization commit.
    _sync_lock = threading.RLock()
    _sync_stop_event = None
    _sync_generation = 0

    @classmethod
    def _runtime_instance_id(cls) -> str:
        """返回宿主为当前运行类分配的稳定实例 ID。"""
        return str(getattr(cls, "__name__", "") or cls.plugin_name).strip()

    @classmethod
    def _is_virtual_instance(cls) -> bool:
        """判断当前类是否由宿主按虚拟分身合同适配。"""
        return bool(getattr(cls, "is_clone", False))

    @classmethod
    def _site_domain_prefix(cls) -> str:
        """返回当前实例使用的站点域名前缀。"""
        historical_prefix = cls._domain_prefixes[0]
        if cls._is_virtual_instance():
            return build_instance_domain_prefix(
                historical_prefix,
                cls._runtime_instance_id(),
            )
        return historical_prefix

    def _bridge_owner_key_for_runtime(self) -> str:
        """返回 bridge 使用的实例级 owner key。"""
        owner_key = self._runtime_instance_id()
        if getattr(self, "_bridge_owner_key", None) != owner_key:
            self._bridge_owner_key = owner_key
        return owner_key

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        # Stop only the previous runtime before replacing shared configuration.
        # Lifecycle cleanup belongs to public stop_service(); doing it here
        # would delete sites during an enabled -> enabled configuration reload.
        self._bridge_owner_key = self._runtime_instance_id()
        self._stop_runtime()

        try:
            self.sites_helper = SitesHelper()
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】SitesHelper 初始化失败：{type(e).__name__}")
            self.sites_helper = None

        # All replacement of configuration/state is serialized with the
        # short commit phase.  A previous-generation worker may still be finishing a DB/event
        # operation after stop_service() returns; waiting here ensures the
        # new generation cannot be overwritten by that operation.
        with self._sync_lock:
            # Initialize every configuration field before applying a possibly
            # empty config.  This is
            # important for init_plugin(None/{}) reloads.
            with self._state_lock:
                self._host = ""
                self._api_key = ""
                self._enabled = False
                self._proxy = False
                self._cron = "0 0 * * *"
                self._timeout = self.SEARCH_TIMEOUT_DEFAULT
                self._indexer_sites = []
                self._indexer_sites_explicit = False
                self._indexers = []
                self._authoritative_indexers = None
                self._fetch_ok = False
                self._sync_ready = False
                self._last_sync_ok = False
                self._last_sync_at = 0.0
                self._indexers_cache = None
                self._indexers_cache_ts = 0.0
                self._last_error = None
                self._last_error_at = 0.0
                self._last_search_error = None
                self._last_search_error_at = 0.0

                # A fresh generation makes prior scheduler callbacks harmless
                # even if a host cannot cancel a callback already queued.
                self._sync_generation += 1
                self._sync_stop_event = threading.Event()

            # 读取配置.  Keep assignment under the same state lock as
            # generation replacement so a request cannot capture a partial
            # replacement between reset and publication.
            with self._state_lock:
                if isinstance(config, dict) and config:
                    # A7: host 去除首尾空白，协议判断大小写不敏感，None 统一兜底为空串
                    host = config.get("host")
                    self._host = self._normalize_host(host)
                    self._api_key = str(config.get("api_key") or "").strip()
                    self._enabled = bool(config.get("enabled"))
                    self._proxy = bool(config.get("proxy"))
                    self._timeout = self._normalize_timeout(
                        config.get("timeout", config.get("search_timeout"))
                    )
                    raw_sites = config.get("indexer_sites") or ""
                    if isinstance(raw_sites, list):
                        # UI 多选(VSelect multiple)保存为数组
                        self._indexer_sites = [str(x).strip() for x in raw_sites if str(x).strip()]
                    else:
                        # API/旧配置为逗号分隔字符串
                        self._indexer_sites = [x.strip() for x in str(raw_sites).split(",") if x.strip()]
                    # Keep the distinction between an intentionally finite whitelist
                    # and an empty value (which means all indexers).  This flag stays
                    # true if cleanup removes only stale entries, preventing a
                    # transient all-stale list from becoming an implicit all-sites
                    # selection.
                    canonical_sites = self._parse_indexer_sites()
                    self._indexer_sites = canonical_sites
                    self._indexer_sites_explicit = bool(canonical_sites)
                    self._cron = str(config.get("cron") or "").strip() or "0 0 * * *"
                # Immutable network settings are published in the
                # same critical section as the fields above.  A worker keeps its
                # local copy even if a reload replaces the instance attributes
                # while its request is in flight.
                self._config_snapshot = self._capture_config_snapshot_locked()
        if not self._enabled:
            # A direct disabled initialization must also converge persisted
            # sites when the host does not call stop_service() first.
            self.__remove_managed_sites()
            return

        # Install after the new configuration snapshot is published, so a
        # host search arriving immediately after enable sees the current
        # generation.  The bridge lazily feature-detects the host boundary.
        _host_compat.install(
            self,
            predicate=self._is_virtual_site,
            owner_key=self._bridge_owner_key_for_runtime(),
        )

        # Validate the cron expression here so the shared host scheduler only
        # ever receives a known-good recurring trigger.  get_service() also
        # exposes a one-shot date trigger for the initial synchronization.
        cron_expr = self._cron or "0 0 * * *"
        logger.info(f"【{self.plugin_name}】索引更新服务启用")
        try:
            CronTrigger.from_crontab(cron_expr, timezone=settings.TZ)
        except Exception as e:
            # A4: cron 表达式非法时回退默认值并告警，避免整个插件初始化崩溃
            logger.warning(
                f"【{self.plugin_name}】cron 表达式无效，已回退为默认 '0 0 * * *'：{type(e).__name__}")
            with self._state_lock:
                self._cron = "0 0 * * *"
        # Initial synchronization is registered as a one-shot MoviePilot
        # scheduler service in get_service().  Keeping all background work under
        # the host scheduler avoids plugin-owned daemon threads during reload/stop.

    @classmethod
    def _normalize_timeout(cls, value: object) -> int:
        """Return a bounded integer search timeout in seconds."""
        if isinstance(value, bool):
            return cls.SEARCH_TIMEOUT_DEFAULT
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                return cls.SEARCH_TIMEOUT_DEFAULT
            timeout = int(numeric)
        except (TypeError, ValueError, OverflowError):
            return cls.SEARCH_TIMEOUT_DEFAULT
        return max(cls.SEARCH_TIMEOUT_MIN, min(cls.SEARCH_TIMEOUT_MAX, timeout))

    @staticmethod
    def _normalize_host(value: object) -> str:
        """Return an HTTP(S) host/base path safe for API URL composition."""
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        if "://" in text and not text.lower().startswith(("http://", "https://")):
            return ""
        if not text.lower().startswith(("http://", "https://")):
            text = "http://" + text
        try:
            parsed = urlsplit(text)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                return ""
            if parsed.username is not None or parsed.password is not None:
                return ""
            try:
                _ = parsed.port
            except ValueError:
                return ""
            # A host query/fragment must not become part of a later API
            # request's query string or leak into diagnostics.
            if parsed.query or parsed.fragment:
                return ""
            path = parsed.path.rstrip("/")
            return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        except (TypeError, ValueError):
            return ""

    def _capture_config_snapshot_locked(self) -> dict:
        """Capture only immutable network settings used by a sync request.

        The caller must hold ``_state_lock``.  Keeping this deliberately small
        prevents a stale worker from reading a newly reloaded credential or
        timeout halfway through its request sequence.
        """
        return {
            "host": self._normalize_host(getattr(self, "_host", "")),
            "api_key": str(getattr(self, "_api_key", "") or ""),
            "proxy": bool(getattr(self, "_proxy", False)),
            "timeout": self._normalize_timeout(getattr(self, "_timeout", self.SEARCH_TIMEOUT_DEFAULT)),
        }

    def _config_for_sync(self) -> dict:
        """Return a copy of the current immutable sync configuration."""
        with self._state_lock:
            snapshot = getattr(self, "_config_snapshot", None)
            if isinstance(snapshot, dict) and snapshot:
                return dict(snapshot)
            return self._capture_config_snapshot_locked()

    def _capture_request_context(self):
        """Capture the configuration and generation for one UI request."""
        with self._sync_lock:
            with self._state_lock:
                snapshot = getattr(self, "_config_snapshot", None)
                if not isinstance(snapshot, dict) or not snapshot:
                    snapshot = self._capture_config_snapshot_locked()
                generation = getattr(self, "_sync_generation", 0)
                return dict(snapshot), generation

    def _commit_indexers_cache(self, indexers: object, generation: int) -> bool:
        """Commit only a useful, current indexer snapshot to the UI cache."""
        with self._state_lock:
            if not self._sync_is_current(generation):
                return False
            if isinstance(indexers, list) and indexers:
                self._indexers_cache = copy.deepcopy(indexers)
                self._indexers_cache_ts = time.time()
            return True

    @classmethod
    def _domain_prefix_set(cls) -> tuple:
        """Return normalized virtual-domain prefixes used by persisted rows."""
        if not cls._is_virtual_instance():
            return tuple(prefix.lower() for prefix in cls._domain_prefixes)
        return (cls._site_domain_prefix().lower(),)

    @classmethod
    def _indexer_id_from_domain(cls, domain: object) -> str:
        """Extract an indexer id from a persisted virtual domain."""
        return indexer_id_from_domain(domain, cls._domain_prefix_set())

    @classmethod
    def _is_virtual_site(cls, site: dict, domain: str = "") -> bool:
        """Recognize current virtual domains and persisted records."""
        return is_virtual_site(
            site,
            domain=domain,
            plugin_name=cls._runtime_instance_id(),
            domain_prefixes=cls._domain_prefix_set(),
        )

    def _sync_is_current(self, generation: Optional[int] = None) -> bool:
        event = self._sync_stop_event
        with self._state_lock:
            current = self._sync_generation
        if event is None:
            # Unit callers may exercise request/cache behavior without
            # starting the plugin service; a generation still identifies the
            # request when no runtime event has been installed.
            return generation is None or generation == current
        return (
            event is not None
            and not event.is_set()
            and (generation is None or generation == current)
        )

    @staticmethod
    def _safe_error_category(category: str) -> str:
        """Normalize a diagnostic category without retaining sensitive text."""
        safe = re.sub(r"[^a-z0-9_.-]", "_", str(category or "error").lower())[:64]
        return safe or "error"

    def _record_error(self, category: str, generation: Optional[int] = None,
                      source: str = "sync"):
        """Remember only a bounded, non-sensitive diagnostic category."""
        safe = self._safe_error_category(category)
        with self._state_lock:
            if generation is not None and not self._sync_is_current(generation):
                return
            now = time.time()
            if source == "search":
                self._last_search_error = safe
                self._last_search_error_at = now
            else:
                self._last_error = safe
                self._last_error_at = now

    def _clear_error(self, generation: Optional[int] = None, source: str = "sync"):
        with self._state_lock:
            if generation is not None and not self._sync_is_current(generation):
                return
            if source == "search":
                self._last_search_error = None
                self._last_search_error_at = 0.0
            else:
                self._last_error = None
                self._last_error_at = 0.0

    def __sync_remove_stale_sites(self, indexers_snapshot: Optional[list] = None,
                                  generation: Optional[int] = None):
        """
        清理插件已注册但不再需要的站点记录（白名单/Prowlarr 变更）
        """
        try:
            if not self._sync_is_current(generation):
                return False
            # A1: empty/failed/cached snapshots never authorize destructive
            # cleanup.  ``_sync_ready`` is set only by a fresh non-empty fetch.
            with self._state_lock:
                sync_ready = self._sync_ready
                if indexers_snapshot is None:
                    indexers_snapshot = list(self._indexers) if isinstance(self._indexers, list) else []
            # An empty selected set is not proof that every previously
            # registered virtual site is stale (it may be a finite whitelist
            # with no currently available IDs).  Never delete the whole set
            # on that single observation.
            if not sync_ready or not isinstance(indexers_snapshot, list) or not indexers_snapshot:
                return True
            current_domains = {
                str(i.get("domain", "")).lower()
                for i in indexers_snapshot
                if isinstance(i, dict) and i.get("domain")
            }
            site_registry = open_site_registry()
            stage_ok = True
            for site in site_registry.list():
                if not self._sync_is_current(generation):
                    return False
                site_domain = str(getattr(site, "domain", "") or "").strip().lower()
                if (self._indexer_id_from_domain(site_domain)
                        and site_domain not in current_domains):
                    site_registry.delete(site.id)
                    logger.info(f"【{self.plugin_name}】已清理过期站点记录: {site_domain}")
                    # A2: 删除后发送 SiteDeleted 事件,触发宿主清理搜索开关/缓存
                    try:
                        site_registry.notify_deleted(site.id)
                    except Exception as e:
                        stage_ok = False
                        logger.warning(f"【{self.plugin_name}】发送 SiteDeleted 事件失败：{type(e).__name__}")
            return stage_ok
        except Exception as e:
            logger.error(f"【{self.plugin_name}】清理过期站点失败: {type(e).__name__}")
            return False

    def get_status(self, generation: Optional[int] = None,
                   config_snapshot: Optional[dict] = None):
        """
        检查连通性
        :return: True、False
        """
        if generation is None:
            captured_snapshot, captured_generation = self._capture_request_context()
            if config_snapshot is None:
                config_snapshot = captured_snapshot
            generation = captured_generation
        if not self._sync_is_current(generation):
            return False
        snapshot = (
            dict(config_snapshot)
            if config_snapshot is not None
            else self._config_for_sync()
        )
        if not snapshot.get("api_key") or not snapshot.get("host"):
            with self._state_lock:
                if not self._sync_is_current(generation):
                    return False
                self._indexers = []
                self._authoritative_indexers = None
                self._fetch_ok = False
                self._sync_ready = False
                self._last_sync_ok = False
            self._record_error("missing_config", generation=generation)
            return False
        try:
            # The sync decision must be based on the complete, fresh Prowlarr
            # list, not on a whitelist-filtered cache.
            indexers = self.get_indexers(
                filter_selected=False,
                force_refresh=True,
                config_snapshot=snapshot,
                generation=generation,
            )
        except Exception as e:
            self._record_error("status_error", generation=generation)
            logger.error(f"【{self.plugin_name}】检查 Prowlarr 连通性失败：{type(e).__name__}")
            indexers = None
        if not self._sync_is_current(generation):
            return False
        # A1: distinguish failed(None), successful-empty([]), and fresh
        # non-empty snapshots.  Only the latter can authorize cleanup.
        selected = self._apply_indexer_selection(indexers)
        with self._state_lock:
            if not self._sync_is_current(generation):
                return False
            # Commit deep copies so callers cannot mutate the authoritative
            # snapshot/cache while a synchronization stage is using it.
            self._authoritative_indexers = copy.deepcopy(indexers)
            self._indexers = copy.deepcopy(selected)
            self._fetch_ok = indexers is not None
            self._sync_ready = bool(indexers)
            self._last_sync_at = time.time()
            # A successful fetch is only the first stage.  __sync_all keeps
            # this false until cleanup, SitesHelper, DB registration, and
            # event publication all complete successfully.
            self._last_sync_ok = False
        if isinstance(indexers, list) and indexers:
            self._clear_error(generation=generation)
        return isinstance(indexers, list) and len(indexers) > 0

    def __sync_all(self, generation: Optional[int] = None):
        """
        完整同步：拉取索引器列表 → 清理失效勾选 → 注册/注入 → 清理过期站点。
        定时任务与初始化共用，确保 Prowlarr 变更(新增/移除/白名单)自动同步到 MP。
        """
        if generation is None:
            _, generation = self._capture_request_context()
        if not self._sync_is_current(generation):
            return
        # Capture immutable network settings before any network call.  The commit
        # lock is intentionally not held during get_status()/HTTP requests.
        with self._state_lock:
            config_snapshot = dict(getattr(self, "_config_snapshot", {}) or {})
            if not config_snapshot:
                config_snapshot = self._capture_config_snapshot_locked()
        # The V3 synchronization contract is snapshot-aware.  Do not retry
        # with the removed one-argument hook: that historical fallback could
        # hide an ABI mismatch and let a worker read mutable live config.
        status_ok = self.get_status(generation=generation, config_snapshot=config_snapshot)
        if not status_ok:
            with self._state_lock:
                if generation is None or self._sync_is_current(generation):
                    self._last_sync_ok = False
            return

        # Only the short state/DB/event phase is serialized.  A reload waits
        # for this lock before replacing configuration, while stale workers
        # are rejected by the generation check at every side-effect boundary.
        self._sync_lock.acquire()
        try:
            if not self._sync_is_current(generation):
                return
            with self._state_lock:
                fetch_ok = self._fetch_ok
                sync_ready = self._sync_ready
                indexers_snapshot = copy.deepcopy(self._indexers) if isinstance(self._indexers, list) else None
                authoritative = (copy.deepcopy(self._authoritative_indexers)
                                 if isinstance(self._authoritative_indexers, list) else None)
            if not fetch_ok or not sync_ready or not isinstance(authoritative, list) or not authoritative:
                # A1: failed/empty results never clear selections or sites.
                logger.debug(f"【{self.plugin_name}】索引器快照不可用于清理，跳过同步清理")
                return
            stage_ok = True
            if self.__cleanup_stale_selection(authoritative, generation=generation) is False:
                stage_ok = False
            # Cleanup can turn an all-stale whitelist into the documented
            # empty-selection meaning (all indexers) only when the user did
            # not configure a finite whitelist.  A finite all-stale list must
            # remain empty and cannot authorize registering every site.
            indexers_snapshot = self._apply_indexer_selection(authoritative)
            with self._state_lock:
                if not self._sync_is_current(generation):
                    return
                self._indexers = copy.deepcopy(indexers_snapshot)
            for indexer in indexers_snapshot or []:
                if not self._sync_is_current(generation):
                    return
                if not isinstance(indexer, dict):
                    stage_ok = False
                    continue
                domain = indexer.get("domain", "")
                if not domain:
                    continue
                new_indexer = copy.deepcopy(indexer)
                try:
                    if self.sites_helper is not None and hasattr(self.sites_helper, "add_indexer"):
                        self.sites_helper.add_indexer(domain, new_indexer)
                    else:
                        stage_ok = False
                        logger.debug(f"【{self.plugin_name}】宿主 SitesHelper 无 add_indexer，跳过内存注入: {domain}")
                except Exception as e:
                    stage_ok = False
                    logger.error(f"【{self.plugin_name}】注入站点 {domain} 失败: {type(e).__name__}")
                if not self._sync_is_current(generation):
                    return
                if self.__register_site(indexer, generation=generation) is False:
                    stage_ok = False
            # An empty selected set (for example a finite all-stale
            # whitelist) must not invoke destructive site cleanup at all.
            if indexers_snapshot:
                if self.__sync_remove_stale_sites(indexers_snapshot, generation=generation) is False:
                    stage_ok = False
            with self._state_lock:
                if self._sync_is_current(generation):
                    self._last_sync_ok = stage_ok
        finally:
            self._sync_lock.release()

    def get_state(self) -> bool:
        return self._enabled

    def _service_id(self) -> str:
        """返回不与其它运行实例冲突的内部服务 ID。"""
        if self._is_virtual_instance():
            return f"{self._runtime_instance_id().lower()}_sync"
        return f"{str(self.plugin_config_prefix or '').strip()}sync"

    def get_service(self) -> List[Dict[str, Any]]:
        """Expose the indexer refresh job to MoviePilot's shared scheduler."""
        with self._state_lock:
            if not self._enabled:
                return []
            cron_expr = self._cron or "0 0 * * *"
            generation = self._sync_generation
            try:
                trigger = CronTrigger.from_crontab(cron_expr, timezone=settings.TZ)
            except Exception as e:
                # init_plugin validates this value, but keep the service ABI
                # safe if a host reads services while replacing configuration.
                logger.warning(
                    f"【{self.plugin_name}】cron 表达式无效，已回退为默认 '0 0 * * *'：{type(e).__name__}")
                self._cron = "0 0 * * *"
                trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
            # Keep both the one-shot initial sync and recurring refresh under
            # MoviePilot's scheduler.  This avoids plugin-owned daemon workers
            # and lets the host own cancellation/reload semantics.
            initial_run_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)
            return [
                {
                    "id": f"{self._service_id()}_initial",
                    "name": f"{self.plugin_name} initial indexer sync",
                    "trigger": "date",
                    "func": self.__sync_all,
                    "func_kwargs": {"generation": generation},
                    "kwargs": {
                        "run_date": initial_run_at,
                        "misfire_grace_time": 60,
                    },
                },
                {
                    "id": self._service_id(),
                    "name": f"{self.plugin_name} indexer sync",
                    "trigger": trigger,
                    "func": self.__sync_all,
                    "func_kwargs": {"generation": generation},
                    "kwargs": {
                        "max_instances": 1,
                        "coalesce": True,
                        "misfire_grace_time": 3600,
                    },
                },
            ]

    def _stop_runtime(self):
        """Stop runtime resources without deleting persisted site rows."""
        _host_compat.uninstall(
            self,
            owner_key=self._bridge_owner_key_for_runtime(),
        )
        event = getattr(self, "_sync_stop_event", None)
        if event is not None:
            event.set()
        with self._state_lock:
            self._sync_generation += 1
        # All synchronization callbacks are scheduler-owned.  Invalidating
        # the generation/event is enough to make an already-dispatched callback
        # harmless at its next side-effect boundary.
        return True

    def stop_service(self):
        """Stop runtime resources while preserving persisted site rows."""
        self._stop_runtime()
        # Drain the short DB/event commit phase.  Network callbacks are owned by
        # MoviePilot's scheduler and stale generations cannot commit afterwards.
        with self._sync_lock:
            with self._state_lock:
                self._enabled = False
        return True

    def __remove_managed_sites(self):
        """Remove only persisted sites in ProwlarrExtend's reserved namespace."""
        with self._sync_lock:
            try:
                site_registry = open_site_registry()
                removed = 0
                failed = 0
                for site in site_registry.list():
                    site_domain = str(getattr(site, "domain", "") or "").strip().lower()
                    if not self._indexer_id_from_domain(site_domain):
                        continue
                    try:
                        site_registry.delete(site.id)
                    except Exception as error:  # noqa: BLE001 - isolate rows
                        failed += 1
                        logger.warning(
                            f"【{self.plugin_name}】生命周期删除站点失败：{type(error).__name__}"
                        )
                        continue
                    removed += 1
                    try:
                        site_registry.notify_deleted(site.id)
                    except Exception as error:  # noqa: BLE001 - DB delete succeeded
                        failed += 1
                        logger.warning(
                            f"【{self.plugin_name}】发送 SiteDeleted 事件失败：{type(error).__name__}"
                        )
                logger.info(
                    f"【{self.plugin_name}】生命周期站点清理完成：删除 {removed} 条，失败 {failed} 条"
                )
                return failed == 0
            except Exception as error:  # noqa: BLE001 - lifecycle boundary
                logger.error(
                    f"【{self.plugin_name}】生命周期站点清理失败：{type(error).__name__}"
                )
                return False

    def __update_config(self, generation: Optional[int] = None):
        """
        更新插件配置
        """
        # V3 适配：宿主 update_config() 为整体替换，必须写回全部配置项，
        # 否则 enabled/proxy 丢失导致插件重载后静默失效
        # update_config is a host-side side effect.  Serialize it with DB and
        # event commits and re-check generation while holding the commit lock
        # so a stale worker cannot write a freshly reloaded config.
        with self._sync_lock:
            if not self._sync_is_current(generation):
                return False
            with self._state_lock:
                payload = {
                    "cron": self._cron,
                    "host": self._host,
                    "api_key": self._api_key,
                    "indexer_sites": copy.deepcopy(self._indexer_sites),
                    "enabled": self._enabled,
                    "proxy": self._proxy,
                    "timeout": self._timeout,
                }
            try:
                self.update_config(payload)
                return True
            except Exception as e:
                logger.error(f"【{self.plugin_name}】保存配置失败：{type(e).__name__}")
                return False

    def __register_site(self, indexer: dict, generation: Optional[int] = None):
        """
        V3 适配：将 Prowlarr indexer 注册为站点写入 DB（site 表）。
        搜索链从 DB 读取有效站点，仅 add_indexer 注入内存时站点不可见。
        """
        if not self._sync_is_current(generation):
            return False
        domain = indexer.get("domain", "")
        if not domain:
            return False
        try:
            site_registry = open_site_registry()
            exists = site_registry.get_by_domain(domain)
            name = indexer.get("name", "")
            # 站点地址必须与插件"查看数据"给出的合成域名格式一致；Torznab API
            # 地址会导致宿主站点校验失败。
            url = f"https://{domain}/"
            public = 1 if indexer.get("public") else 0
            if not self._sync_is_current(generation):
                return False
            if exists:
                # B1: 更新分支只同步 name/url/public 来源字段,
                # 保留 is_active/pri/proxy 等用户站点设置不被 cron 覆盖
                site_registry.update(exists.id, {"name": name, "url": url, "public": public})
                logger.info(f"【{self.plugin_name}】已更新站点记录: {domain}")
            else:
                # 新增才写入默认启停/优先级/代理
                payload = {
                    "name": name,
                    "domain": domain,
                    "url": url,
                    "public": public,
                    "proxy": 1 if indexer.get("proxy") else 0,
                    "is_active": True,
                    "pri": 1,
                }
                try:
                    added = site_registry.add(**payload)
                    if added is False:
                        # Current V3 SiteOper.add reports duplicate/race conflicts
                        # as (False, message) instead of necessarily raising.
                        existing = site_registry.get_by_domain(domain)
                        if not existing:
                            raise RuntimeError("site add rejected without persisted row")
                        site_registry.update(existing.id, {"name": name, "url": url, "public": public})
                        logger.debug(f"【{self.plugin_name}】站点已存在(并发注册返回失败),转为更新: {domain}")
                    else:
                        logger.info(f"【{self.plugin_name}】已注册站点到 DB: {domain}")
                except Exception as e:
                    # B2: 并发下重复插入等冲突,重新查询,已存在则转更新分支
                    existing = site_registry.get_by_domain(domain)
                    if not existing:
                        raise
                    site_registry.update(existing.id, {"name": name, "url": url, "public": public})
                    logger.debug(f"【{self.plugin_name}】站点已存在(并发注册),转为更新: {domain}, {type(e).__name__}")
            # 通知宿主刷新站点缓存
            try:
                site_registry.notify_updated(domain)
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】发送 SiteUpdated 事件失败：{type(e).__name__}")
                return False
            return True
        except Exception as e:
            logger.error(f"【{self.plugin_name}】注册站点 {domain} 到 DB 失败: {type(e).__name__}")
            return False

    def _parse_indexer_sites(self) -> list:
        """
        统一解析 indexer_sites 配置为小写 id 列表。
        兼容：list(UI 多选)、逗号分隔字符串(API/旧格式)、None/其他类型。
        """
        with self._state_lock:
            sites = self._indexer_sites
        return parse_indexer_sites(sites)

    def _selection_is_explicit(self) -> bool:
        """Whether an empty parsed selection still represents a finite list."""
        with self._state_lock:
            explicit = getattr(self, "_indexer_sites_explicit", None)
            raw = getattr(self, "_indexer_sites", None)
        return selection_is_explicit(raw, explicit)

    def _apply_indexer_selection(self, indexers: object) -> list:
        """Apply the canonical whitelist without turning stale-all into all."""
        with self._state_lock:
            raw_sites = getattr(self, "_indexer_sites", None)
            explicit = getattr(self, "_indexer_sites_explicit", None)
        return apply_indexer_selection(indexers, raw_sites, explicit)

    def __cleanup_stale_selection(self, authoritative_indexers: Optional[list] = None,
                                  generation: Optional[int] = None):
        """
        移除 indexer_sites 中已被 Prowlarr 删除的索引器勾选，
        避免配置界面已勾选区域残留失效索引器（下拉 items 已无对应项）。
        """
        try:
            if not self._sync_is_current(generation):
                return False
            with self._state_lock:
                sites_snapshot = list(self._indexer_sites) if isinstance(self._indexer_sites, list) else []
                indexers_snapshot = (
                    list(authoritative_indexers)
                    if isinstance(authoritative_indexers, list)
                    else (list(self._authoritative_indexers)
                          if isinstance(self._authoritative_indexers, list) else [])
                )
            if not sites_snapshot:
                return True
            # A1: only a fresh, non-empty authoritative snapshot is allowed
            # to rewrite the user's selection.
            with self._state_lock:
                sync_ready = self._sync_ready
            if not sync_ready or not indexers_snapshot:
                return True
            # C2: 以 indexer_id 为单一事实来源,合成名仅用于显示
            valid = {str(i.get("indexer_id") or "").strip().lower()
                     for i in indexers_snapshot if i.get("indexer_id")}
            stale = [x for x in sites_snapshot if str(x).strip().lower() not in valid]
            if stale:
                if not self._sync_is_current(generation):
                    return False
                retained = [
                    x for x in sites_snapshot
                    if str(x).strip().lower() in valid
                ]
                # Never erase the complete finite whitelist.  Keeping stale
                # IDs is safer than persisting [] and silently registering all
                # indexers on the next sync/reload.
                if not retained:
                    logger.warning(f"【{self.plugin_name}】白名单全部失效，保留原配置并跳过全量注册")
                    return True
                with self._state_lock:
                    self._indexer_sites = retained
                if self.__update_config(generation=generation) is False:
                    return False
                logger.info(f"【{self.plugin_name}】已清理失效勾选: {stale}")
            return True
        except Exception as e:
            logger.error(f"【{self.plugin_name}】清理失效勾选失败: {type(e).__name__}")
            return False

    def search_torrents(self, site: dict, keyword: str = None, mtype: Optional[MediaType] = None,
                        cat: Optional[str] = None, page: Optional[int] = 0, **kwargs) -> \
            List[
                TorrentInfo]:
        return self._search_torrents(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
            propagate_upstream_error=False,
        )

    def _search_torrents(self, site: dict, keyword: str = None,
                         mtype: Optional[MediaType] = None,
                         cat: Optional[str] = None, page: Optional[int] = 0,
                         propagate_upstream_error: bool = False) -> List[TorrentInfo]:
        """
        使用 Prowlarr Torznab API 根据关键字检索种子
        :param site:  站点
        :param keyword:  搜索关键词（为空时获取最新资源，refresh_torrents 语义）
        :param mtype:  媒体类型
        :param page:  页码（插件不支持翻页，page>0 直接返回空避免重复结果）
        :return: 资源列表
        """
        results = []
        if not site:
            return results

        # D1: 以 domain 前缀识别本插件站点，不依赖 name 前缀
        # （新架构宿主会把 DB 站点行 name 与注入 profile 合并，name 取自 DB 时会静默误判）
        domain = str(site.get("domain") or "").strip()
        if domain.lower().startswith(("http://", "https://")):
            try:
                domain = StringUtils.get_url_domain(domain) or domain
            except Exception:
                pass
            if domain.lower().startswith(("http://", "https://")):
                try:
                    domain = urlsplit(domain).hostname or domain
                except ValueError:
                    pass
        if not self._is_virtual_site(site, domain):
            # 非本插件站点属正常分发，DEBUG 即可，避免百站搜索刷屏
            logger.debug(f"【{self.plugin_name}】站点非本插件注册，交由其他模块处理：name={site.get('name')}, domain={domain!r}")
            return results
        # Prefer the exact ID carried by the profile.  A persisted hostname
        # can be lower-cased or percent-decoded by the host, so use domain
        # decoding only as a fallback for legacy rows.
        indexer_id = str(site.get("indexer_id") or "").strip()
        if not indexer_id:
            indexer_id = self._indexer_id_from_domain(domain)
        indexer_id = normalize_indexer_id(indexer_id)
        if not indexer_id:
            # D1: 前缀命中但无法解析 id 才是真正的识别失败，用 WARNING 便于诊断
            logger.warning(f"【{self.plugin_name}】站点识别失败，无法从 domain 解析 indexer id：name={site.get('name')}, domain={domain!r}")
            return results
        # D5: 不支持翻页；page>0 直接返回空，避免宿主重复请求同页造成结果重复合并
        try:
            page_num = int(page or 0)
        except (TypeError, ValueError):
            page_num = 0
        if page_num > 0:
            logger.debug(f"【{self.plugin_name}】不支持翻页，跳过 page={page_num} 请求以避免重复结果：domain={domain}")
            return results

        # D4: keyword 为空时使用 Prowlarr 空查询获取最新资源(refresh_torrents/RSS 刷新)
        keyword = keyword or ""
        # Canonicalize width/compatibility characters before MoviePilot's
        # punctuation cleaning to avoid upstream redirects (e.g. Ｓ -> S).
        keyword = unicodedata.normalize("NFKC", keyword)
        keyword = StringUtils.clear(text=keyword, replace_word=" ", allow_space=True)
        propagate_upstream_error = bool(propagate_upstream_error and not keyword)
        masked_keyword = self.__mask_keyword(keyword)
        api_url = ""
        config_snapshot = self._config_for_sync()
        # The snapshot is authoritative even when a reload has already
        # replaced instance attributes.  Falling back with ``or`` here would
        # leak the new generation's host/key into an in-flight search when the
        # old snapshot intentionally contains an empty value.
        host = self._normalize_host(config_snapshot.get("host"))
        api_key = str(config_snapshot.get("api_key", "") or "")
        try:
            if not host or not api_key:
                self._record_error("missing_config", source="search")
                return results
            # D6: 搜索热路径日志降为 DEBUG,关键词仅 DEBUG 输出且脱敏
            logger.debug(f"【{self.plugin_name}】开始检索 Indexer：\"{site.get('name')}\"，关键词：\"{masked_keyword}\"")

            # Prowlarr authenticates through X-Api-Key.  Keep the key out of
            # query strings and therefore out of URL/log/referrer surfaces.
            params = {
                "t": "search",
                "q": keyword,
            }
            # BUGFIX: 透传用户在站点浏览弹窗中显式选择的分类 ID。
            # 宿主 /site/{id}/category 返回的条目 id 会以逗号分隔字符串回传
            # (如 "2000,3000")，Prowlarr torznab cat 参数同样接受该格式，
            # 让 UI 分类筛选真实生效；未显式选择时不传 cat，
            # 保持按标题/媒体类型兜底过滤的既有语义。
            cat_value = str(cat or "").strip()
            # Prowlarr's canonical music category is 3000.  Keep this narrow
            # fallback for music while leaving movie/TV category discovery to
            # the caller-selected caps; do not inject Prowlarr-only 2000/5000
            # defaults into a music request.
            if not cat_value and mtype is not None:
                mtype_value = str(getattr(mtype, "value", mtype)).lower()
                mtype_name = str(getattr(mtype, "name", "")).lower()
                if mtype_value == "music" or mtype_value == "音乐" or mtype_name == "music":
                    cat_value = "3000"
            if cat_value:
                cat_value = re.sub(r"\s+", "", cat_value)
                if re.fullmatch(r"\d+(,\d+)*", cat_value):
                    params["cat"] = cat_value
                else:
                    logger.debug(f"【{self.plugin_name}】忽略非分类 ID 格式的 cat 参数")
            api_url = build_torznab_url(
                host,
                indexer_id,
                keyword=keyword,
                cat=params.get("cat"),
            )
            if not api_url:
                self._record_error("invalid_search_url", source="search")
                return results

            result_array = self.__parse_torznab_xml(
                api_url, site=site, mtype=mtype, keyword=keyword,
                config_snapshot=config_snapshot,
                propagate_upstream_error=propagate_upstream_error,
            )

            if not result_array:
                # D6: 无结果是常态,降为 DEBUG
                logger.debug(f"【{self.plugin_name}】Indexer：\"{site.get('name')}\" 未检索到数据，关键词：\"{masked_keyword}\"")
                return results

            logger.debug(f"【{self.plugin_name}】Indexer：\"{site.get('name')}\" 返回数据：{len(result_array)} 条")
            results.extend(result_array)

        except _host_compat.SanitizedUpstreamError:
            # Only the dedicated empty-keyword refresh route opts into this
            # process-stable, non-sensitive signal.  Ordinary searches never
            # request propagation and remain fail-closed.
            raise
        except Exception as e:
            # D8: 异常日志附带 URL/站点/关键词(脱敏)/异常类型
            self._record_error("search_error", source="search")
            logger.error(
                f"【{self.plugin_name}】检索出错：site={site.get('name')}, indexer={indexer_id}, "
                f"url={redact_url(api_url) if api_url else '-'}, 关键词={masked_keyword or '-'}, "
                f"类型={type(e).__name__}")

        return results

    def refresh_torrents(self, site: dict, keyword: str = None,
                         cat: Optional[str] = None,
                         page: Optional[int] = 0,
                         mtype: Optional[MediaType] = None, **kwargs) -> List[TorrentInfo]:
        """Refresh one owned site, distinguishing upstream failure from empty."""
        return self._search_torrents(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
            propagate_upstream_error=True,
        )

    async def async_search_torrents(self, site: dict, keyword: str = None,
                                     mtype: Optional[MediaType] = None,
                                     cat: Optional[str] = None,
                                     page: Optional[int] = 0, **kwargs) -> List[TorrentInfo]:
        """Run the synchronous HTTP parser off the event loop."""
        return await asyncio.to_thread(
            self.search_torrents,
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
            **kwargs,
        )

    async def async_refresh_torrents(self, site: dict, keyword: str = None,
                                      cat: Optional[str] = None,
                                      page: Optional[int] = 0,
                                      mtype: Optional[MediaType] = None, **kwargs) -> List[TorrentInfo]:
        """Run the dedicated refresh boundary off the event loop."""
        return await asyncio.to_thread(
            self.refresh_torrents,
            site=site,
            keyword=keyword,
            cat=cat,
            page=page,
            mtype=mtype,
            **kwargs,
        )

    def get_search_page_size(self, site: dict, keyword: str = None) -> Optional[int]:
        """Prowlarr virtual profiles do not expose a reliable page size."""
        return None

    def get_indexers(self, filter_selected: bool = True, force_refresh: bool = False,
                     config_snapshot: Optional[dict] = None,
                     generation: Optional[int] = None):
        """
        获取配置的 Prowlarr Indexer 信息
        :param filter_selected: True 按白名单过滤(留空=全部),False 返回完整列表
        :param force_refresh: True 强制实时拉取(初始化/cron),False 优先使用 TTL 缓存
        :return: Indexer 列表；拉取成功但为空返回 []，拉取失败返回 None
        """
        current_snapshot, current_generation = self._capture_request_context()
        request_snapshot = (
            dict(config_snapshot)
            if config_snapshot is not None
            else current_snapshot
        )
        request_generation = (
            generation
            if generation is not None
            else current_generation
        )
        if not self._sync_is_current(request_generation):
            return None

        now = time.time()
        with self._state_lock:
            cached = copy.deepcopy(self._indexers_cache)
            cached_ts = self._indexers_cache_ts

        if force_refresh:
            # 初始化/cron 同步强制刷新;失败返回 None,不复用旧缓存,
            # 避免把旧数据误判为"本次拉取成功"而触发破坏性清理(A1)
            raw = self.__fetch_indexers(
                config_snapshot=request_snapshot,
                generation=request_generation,
            )
            if raw is None or not self._sync_is_current(request_generation):
                return None
            if not self._commit_indexers_cache(raw, request_generation):
                return None
        elif isinstance(cached, list) and cached_ts and (now - cached_ts) < self._indexers_ttl:
            # E1: TTL 内直接使用缓存
            raw = cached
        else:
            # E1/G4: 表单/详情页缓存过期或缺失时尝试刷新;失败用旧缓存兜底,
            # 并顺延时间戳,避免 Prowlarr 故障时每次打开页面都阻塞在超时请求上
            raw = self.__fetch_indexers(
                config_snapshot=request_snapshot,
                generation=request_generation,
            )
            if raw is not None:
                if not self._commit_indexers_cache(raw, request_generation):
                    return None
            elif isinstance(cached, list):
                with self._state_lock:
                    if not self._sync_is_current(request_generation):
                        return None
                    self._indexers_cache_ts = time.time()
                    raw = copy.deepcopy(self._indexers_cache)
            else:
                return None

        if not self._sync_is_current(request_generation):
            return None
        if not filter_selected:
            result = copy.deepcopy(raw)
        else:
            result = self._apply_indexer_selection(raw)
        return result if self._sync_is_current(request_generation) else None

    def __fetch_indexers(self, config_snapshot: Optional[dict] = None,
                         generation: Optional[int] = None,
                         error_sink=None):
        """
        实时从 Prowlarr 拉取并构造 indexer 列表（完整列表,不过滤白名单）。
        :return: 成功返回 list(可能为空);失败返回 None
        """
        def record_fetch_error(category: str):
            """Record sync failures or send them to a caller-local probe sink."""
            if callable(error_sink):
                error_sink(self._safe_error_category(category))
            else:
                self._record_error(category, generation=generation)

        snapshot = (
            dict(config_snapshot)
            if config_snapshot is not None
            else self._config_for_sync()
        )
        host = self._normalize_host(snapshot.get("host"))
        api_key = str(snapshot.get("api_key") or "")
        proxy = bool(snapshot.get("proxy"))
        timeout = self._normalize_timeout(snapshot.get("timeout"))
        if not host or not api_key:
            record_fetch_error("missing_config")
            return None
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot"),
            "X-Api-Key": api_key,
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }

        try:
            indexer_query_url = f"{host}/api/v1/indexer"
            with RequestUtils(headers=headers, timeout=timeout).get_stream(
                indexer_query_url,
                proxies=getattr(settings, "PROXY", None) if proxy else None,
                raise_exception=True,
            ) as ret:
                # E3: 校验状态码/Content-Type/数据类型,json 只解析一次
                if ret is None:
                    record_fetch_error("empty")
                    logger.warning(f"【{self.plugin_name}】拉取 indexers 请求失败：{redact_url(indexer_query_url)}")
                    return None
                status_code = getattr(ret, "status_code", 0)
                if status_code != 200:
                    record_fetch_error(f"http_{status_code}")
                    logger.warning(f"【{self.plugin_name}】拉取 indexers 失败,HTTP {status_code}：{redact_url(indexer_query_url)}")
                    return None
                response_headers = getattr(ret, "headers", {}) or {}
                content_type = (response_headers.get("Content-Type") or "").lower()
                if "json" not in content_type:
                    record_fetch_error("content_type")
                    logger.warning(f"【{self.plugin_name}】拉取 indexers 响应非 JSON(Content-Type={content_type!r})")
                    return None
                body = read_limited_response(ret, self.REST_MAX_JSON_BYTES)
                try:
                    raw_indexers = json.loads(body)
                except ValueError as e:
                    record_fetch_error("json_error")
                    logger.warning(f"【{self.plugin_name}】拉取 indexers JSON 解析失败：{type(e).__name__}")
                    return None
                if not isinstance(raw_indexers, list):
                    record_fetch_error("json_type")
                    logger.warning(
                        f"【{self.plugin_name}】拉取 indexers 响应类型异常"
                        f"(期望 list,实际 {type(raw_indexers).__name__})")
                    return None
                if len(raw_indexers) > self.REST_MAX_ITEMS:
                    record_fetch_error("json_too_many_items")
                    logger.warning(f"【{self.plugin_name}】Prowlarr indexer 数量超出限制")
                    return None
        except ResponseReadTimeout:
            record_fetch_error("timeout")
            logger.warning(f"【{self.plugin_name}】获取 Prowlarr indexers 超时")
            return None
        except ResponseBodyTooLarge:
            record_fetch_error("json_too_large")
            logger.warning(f"【{self.plugin_name}】Prowlarr indexer JSON 超出大小限制")
            return None
        except (requests.Timeout, TimeoutError):
            record_fetch_error("timeout")
            logger.warning(f"【{self.plugin_name}】获取 Prowlarr indexers 超时")
            return None
        except Exception as e:
            record_fetch_error("request_error")
            logger.error(f"【{self.plugin_name}】获取 Prowlarr indexers 失败：{type(e).__name__}")
            return None

        # Prowlarr normally returns only objects, but malformed entries should
        # be ignored before any diagnostic access (and never abort the list).
        raw_indexers = [v for v in raw_indexers if isinstance(v, dict)]
        logger.debug(f"【{self.plugin_name}】Prowlarr indexers: {[v.get('id') for v in raw_indexers]}")
        # Pure profile construction is isolated; network/session/config and
        # generation checks above remain part of this lifecycle-heavy method.
        indexers = build_indexer_profiles(
            raw_indexers,
            host=host,
            proxy=proxy,
            plugin_name=self.plugin_name,
            domain_prefix=self._site_domain_prefix(),
            owner_id=self._runtime_instance_id(),
        )

        logger.info(f"【{self.plugin_name}】获取到 {len(indexers)} 个 Prowlarr indexers")
        return indexers

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        {
            "id1": self.xxx1,
            "id2": self.xxx2,
        }
        """
        def module_search_torrents(site, keyword=None, mtype=None, page=0):
            return self.search_torrents(
                site=site, keyword=keyword, mtype=mtype, page=page
            )

        async def module_async_search_torrents(site, keyword=None, mtype=None, page=0):
            return await self.async_search_torrents(
                site=site, keyword=keyword, mtype=mtype, page=page
            )

        def module_refresh_torrents(site, keyword=None, cat=None, page=0, mtype=None):
            return self.refresh_torrents(
                site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
            )

        async def module_async_refresh_torrents(
                site, keyword=None, cat=None, page=0, mtype=None):
            return await self.async_refresh_torrents(
                site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
            )

        return {
            "search_torrents": module_search_torrents,
            "async_search_torrents": module_async_search_torrents,
            "refresh_torrents": module_refresh_torrents,
            "async_refresh_torrents": module_async_refresh_torrents,
            "get_search_page_size": self.get_search_page_size,
        }

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """

        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "auth": "apikey",
                "response_model": ProwlarrTestResponse,
                "summary": "ProwlarrExtend 只读连接测试",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "response_model": ProwlarrStatusResponse,
                "summary": "ProwlarrExtend 同步状态（脱敏）",
            },
        ]

    def _diagnostic_payload(self, probe: bool = False) -> Dict[str, Any]:
        """Build a read-only status payload containing no credentials."""
        connected = False
        probe_error = None
        probe_error_at = None
        if probe:
            probe_state = {"error": None, "error_at": None}

            def record_probe_error(category):
                probe_state["error"] = self._safe_error_category(category)
                probe_state["error_at"] = time.time()

            try:
                # A test request performs only a fresh Prowlarr read.  It does
                # not replace the authoritative sync snapshot or mutate DB
                # sites while a background synchronization may be running.
                probe_indexers = self.__fetch_indexers(error_sink=record_probe_error)
                connected = isinstance(probe_indexers, list) and bool(probe_indexers)
            except Exception:
                record_probe_error("status_error")
            probe_error = probe_state["error"]
            probe_error_at = probe_state["error_at"]
            if connected:
                # A successful probe has no local error to report.  It must
                # not clear the last synchronization or search diagnostic.
                probe_error = None
                probe_error_at = None
        with self._state_lock:
            authoritative = self._authoritative_indexers
            selected = self._indexers
            fetch_ok = bool(self._fetch_ok)
            sync_ready = bool(self._sync_ready)
            last_sync_ok = bool(self._last_sync_ok)
            last_sync_at = self._last_sync_at
            last_error = self._last_error
            last_error_at = self._last_error_at
            last_search_error = getattr(self, "_last_search_error", None)
            last_search_error_at = getattr(self, "_last_search_error_at", 0.0)
        if not probe:
            connected = fetch_ok and isinstance(authoritative, list) and bool(authoritative)
        # Host/API key are intentionally not represented.
        # ``configured`` is sufficient for remote diagnosis without risking
        # secrets embedded in a legacy host path.
        return {
            "enabled": bool(self._enabled),
            "configured": bool(self._host and self._api_key),
            "connected": connected,
            "sync": {
                "fetch_ok": fetch_ok,
                "ready": sync_ready,
                "last_ok": last_sync_ok,
                "last_at": last_sync_at or None,
            },
            "indexer_count": len(authoritative) if isinstance(authoritative, list) else 0,
            "selected_count": len(selected) if isinstance(selected, list) else 0,
            "last_error": last_error,
            "last_error_at": last_error_at or None,
            "last_search_error": last_search_error,
            "last_search_error_at": last_search_error_at or None,
            "probe_error": probe_error,
            "probe_error_at": probe_error_at or None,
        }

    def api_test(self) -> ProwlarrTestResponse:
        """Read-only connectivity probe with redacted, aggregate output."""
        payload = self._diagnostic_payload(probe=True)
        payload["ok"] = bool(payload["connected"])
        return ProwlarrTestResponse.model_validate(payload)

    def api_status(self) -> ProwlarrStatusResponse:
        """Read-only cached state endpoint; it never starts synchronization."""
        return ProwlarrStatusResponse.model_validate(self._diagnostic_payload(probe=False))

    @staticmethod
    def __mask_keyword(keyword):
        """D6/D8: 日志中的关键词脱敏,仅在 DEBUG/异常上下文使用"""
        if not keyword:
            return ""
        # Do not retain a prefix: titles/names can be identifying even when
        # only a few leading characters are exposed.  Length is useful for
        # diagnostics without leaking the original search text.
        return f"{'*' * min(len(str(keyword)), 12)}({len(str(keyword))})"

    def __parse_torznab_xml(self, url, site: dict = None, mtype: Optional[MediaType] = None,
                            keyword: str = None, config_snapshot: Optional[dict] = None,
                            propagate_upstream_error: bool = False) -> List[TorrentInfo]:
        """
        从 torznab XML 中解析种子信息
        :param url: XML 数据的 URL
        :return: TorrentInfo 列表
        """
        if not url:
            return []

        def upstream_failure(category: str) -> list:
            if propagate_upstream_error and not keyword:
                raise _host_compat.SanitizedUpstreamError(category)
            return []

        log_url = redact_url(url)
        request_config = dict(config_snapshot or self._config_for_sync())
        request_timeout = self._normalize_timeout(
            request_config.get("timeout", getattr(self, "_timeout", self.SEARCH_TIMEOUT_DEFAULT))
        )
        if not keyword:
            request_timeout = max(request_timeout, self.BROWSE_TIMEOUT_MIN)
        request_proxy = bool(request_config.get("proxy", getattr(self, "_proxy", False)))
        try:
            headers = {
                "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot"),
                "X-Api-Key": str(request_config.get("api_key") or ""),
                "Accept": "application/xml, application/rss+xml, text/xml, */*",
            }
            with RequestUtils(headers=headers, timeout=request_timeout).get_stream(
                url,
                proxies=getattr(settings, "PROXY", None) if request_proxy else None,
                raise_exception=True,
            ) as ret:
                if ret is None:
                    self._record_error("empty", source="search")
                    logger.debug(f"【{self.plugin_name}】torznab 空响应：url={log_url}")
                    return upstream_failure("empty_response")

                status_code = getattr(ret, "status_code", 0)
                response_headers = getattr(ret, "headers", {}) or {}
                content_type = (response_headers.get("Content-Type") or "").lower()
                if status_code != 200:
                    error_category = f"http_{status_code}"
                    self._record_error(error_category, source="search")
                    logger.warning(
                        f"【{self.plugin_name}】Prowlarr torznab 响应不可用："
                        f"url={log_url}, category=http_error, HTTP={status_code}, "
                        f"content_type={content_type or '-'}"
                    )
                    return upstream_failure(error_category)
                body = read_limited_response(ret, self.TORZNAB_MAX_XML_BYTES)
        except _host_compat.SanitizedUpstreamError:
            raise
        except ResponseReadTimeout:
            self._record_error("timeout", source="search")
            logger.warning(f"【{self.plugin_name}】torznab 响应超时：url={log_url}")
            return upstream_failure("timeout")
        except (requests.Timeout, TimeoutError):
            self._record_error("timeout", source="search")
            logger.warning(f"【{self.plugin_name}】torznab 响应超时：url={log_url}")
            return upstream_failure("timeout")
        except ResponseBodyTooLarge:
            self._record_error("xml_too_large", source="search")
            logger.warning(f"【{self.plugin_name}】torznab XML 超出大小限制：url={log_url}")
            return upstream_failure("invalid_response")
        except Exception as e:
            # Request exception text may echo the original URL; only record
            # the exception type.
            self._record_error("request_error", source="search")
            logger.error(f"【{self.plugin_name}】torznab 请求异常：url={log_url}, 类型={type(e).__name__}")
            return upstream_failure("request_error")
        # F1: 校验状态码与 Content-Type;JSON 错误体不进 XML 解析
        response_category = classify_torznab_response(
            200, content_type, body
        )
        if response_category != "ok":
            if response_category == "http_error":
                error_category = f"http_{status_code}"
                self._record_error(error_category, source="search")
            else:
                error_category = (
                    "empty_response" if response_category == "empty"
                    else "invalid_response"
                )
                self._record_error(response_category, source="search")
            logger.warning(
                f"【{self.plugin_name}】Prowlarr torznab 响应不可用："
                f"url={log_url}, category={response_category}, HTTP={status_code}, "
                f"content_type={content_type or '-'}"
            )
            return upstream_failure(error_category)

        body_empty = not body.strip()
        if body_empty:
            self._record_error("empty", source="search")
            logger.debug(f"【{self.plugin_name}】torznab 空响应：url={log_url}")
            return upstream_failure("empty_response")
        # Keep minidom bounded and reject DTD/entity declarations before the
        # parser sees them.  Normal Torznab RSS is small and has no DOCTYPE;
        # oversized/adversarial payloads are rejected by the streaming reader.
        try:
            contains_dtd = contains_xml_dtd(body)
        except (LookupError, UnicodeError) as e:
            self._record_error("xml_error", source="search")
            logger.warning(f"【{self.plugin_name}】torznab XML 编码扫描失败：url={log_url}, 类型={type(e).__name__}")
            return upstream_failure("torznab_error")
        if contains_dtd:
            self._record_error("xml_doctype", source="search")
            logger.warning(f"【{self.plugin_name}】torznab XML 含不支持的 DOCTYPE：url={log_url}")
            return upstream_failure("invalid_response")

        torrents = []
        # One primary identity per item.  Secondary fields must not be added
        # to the dedupe set: two releases can legitimately share a GUID while
        # carrying different infohashes.
        seen_identities = {}
        try:
            # F3: 保持 stdlib minidom,不加新依赖。torznab:attr 命名空间取值依赖
            # getAttribute,DOM 树整体解析实现简单稳定;ElementTree.iterparse 流式解析
            # 需自行处理命名空间且收益有限,故保留 minidom 并在此标注。
            dom_tree = xml.dom.minidom.parseString(body)
            root_node = dom_tree.documentElement
            root_name = getattr(root_node, "localName", None) or root_node.tagName.rsplit(":", 1)[-1]
            if root_name.lower() == "error":
                self._record_error("torznab_error", source="search")
                logger.warning(f"【{self.plugin_name}】torznab 返回错误 XML")
                return upstream_failure("torznab_error")
            items = root_node.getElementsByTagName("item")
            if len(items) > self.TORZNAB_MAX_ITEMS:
                self._record_error("xml_too_many_items", source="search")
                logger.warning(f"【{self.plugin_name}】torznab XML item 数量超出限制：url={log_url}")
                return upstream_failure("invalid_response")
        except Exception as e:
            # F1: XML 解析失败降为 WARNING,不输出完整 traceback 刷屏
            self._record_error("xml_error", source="search")
            logger.warning(f"【{self.plugin_name}】torznab XML 解析失败：url={log_url}, 类型={type(e).__name__}")
            return upstream_failure("torznab_error")

        for item in items:
            try:
                # Pure DOM/torznab extraction is isolated from host adapters;
                # StringUtils/TorrentInfo handling remains in this entrypoint.
                item_fields = extract_torznab_item(item)
                title = item_fields["title"]
                if not title:
                    continue
                enclosure = item_fields["enclosure"]
                link = item_fields["link"]
                guid = item_fields["guid"]
                description = item_fields["description"]
                size = item_fields["size"]
                page_url = item_fields["page_url"]
                pubdate = item_fields["pubdate"]
                if pubdate:
                    pubdate = StringUtils.unify_datetime_str(pubdate)
                seeders = item_fields["seeders"]
                peers = item_fields["peers"]
                imdbid = item_fields["imdbid"]
                infohash = item_fields["infohash"]
                grabs = item_fields["grabs"]
                labels = item_fields["labels"]
                uploadvolumefactor = item_fields["uploadvolumefactor"]
                downloadvolumefactor = item_fields["downloadvolumefactor"]
                hit_and_run = item_fields["hit_and_run"]
                magnet_url = item_fields["magnet_url"]

                enclosure = select_torznab_enclosure(
                    enclosure=enclosure,
                    link=link,
                    magnet_url=magnet_url,
                    guid=guid,
                )
                if not enclosure:
                    continue

                # One virtual site owns one dedupe scope.  Select exactly one
                # primary identity in the documented order.  For duplicate
                # infohashes, an HTTP torrent is more useful than a magnet and
                # replaces an earlier magnet regardless of item order.
                identity = select_torznab_identity(
                    infohash=infohash,
                    guid=guid,
                    page_url=page_url,
                    enclosure=enclosure,
                )

                # D3: imdbid 映射为 media_source/media_id 媒体身份
                media_source = None
                media_id = None
                if imdbid:
                    media_id = imdbid
                    media_source = MediaSource.IMDb

                tmp_dict = TorrentInfo(
                    title=title,
                    enclosure=enclosure,
                    description=description,
                    # D2: size/计数安全转换，非法或负值回退 0
                    size=safe_float(size),
                    seeders=safe_count(seeders),
                    peers=safe_count(peers),
                    grabs=safe_count(grabs),
                    # V3 适配：显示真实站点名。
                    site=site.get("id") if site else None,
                    site_name=site.get("name", self.plugin_name) if site else self.plugin_name,
                    site_cookie=site.get("cookie") if site else None,
                    site_ua=site.get("ua") if site else None,
                    site_proxy=bool(site.get("proxy")) if site else False,
                    site_order=safe_int(site.get("pri", site.get("order", 0))) if site else 0,
                    site_downloader=site.get("downloader") if site else None,
                    page_url=page_url,
                    # D3: pubdate/促销因子/HR 传入 TorrentInfo,支持发布时长与促销过滤
                    pubdate=pubdate or None,
                    uploadvolumefactor=safe_float_none(uploadvolumefactor),
                    downloadvolumefactor=safe_float_none(downloadvolumefactor),
                    hit_and_run=bool(hit_and_run),
                    media_source=media_source,
                    media_id=media_id,
                    labels=labels,
                    # V3 适配：填种子分类。MP 音乐匹配（_matching_music_torrents）要求
                    # torrent.category == MUSIC，原版不填导致音乐搜索全部被过滤。
                    # Prowlarr 的 torznab category 值（部分索引器会使用源站分类 ID）与标准
                    # 分类不一致，无法可靠映射，直接用宿主搜索 mtype 兜底（音乐搜索时
                    # mtype 必为 MUSIC，再由上层标题+艺术家匹配筛除无关资源）。
                    category=getattr(mtype, "value", mtype) if mtype else None,
                )
                previous = seen_identities.get(identity)
                if previous is not None:
                    previous_index, previous_enclosure = previous
                    if should_replace_torznab_duplicate(previous_enclosure, enclosure):
                        torrents[previous_index] = tmp_dict
                        seen_identities[identity] = (previous_index, enclosure)
                    continue
                seen_identities[identity] = (len(torrents), enclosure)
                torrents.append(tmp_dict)
            except Exception as e:
                # D8: item 级解析异常附带 URL 与异常类型,降为 DEBUG 避免刷屏
                logger.debug(
                    f"【{self.plugin_name}】torznab item 解析失败,已跳过：url={log_url}, "
                    f"类型={type(e).__name__}")
                continue

        ambiguous_page_urls = find_ambiguous_torznab_page_urls([
            (
                getattr(torrent, "page_url", None),
                getattr(torrent, "enclosure", None),
            )
            for torrent in torrents
        ])
        if ambiguous_page_urls:
            cleared = 0
            for torrent in torrents:
                page_url = str(getattr(torrent, "page_url", None) or "").strip()
                if page_url in ambiguous_page_urls:
                    torrent.page_url = None
                    cleared += 1
            logger.debug(
                f"【{self.plugin_name}】检测到 {len(ambiguous_page_urls)} 个共享详情页 URL，"
                f"已清理 {cleared} 条资源的 page_url，避免宿主资源列表误去重"
            )

        return torrents

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        config_snapshot, generation = self._capture_request_context()
        # 动态生成索引器多选选项(完整列表,不受白名单过滤影响,否则无法取消勾选)
        # E1/G4: 优先使用 TTL 缓存,避免每次打开表单都请求 Prowlarr
        site_options = []
        try:
            indexers = self.get_indexers(
                filter_selected=False,
                config_snapshot=config_snapshot,
                generation=generation,
            )
            if not self._sync_is_current(generation):
                indexers = None
            for idx in indexers or []:
                site_options.append({"title": f"{idx.get('name', '')} ({idx.get('indexer_id', '')})",
                                     # C2: 以 indexer_id 为单一事实来源,合成名仅用于显示
                                     "value": idx.get('indexer_id')})
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】获取索引器选项失败: {type(e).__name__}")
        return build_form(
            site_options,
            timeout_default=self.SEARCH_TIMEOUT_DEFAULT,
            timeout_min=self.SEARCH_TIMEOUT_MIN,
            timeout_max=self.SEARCH_TIMEOUT_MAX,
        )

    def _ensure_sites_loaded(self, config_snapshot: Optional[dict] = None,
                             generation: Optional[int] = None) -> bool:
        """
        确保 self._indexers 已加载数据，若为空则尝试重新加载。
        :return: 成功加载返回 True，否则 False
        """
        current_snapshot, current_generation = self._capture_request_context()
        request_snapshot = (
            dict(config_snapshot)
            if config_snapshot is not None
            else current_snapshot
        )
        request_generation = (
            generation
            if generation is not None
            else current_generation
        )
        if not self._sync_is_current(request_generation):
            return False

        with self._state_lock:
            current = self._indexers
        if isinstance(current, list) and len(current) > 0:
            return self._sync_is_current(request_generation)

        # E1/G4: 详情页优先使用 TTL 缓存,不强制实时请求
        indexers = self.get_indexers(
            filter_selected=True,
            config_snapshot=request_snapshot,
            generation=request_generation,
        )
        if indexers is None or not indexers:
            return False
        with self._state_lock:
            if not self._sync_is_current(request_generation):
                return False
            self._indexers = copy.deepcopy(indexers)
            self._fetch_ok = True
        return self._sync_is_current(request_generation)

    def get_page(self) -> List[dict]:
        """
            拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        config_snapshot, generation = self._capture_request_context()
        if not self._ensure_sites_loaded(
                config_snapshot=config_snapshot,
                generation=generation):
            return []

        with self._state_lock:
            if not self._sync_is_current(generation):
                return []
            indexers = (
                copy.deepcopy(self._indexers)
                if isinstance(self._indexers, list)
                else []
            )
        if not self._sync_is_current(generation):
            return []
        return build_page(indexers)
