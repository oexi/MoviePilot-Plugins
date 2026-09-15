# _*_ coding: utf-8 _*_
"""Prowlarr-specific Torznab helpers on top of the repository pure core."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from ._torznab_core import (
    _supported_url,
    classify_torznab_response,
    contains_xml_dtd,
    extract_torznab_item,
    find_ambiguous_torznab_page_urls,
    is_http_torznab_url,
    is_usable_torznab_response,
    normalize_imdbid,
    redact_url,
    safe_count,
    safe_float,
    safe_float_none,
    safe_int,
    select_torznab_identity,
    should_replace_torznab_duplicate,
)


MIN_INDEXER_ID = 1
MAX_INDEXER_ID = 2_147_483_647
_MAX_INDEXER_ID_TEXT_LENGTH = len(str(MAX_INDEXER_ID))


def normalize_indexer_id(value: object) -> str:
    """Return a bounded positive numeric indexer id, or ``""``."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate or len(candidate) > _MAX_INDEXER_ID_TEXT_LENGTH:
            return ""
        if not re.fullmatch(r"[0-9]+", candidate):
            return ""
        try:
            numeric = int(candidate, 10)
        except (TypeError, ValueError, OverflowError):
            return ""
    else:
        return ""
    if numeric < MIN_INDEXER_ID or numeric > MAX_INDEXER_ID:
        return ""
    return str(numeric)


def parse_indexer_id(value: object) -> Optional[int]:
    """Parse an id after applying the same validation as URL construction."""
    normalized = normalize_indexer_id(value)
    return int(normalized) if normalized else None


def is_valid_indexer_id(value: object) -> bool:
    """Return whether an id can safely identify a Prowlarr endpoint."""
    return bool(normalize_indexer_id(value))


validate_indexer_id = is_valid_indexer_id


def _base_url(host: object) -> str:
    """Normalize an HTTP(S) host while preserving an optional base path."""
    if host is None:
        return ""
    value = str(host).strip()
    if not value:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = "http://" + value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_torznab_url(
        host: object,
        indexer_id: object,
        api_key: object = "",
        keyword: object = "",
        cat: object = None,
        *,
        query_type: str = "search",
        params: Optional[Mapping] = None,
        query: Optional[Mapping] = None,
        include_api_key: bool = False,
        **extra: object,
) -> str:
    """Build a Prowlarr per-indexer Newznab URL with encoded query values."""
    base = _base_url(host)
    normalized_id = normalize_indexer_id(indexer_id)
    if not base or not normalized_id:
        return ""
    query_values = {
        "t": str(query_type or "search"),
        "q": "" if keyword is None else str(keyword),
    }
    if include_api_key:
        query_values["apikey"] = "" if api_key is None else str(api_key)
    if cat is not None and str(cat).strip():
        query_values["cat"] = cat
    if isinstance(api_key, Mapping) and params is None and query is None:
        params = api_key
    for query_params in (query, params):
        if not isinstance(query_params, Mapping):
            continue
        for key, value in query_params.items():
            if key is None:
                continue
            query_values[str(key)] = value
    if cat is None and "category" in extra:
        category = extra.pop("category")
        if category is not None and str(category).strip():
            query_values["cat"] = category
    for key, value in extra.items():
        if key == "category":
            continue
        if value is not None:
            query_values[str(key)] = value
    query_string = urlencode(query_values, doseq=True, quote_via=quote_plus)
    return f"{base}/api/v1/indexer/{normalized_id}/newznab?{query_string}"


def build_indexer_torznab_url(*args: object, **kwargs: object) -> str:
    """Descriptive alias for :func:`build_torznab_url`."""
    return build_torznab_url(*args, **kwargs)


def _looks_like_download_link(value: object) -> bool:
    """Distinguish a Prowlarr detail page from a direct ``link`` download."""
    direct = _supported_url(value, ("http", "https"))
    if not direct:
        return False
    try:
        parsed = urlsplit(direct)
    except ValueError:
        return False
    path = parsed.path.lower().rstrip("/")
    if path.endswith(".torrent") or path.endswith(".magnet"):
        return True
    if "/dl" in path or "/download" in path:
        return True
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    return bool(query_keys.intersection({"download", "downloadurl", "torrent", "torrentfile"}))


def select_torznab_enclosure(
        enclosure: object = None,
        link: object = None,
        magnet_url: object = None,
        guid: object = None,
        download_url: object = None,
) -> str:
    """Select a usable HTTP torrent URL, or fall back to a magnet exactly."""
    for value in (download_url, enclosure):
        direct_url = _supported_url(value, ("http", "https"))
        if direct_url:
            return direct_url
    if _looks_like_download_link(link):
        return str(link).strip()
    for value in (download_url, enclosure, link, magnet_url, guid):
        magnet = _supported_url(value, ("magnet",))
        if magnet:
            return magnet
    return ""


def dedupe_torznab_items(items: object) -> list:
    """Deduplicate extracted item mappings without mutating caller data."""
    if not isinstance(items, (list, tuple)):
        return []
    results = []
    seen = {}
    for value in items:
        if isinstance(value, Mapping):
            item = copy.deepcopy(dict(value))
        elif hasattr(value, "getElementsByTagName"):
            item = extract_torznab_item(value)
        else:
            continue
        enclosure = select_torznab_enclosure(
            enclosure=item.get("enclosure"),
            link=item.get("link"),
            magnet_url=item.get("magnet_url"),
            guid=item.get("guid"),
            download_url=item.get("download_url"),
        )
        if not enclosure:
            continue
        item["enclosure"] = enclosure
        identity = select_torznab_identity(
            item.get("infohash", ""),
            item.get("guid", ""),
            item.get("page_url", item.get("comments", "")),
            enclosure,
        )
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = (len(results), enclosure)
            results.append(item)
        elif should_replace_torznab_duplicate(previous[1], enclosure):
            results[previous[0]] = item
            seen[identity] = (previous[0], enclosure)
    return results


deduplicate_torznab_items = dedupe_torznab_items
build_newznab_url = build_torznab_url
build_indexer_url = build_torznab_url
build_search_url = build_torznab_url
build_prowlarr_torznab_url = build_torznab_url
classify_response = classify_torznab_response
