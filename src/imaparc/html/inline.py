"""Make email HTML self-contained and safe before it reaches the browser.

The browser must never see a ``cid:`` reference or an ``http(s):`` URL:
  * ``cid:`` references are resolved to ``data:`` URIs from the mail's inline
    parts, so images load from memory (making ``wait_until="load"``
    deterministic);
  * every remote resource is stripped, so no tracking pixel survives in the DOM;
  * active content (scripts, iframes, event handlers) is removed.

This is deliberately *not* an allowlist sanitiser: email relies on table
layout, ``bgcolor`` and heavy inline CSS, which an allowlist would silently
degrade. We remove specific dangerous things and leave layout untouched.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import html as lxml_html
from lxml.etree import ParserError

from imaparc.mail.models import AttachmentPart

logger = logging.getLogger(__name__)

# url(...) inside CSS, capturing the (optionally quoted) target.
_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)

# @import with a bare quoted string (the url(...) form is handled by _CSS_URL).
_CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.IGNORECASE)

# Elements removed wholesale.
_DANGEROUS_TAGS = ("script", "iframe", "object", "embed", "applet", "base")

# Attributes carrying a resource URL that we rewrite.
_SRC_ATTRS = ("src", "background", "poster")

# SVG resource attributes that auto-fetch (on <image>/<use>); the html parser
# may expose the xlink form namespaced or literal, so we check every spelling.
_SVG_HREF_ATTRS = ("href", "xlink:href", "{http://www.w3.org/1999/xlink}href")
_SVG_HREF_TAGS = ("image", "use")

# <link> rels that make the browser fetch a remote resource.
_FETCHING_LINK_RELS = frozenset(
    {
        "stylesheet",
        "preload",
        "prefetch",
        "dns-prefetch",
        "preconnect",
        "prerender",
        "icon",
        "shortcut",  # from "shortcut icon"
        "apple-touch-icon",
    }
)


@dataclass(slots=True)
class InlineResult:
    """Outcome of making one HTML body self-contained.

    Attributes:
        html: The rewritten document.
        unresolved_cids: cid: references with no matching part.
        blocked_remote: Remote URLs removed from the document.
        kept_remote: Remote URLs deliberately left in (``allow_remote`` mode).
        allow_remote: Which policy was applied.
    """

    html: str
    unresolved_cids: list[str] = field(default_factory=list)
    blocked_remote: list[str] = field(default_factory=list)
    kept_remote: list[str] = field(default_factory=list)
    allow_remote: bool = False


def _norm_location(url: str) -> str:
    """Normalise a Content-Location reference for matching (RFC 2557).

    Strips a leading ``./`` so a body ``./image001.png`` matches a part whose
    Content-Location is ``image001.png`` and vice versa. Full base-URI resolution
    is out of scope; this covers the common relative form.
    """
    return url[2:] if url.startswith("./") else url


def _build_maps(
    parts: list[AttachmentPart],
) -> tuple[dict[str, AttachmentPart], dict[str, AttachmentPart]]:
    """Index inline parts by Content-ID and by Content-Location."""
    by_cid: dict[str, AttachmentPart] = {}
    by_location: dict[str, AttachmentPart] = {}
    for part in parts:
        # A Content-ID is an RFC 5322 msg-id whose local part is case-sensitive
        # (RFC 2392) — index it verbatim so distinct IDs never collide.
        if part.content_id:
            by_cid[part.content_id] = part
        if part.content_location:
            by_location[_norm_location(part.content_location)] = part
    return by_cid, by_location


def _data_uri(part: AttachmentPart) -> str:
    b64 = base64.b64encode(part.content).decode("ascii")
    return f"data:{part.content_type};base64,{b64}"


def _resolve_reference(
    url: str,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
) -> str | None:
    """Resolve a cid:/Content-Location URL to a data: URI, or None."""
    if url.lower().startswith("cid:"):
        cid = unquote(url[4:]).strip().lstrip("<").rstrip(">")
        part = by_cid.get(cid)
        return _data_uri(part) if part else None
    part = by_location.get(_norm_location(url))
    return _data_uri(part) if part else None


def _is_remote(url: str) -> bool:
    return url.lower().startswith(("http://", "https://", "//", "ftp:"))


def inline_resources(
    html: str, parts: list[AttachmentPart], *, allow_remote: bool = False
) -> InlineResult:
    """Return a self-contained, tracking-free version of the HTML.

    Args:
        html: The email's HTML body.
        parts: All non-body parts; those with a Content-ID or Content-Location
            are candidates for inline resolution.
        allow_remote: When True, remote image URLs are left in place so the
            browser may fetch them. This trades privacy and reproducibility
            for visual fidelity — the sender learns when the mail was
            processed, and the result depends on network availability.

    Returns:
        The rewritten HTML plus the lists of unresolved cid references and
        blocked remote URLs (for logging / audit).
    """
    by_cid, by_location = _build_maps(parts)
    result = InlineResult(html="", allow_remote=allow_remote)
    try:
        tree = lxml_html.fromstring(html)
    except (ParserError, ValueError):
        # Empty or unparseable body: return a minimal safe document.
        result.html = "<html><body></body></html>"
        return result

    _strip_dangerous(tree, result)
    _rewrite_resource_attrs(tree, by_cid, by_location, result)
    _rewrite_inline_styles(tree, by_cid, by_location, result)
    _rewrite_style_elements(tree, by_cid, by_location, result)

    result.html = str(lxml_html.tostring(tree, encoding="unicode"))
    return result


def _strip_dangerous(tree: lxml_html.HtmlElement, result: InlineResult) -> None:
    """Remove active content, remote fetching links, refresh redirects, handlers."""
    for tag in _DANGEROUS_TAGS:
        for element in tree.iter(tag):
            element.drop_tree()
    for link in list(tree.iter("link")):
        rels = (link.get("rel") or "").lower().split()
        href = link.get("href") or ""
        if _is_remote(href) and any(rel in _FETCHING_LINK_RELS for rel in rels):
            result.blocked_remote.append(href)
            link.drop_tree()
    for meta in list(tree.iter("meta")):
        if (meta.get("http-equiv") or "").lower() == "refresh":
            meta.drop_tree()
    # Strip event handlers everywhere. (srcset is NOT dropped here — it carries
    # resource URLs that must go through the same cid:/remote policy as src, so
    # it is handled per-candidate in _rewrite_srcset.)
    for element in tree.iter():
        for attr in list(element.keys()):
            if attr.lower().startswith("on"):
                del element.attrib[attr]


def _rewrite_resource_attrs(
    tree: lxml_html.HtmlElement,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> None:
    """Resolve cid: in src/background/poster/srcset and SVG/link href; block remote."""
    for element in tree.iter():
        for attr in _SRC_ATTRS:
            _rewrite_url_attr(element, attr, by_cid, by_location, result)
        if element.get("srcset"):
            _rewrite_srcset(element, by_cid, by_location, result)
        local = _local_tag(element)
        # A <link rel=stylesheet/preload/…> with a cid: href must be resolved to
        # a data: URI or dropped — the locked-down browser cannot fetch cid:.
        if local == "link":
            _rewrite_link(element, by_cid, by_location, result)
        # SVG <image>/<use> auto-fetch via href/xlink:href — a plain <a href>
        # is a link, not a resource load, so those are left untouched.
        if local in _SVG_HREF_TAGS:
            for attr in _SVG_HREF_ATTRS:
                _rewrite_url_attr(
                    element, attr, by_cid, by_location, result, placeholder=False
                )


def _rewrite_srcset(
    element: lxml_html.HtmlElement,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> None:
    """Apply the cid:/remote policy to each candidate of a ``srcset`` attribute.

    ``srcset`` is a comma-separated list of ``url [descriptor]`` candidates. Each
    URL is resolved (cid:/Content-Location → data:) or run through the remote
    policy just like ``src``; a candidate that is blocked/unresolved is dropped.
    If nothing survives, the attribute is removed so no broken reference remains.
    """
    kept: list[str] = []
    for candidate in element.get("srcset", "").split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        url, _, descriptor = candidate.partition(" ")
        suffix = f" {descriptor.strip()}" if descriptor.strip() else ""
        low = url.lower()
        if low.startswith("data:"):
            kept.append(candidate)
        elif low.startswith("cid:") or (_norm_location(url) in by_location):
            resolved = _resolve_reference(url, by_cid, by_location)
            if resolved is not None:
                kept.append(f"{resolved}{suffix}")
            else:
                result.unresolved_cids.append(url)
        elif _is_remote(url):
            if result.allow_remote:
                result.kept_remote.append(url)
                kept.append(candidate)
            else:
                result.blocked_remote.append(url)
        else:
            kept.append(candidate)
    if kept:
        element.set("srcset", ", ".join(kept))
    else:
        del element.attrib["srcset"]


def _rewrite_link(
    element: lxml_html.HtmlElement,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> None:
    """Resolve a cid: href on a fetching <link>, or drop the link if it cannot be."""
    rels = (element.get("rel") or "").lower().split()
    if not any(rel in _FETCHING_LINK_RELS for rel in rels):
        return
    href = element.get("href") or ""
    low = href.lower()
    if not (low.startswith("cid:") or (_norm_location(href) in by_location)):
        return  # remote fetching links are already handled in _strip_dangerous
    resolved = _resolve_reference(href, by_cid, by_location)
    if resolved is not None:
        element.set("href", resolved)
    else:
        result.unresolved_cids.append(href)
        element.drop_tree()


def _local_tag(element: lxml_html.HtmlElement) -> str:
    """The lowercased local name of an element, namespace stripped."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _rewrite_url_attr(
    element: lxml_html.HtmlElement,
    attr: str,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
    *,
    placeholder: bool = True,
) -> None:
    """Resolve a cid: reference, or neutralise a remote one, in one attribute.

    ``placeholder`` controls how a blocked/unresolved reference is neutralised:
    a transparent pixel for image-bearing attributes (keeping layout), or plain
    removal for SVG href where a pixel data URI would be meaningless.
    """
    url = element.get(attr)
    if not url:
        return
    low = url.lower()
    if low.startswith("data:"):
        return

    def neutralise() -> None:
        if placeholder:
            element.set(attr, _PLACEHOLDER_PIXEL)
        else:
            del element.attrib[attr]

    if low.startswith("cid:") or (_norm_location(url) in by_location):
        resolved = _resolve_reference(url, by_cid, by_location)
        if resolved is not None:
            element.set(attr, resolved)
        else:
            result.unresolved_cids.append(url)
            neutralise()
            if placeholder:
                element.set("alt", f"[unresolved: {url}]")
    elif _is_remote(url):
        if result.allow_remote:
            result.kept_remote.append(url)
        else:
            result.blocked_remote.append(url)
            neutralise()


