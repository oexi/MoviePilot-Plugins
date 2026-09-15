# _*_ coding: utf-8 _*_
"""Repository-canonical pure Torznab helpers shared by indexer plugins.

MoviePilot plugins are packaged independently, so this source is mirrored
into each plugin directory by ``tools/sync_shared_modules.py``.  Keep only
host-independent, side-effect-free parsing and validation logic here;
provider-specific URL construction and download-link policy stay local.
"""

from __future__ import annotations

import codecs
import math
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = {
    "apikey",
    "api_key",
    "jackett_apikey",
    "password",
    "token",
    "q",
}

_XML_DTD_DECLARATION_RE = re.compile(
    r"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)\b",
    re.IGNORECASE,
)
_XML_DECLARATION_RE = re.compile(
    rb"\A(?:\xef\xbb\xbf)?\s*<\?xml(?=\s)(?P<declaration>.*?)\?>",
    re.IGNORECASE | re.DOTALL,
)
_XML_ENCODING_RE = re.compile(
    rb"\bencoding\s*=\s*(['\"])(?P<encoding>[^'\"]+)\1",
    re.IGNORECASE,
)


def _xml_encoding_for_scan(body: bytes) -> str:
    """Choose the wire encoding needed to inspect XML markup safely."""
    if body.startswith(codecs.BOM_UTF32_LE):
        return "utf-32-le"
    if body.startswith(codecs.BOM_UTF32_BE):
        return "utf-32-be"
    if body.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le"
    if body.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be"
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8"

    if body.startswith(b"\x00\x00\x00<"):
        return "utf-32-be"
    if body.startswith(b"<\x00\x00\x00"):
        return "utf-32-le"
    if body.startswith(b"\x00<\x00?"):
        return "utf-16-be"
    if body.startswith(b"<\x00?\x00"):
        return "utf-16-le"
    if len(body) >= 4:
        if body[1] == 0 and body[3] == 0:
            return "utf-32-le" if body[2] == 0 else "utf-16-le"
        if body[0] == 0 and body[2] == 0:
            return "utf-32-be" if body[1] == 0 else "utf-16-be"

    declaration = _XML_DECLARATION_RE.match(body)
    if declaration:
        encoding_match = _XML_ENCODING_RE.search(declaration.group("declaration"))
        if encoding_match:
            try:
                declared = encoding_match.group("encoding").decode("ascii").strip()
            except UnicodeDecodeError as error:
                raise UnicodeError("XML encoding declaration is not ASCII") from error
            if not declared:
                raise UnicodeError("XML encoding declaration is empty")
            return codecs.lookup(declared).name
    return "utf-8"


def contains_xml_dtd(body: object) -> bool:
    """Return whether XML text/bytes contain active DTD/entity declarations."""
    if isinstance(body, str):
        text = body
    elif isinstance(body, (bytes, bytearray, memoryview)):
        raw_body = bytes(body)
        text = raw_body.decode(_xml_encoding_for_scan(raw_body))
    else:
        raise TypeError("XML body must be text or bytes")

    position = 0
    while position < len(text):
        if text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            if end < 0:
                return False
            position = end + 3
            continue
        if text.startswith("<![CDATA[", position):
            end = text.find("]]>", position + 9)
            if end < 0:
                return False
            position = end + 3
            continue
        if text.startswith("<?", position):
            end = text.find("?>", position + 2)
            if end < 0:
                return False
            position = end + 2
            continue
        if _XML_DTD_DECLARATION_RE.match(text, position):
            return True
        position += 1
    return False


def safe_int(value: object) -> int:
    """Convert an integer field, returning zero for malformed values."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_float(value: object) -> float:
    """Convert a finite, non-negative float or return ``0.0``."""
    if isinstance(value, bool):
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return numeric if math.isfinite(numeric) and numeric >= 0 else 0.0


def safe_count(value: object) -> int:
    """Convert a count to a non-negative integer or return zero."""
    if isinstance(value, bool):
        return 0
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return numeric if numeric >= 0 else 0


def safe_float_none(value: object) -> Optional[float]:
    """Convert a finite, non-negative optional float or return ``None``."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def normalize_imdbid(value: object) -> str:
    """Normalize a valid IMDb title id without inventing an identity."""
    candidate = str(value or "").strip().lower()
    if not re.fullmatch(r"tt[0-9]{7,}", candidate):
        return ""
    if set(candidate[2:]) == {"0"}:
        return ""
    return candidate


def _dom_tag_value(
        node: object,
        tag: str,
        attr: Optional[str] = None,
        default: object = None,
) -> object:
    """Read the first matching DOM tag using stdlib DOM operations only."""
    if node is None or not hasattr(node, "getElementsByTagName"):
        return default
    try:
        elements = node.getElementsByTagName(tag)
    except Exception:
        return default
    if not elements:
        return default
    element = elements[0]
    if attr:
        return element.getAttribute(attr) or default
    first_child = getattr(element, "firstChild", None)
    if first_child is not None and hasattr(first_child, "data"):
        return first_child.data
    return default


def _torznab_attrs(item: object):
    """Yield Torznab attr elements while accepting namespace-prefix variants."""
    if item is None or not hasattr(item, "getElementsByTagName"):
        return
    try:
        elements = item.getElementsByTagName("torznab:attr")
    except Exception:
        elements = []
    yielded = set()
    for element in elements:
        yielded.add(id(element))
        yield element
    try:
        all_elements = item.getElementsByTagName("*")
    except Exception:
        all_elements = []
    for element in all_elements:
        if id(element) in yielded:
            continue
        tag_name = str(getattr(element, "tagName", "") or "")
        local_name = str(getattr(element, "localName", "") or "")
        if tag_name.rsplit(":", 1)[-1].lower() != "attr" and local_name.lower() != "attr":
            continue
        namespace = str(getattr(element, "namespaceURI", "") or "")
        if namespace and "torznab" not in namespace.lower():
            continue
        yield element


