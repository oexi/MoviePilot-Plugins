# _*_ coding: utf-8 _*_
"""Jackett-specific Torznab download selection on top of the pure core."""

from __future__ import annotations

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


def select_torznab_enclosure(
        enclosure: object = None,
        link: object = None,
        magnet_url: object = None,
        guid: object = None,
) -> str:
    """Select Jackett's protected HTTP download before a magnet fallback.

    Jackett rewrites an indexer's download link to its own protected ``/dl``
    endpoint before emitting Torznab XML.  Both ``enclosure`` and ``link`` are
    therefore valid HTTP(S) download candidates for this provider.  Detail or
    comments URLs are never passed here as dedicated download candidates.
    """
    for value in (enclosure, link):
        direct_url = _supported_url(value, ("http", "https"))
        if direct_url:
            return direct_url

    for value in (enclosure, link, magnet_url, guid):
        magnet = _supported_url(value, ("magnet",))
        if magnet:
            return magnet

    return ""