def _rewrite_inline_styles(
    tree: lxml_html.HtmlElement,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> None:
    """Rewrite url(...) inside style="" attributes."""
    for element in tree.iter():
        style = element.get("style")
        if style and _has_css_ref(style):
            element.set("style", _rewrite_css(style, by_cid, by_location, result))


def _rewrite_style_elements(
    tree: lxml_html.HtmlElement,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> None:
    """Rewrite url(...) inside <style> element text."""
    for style in tree.iter("style"):
        if style.text and _has_css_ref(style.text):
            style.text = _rewrite_css(style.text, by_cid, by_location, result)


def _has_css_ref(css: str) -> bool:
    """True if the CSS holds a url(...) or @import worth rewriting."""
    low = css.lower()
    return "url(" in low or "@import" in low


def _rewrite_css(
    css: str,
    by_cid: dict[str, AttachmentPart],
    by_location: dict[str, AttachmentPart],
    result: InlineResult,
) -> str:
    """Resolve cid: and remove remote url(...)/@import targets in CSS."""

    def repl(match: re.Match[str]) -> str:
        url = match.group(2).strip()
        low = url.lower()
        if low.startswith("data:"):
            return match.group(0)
        if low.startswith("cid:") or (_norm_location(url) in by_location):
            resolved = _resolve_reference(url, by_cid, by_location)
            if resolved is not None:
                return f"url({resolved})"
            result.unresolved_cids.append(url)
            return "none"
        if _is_remote(url):
            if result.allow_remote:
                result.kept_remote.append(url)
                return match.group(0)
            result.blocked_remote.append(url)
            return "none"
        return match.group(0)

    def import_repl(match: re.Match[str]) -> str:
        url = match.group(2).strip()
        if _is_remote(url) and not result.allow_remote:
            result.blocked_remote.append(url)
            return "@import none"
        if _is_remote(url):
            result.kept_remote.append(url)
        return match.group(0)

    # Neutralise bare-string @imports first, then rewrite every url(...).
    return _CSS_URL.sub(repl, _CSS_IMPORT.sub(import_repl, css))


# A transparent 1x1 GIF used in place of blocked / unresolved images.
_PLACEHOLDER_PIXEL = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