def extract_torznab_item(item: object) -> dict:
    """Extract stable RSS/Torznab fields without importing MoviePilot."""
    fields = {
        "title": _dom_tag_value(item, "title", default=""),
        "enclosure": _dom_tag_value(item, "enclosure", "url", default=""),
        "link": _dom_tag_value(item, "link", default=""),
        "guid": _dom_tag_value(item, "guid", default=""),
        "description": _dom_tag_value(item, "description", default=""),
        "size": _dom_tag_value(item, "size", default=0),
        "page_url": _dom_tag_value(item, "comments", default=""),
        "pubdate": _dom_tag_value(item, "pubDate", default=""),
        "seeders": 0,
        "peers": 0,
        "imdbid": "",
        "infohash": "",
        "grabs": 0,
        "uploadvolumefactor": None,
        "downloadvolumefactor": None,
        "hit_and_run": False,
        "magnet_url": "",
        "labels": [],
    }
    for torznab_attr in _torznab_attrs(item):
        try:
            name = str(torznab_attr.getAttribute("name") or "").strip().lower()
            value = torznab_attr.getAttribute("value")
        except Exception:
            continue
        if name == "seeders":
            fields["seeders"] = value
        elif name == "peers":
            fields["peers"] = value
        elif name == "downloadvolumefactor":
            fields["downloadvolumefactor"] = value
        elif name == "uploadvolumefactor":
            fields["uploadvolumefactor"] = value
        elif name == "hit_and_run":
            fields["hit_and_run"] = str(value).strip().lower() in ("1", "true", "yes")
        elif name in ("imdb", "imdbid"):
            imdb_value = str(value or "").strip().lower()
            if name == "imdb" and re.fullmatch(r"[0-9]{7,}", imdb_value):
                imdb_value = f"tt{imdb_value}"
            fields["imdbid"] = normalize_imdbid(imdb_value)
        elif name in ("infohash", "info_hash"):
            fields["infohash"] = str(value).strip()
        elif name == "magneturl":
            fields["magnet_url"] = str(value or "").strip()
        elif name == "grabs":
            fields["grabs"] = value
        elif name in ("label", "tag"):
            label = str(value).strip()
            if label and label not in fields["labels"]:
                fields["labels"].append(label)
    return fields


def _supported_url(value: object, schemes: tuple[str, ...]) -> Optional[str]:
    """Return a URL only when its scheme and required authority are valid."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in schemes:
        return None
    if scheme in ("http", "https") and not parsed.netloc:
        return None
    if scheme == "magnet" and not (parsed.query or parsed.path):
        return None
    return candidate


def select_torznab_identity(
        infohash: object,
        guid: object,
        page_url: object,
        enclosure: object,
) -> tuple[str, str]:
    """Choose one stable result identity in historical priority order."""
    for kind, value in (
        ("infohash", infohash),
        ("guid", guid),
        ("page_url", page_url),
        ("enclosure", enclosure),
    ):
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return kind, text.lower()
    return "enclosure", str(enclosure or "").strip().lower()


def find_ambiguous_torznab_page_urls(pairs: object) -> set[str]:
    """Return shared page URLs that point at more than one download resource.

    Some indexers expose a site/home page in ``comments`` for every item.
    MoviePilot's resource browser may use ``page_url`` as an item identity, so
    retaining such a shared URL can collapse otherwise distinct torrents into
    one visible row.  Only exact shared URLs backed by multiple distinct
    enclosures are considered ambiguous; normal per-release detail URLs are
    untouched.
    """
    if not isinstance(pairs, (list, tuple)):
        return set()
    grouped: dict[str, set[str]] = {}
    for value in pairs:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        page_url = str(value[0] or "").strip()
        enclosure = str(value[1] or "").strip()
        if not page_url or not enclosure:
            continue
        grouped.setdefault(page_url, set()).add(enclosure)
    return {
        page_url
        for page_url, enclosures in grouped.items()
        if len(enclosures) > 1
    }


def is_http_torznab_url(value: object) -> bool:
    """Return whether a value is a valid HTTP(S) URL."""
    return bool(_supported_url(value, ("http", "https")))


def should_replace_torznab_duplicate(
        previous_enclosure: object,
        current_enclosure: object,
) -> bool:
    """Prefer an HTTP torrent when it duplicates an earlier magnet result."""
    return is_http_torznab_url(current_enclosure) and not is_http_torznab_url(previous_enclosure)


def classify_torznab_response(
        status_code: object,
        content_type: object,
        text: object,
) -> str:
    """Classify a response before XML parsing without retaining its body."""
    if isinstance(status_code, bool):
        return "http_error"
    try:
        status = int(status_code)
    except (TypeError, ValueError, OverflowError):
        return "http_error"
    if status != 200:
        return "http_error"
    if isinstance(text, bytes):
        body = text.decode("utf-8", errors="replace")
    elif isinstance(text, str):
        body = text
    else:
        body = ""
    if not body.strip():
        return "empty"
    content = str(content_type or "").lower()
    if "json" in content or body.lstrip().startswith(("{", "[")):
        return "json"
    return "ok"


def is_usable_torznab_response(
        status_code: object,
        content_type: object,
        text: object,
) -> bool:
    """Return whether status/body are suitable for Torznab XML parsing."""
    return classify_torznab_response(status_code, content_type, text) == "ok"


def redact_url(url: object) -> str:
    """Mask credentials and search text in a diagnostic URL."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if hostname and ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        try:
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
        except ValueError:
            return "<invalid-url>"
        query = urlencode([
            (key, "***" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return "<invalid-url>"
