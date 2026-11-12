"""Derive a readable plain-text rendition from HTML.

Used for the mandatory plain-text version of the combined PDF when a mail has no
``text/plain`` part (HTML-only mail). Block elements become line breaks and list
items get a bullet, so the result reads like text, not a run-on line.
"""

from __future__ import annotations

import re

from lxml import html as lxml_html
from lxml.etree import ParserError

# Block-level tags after which a line break is inserted.
_BLOCK_TAGS = (
    "p",
    "div",
    "li",
    "tr",
    "table",
    "ul",
    "ol",
    "blockquote",
    "pre",
    "section",
    "article",
    "header",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
)
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\ufeff]+")

# Private-use markers for where a quote opens and closes. They survive
# text_content() and are turned into ``>`` prefixes line by line afterwards.
_QUOTE_OPEN = "\ue000"
_QUOTE_CLOSE = "\ue001"

# Tags that carry visual meaning beyond plain text. If a mail's HTML contains any
# of them, it is NOT a trivial text wrapper and must be rendered as HTML.
_RICH_TAGS = frozenset(
    {
        "img",
        "picture",
        "figure",
        "figcaption",
        "svg",
        "canvas",
        "video",
        "audio",
        "iframe",
        "object",
        "embed",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "colgroup",
        "col",
        "a",
        "dl",
        "dt",
        "dd",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "code",
        "tt",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "strike",
        "mark",
        "sub",
        "sup",
        "font",
        "big",
        "small",
        "button",
        "input",
        "select",
        "textarea",
        "label",
        "form",
        "details",
        "summary",
    }
)
# A background or border in a style makes a visible box/colour beyond text.
_BOX_STYLE = re.compile(r"\b(background|border)\b", re.IGNORECASE)
_BACKGROUND_STYLE = re.compile(r"\bbackground\b", re.IGNORECASE)

# Mail-client scaffolding that is *not* in _RICH_TAGS on purpose: quotes
# (``blockquote``), simple lists and preformatted text (``pre``, Apple Mail's
# quote wrapper) all survive as text — the text/plain part writes them as ``>``
# lines, ``*`` bullets and plain lines. A quote's own border is the familiar quote
# bar (Gmail), not a box, so only a background counts against it.
_QUOTE_TAG = "blockquote"

# ``<font>`` attributes that only change the typeface or size, never meaning.
_PLAIN_FONT_ATTRS = frozenset({"face", "size", "style"})

_LINK_PREFIX = re.compile(r"^(mailto:|https?://)", re.IGNORECASE)


def _link_target(text: str) -> str:
    """Reduce a link text or href to the address it names, for comparison."""
    bare = _LINK_PREFIX.sub("", text.strip().strip("<>").strip())
    return bare.rstrip("/").lower()


def _is_text_equivalent(element: lxml_html.HtmlElement, tag: str) -> bool:
    """True for a rich-looking tag whose rendering adds nothing to its text.

    A link counts only when its visible text *is* its target — the autolink a
    mail client makes of a typed address or URL. Any other link hides where it
    goes, which only the HTML rendition shows.
    """
    if tag == "a":
        text = element.text_content()
        href = element.get("href") or ""
        return bool(text.strip()) and _link_target(text) == _link_target(href)
    if tag == "font":
        return set(element.attrib) <= _PLAIN_FONT_ATTRS
    return False


def html_is_trivial_wrapper(html: str) -> bool:
    """True if the HTML carries no visual meaning beyond its plain text.

    Conservative on purpose: it returns True only when the HTML is *provably*
    just text wrapped in structural tags (``p``/``div``/``span``/``br`` …) and
    the scaffolding mail clients add to plain replies (quotes, simple lists,
    ``pre``, a ``font`` face, autolinked addresses), with no image, table, real
    link, heading, emphasis, background or border. Then the
    scaled/reflowed HTML renditions add nothing over the plain-text version and
    can be skipped. Anything richer — anything that could look different in the
    PDF — makes it return False, so real formatting is never flattened away.
    """
    if not html or not html.strip():
        return False
    try:
        tree: lxml_html.HtmlElement = lxml_html.fromstring(html)
    except (ParserError, ValueError):
        return False
    for element in tree.iter():
        tag = element.tag
        if not isinstance(tag, str):  # comments / processing instructions
            continue
        local = tag.rsplit("}", 1)[-1].lower()  # drop any XML namespace
        if local in _RICH_TAGS and not _is_text_equivalent(element, local):
            return False
        style = element.get("style")
        box = _BACKGROUND_STYLE if local == _QUOTE_TAG else _BOX_STYLE
        if style and box.search(style):
            return False
        if local == "style" and element.text and _BOX_STYLE.search(element.text):
            return False
    return True


def html_to_text(html: str) -> str:
    """Return a plain-text rendition of ``html`` (empty string if unparseable)."""
    if not html or not html.strip():
        return ""
    try:
        tree: lxml_html.HtmlElement = lxml_html.fromstring(html)
    except (ParserError, ValueError):
        return ""

    for element in tree.iter("script", "style"):
        element.drop_tree()
    for br in tree.iter("br"):
        br.tail = "\n" + (br.tail or "")
    for li in tree.iter("li"):
        li.text = "• " + (li.text or "")
    for quote in tree.iter("blockquote"):
        quote.text = "\n" + _QUOTE_OPEN + (quote.text or "")
        if len(quote):
            quote[-1].tail = (quote[-1].tail or "") + _QUOTE_CLOSE
        else:
            quote.text += _QUOTE_CLOSE
    for tag in _BLOCK_TAGS:
        for element in tree.iter(tag):
            element.tail = "\n" + (element.tail or "")

    lines = _quote_prefixed(tree.text_content().splitlines())
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _quote_prefixed(raw_lines: list[str]) -> list[str]:
    """Clean each line and prefix it with one ``>`` per enclosing quote.

    A line takes the depth reached after the quotes it opens and before the ones
    it closes, so a quote's first and last lines are prefixed like its middle.
    """
    depth = 0
    lines: list[str] = []
    for raw in raw_lines:
        depth += raw.count(_QUOTE_OPEN)
        line = _SPACES.sub(" ", raw.replace(_QUOTE_OPEN, "").replace(_QUOTE_CLOSE, ""))
        line = line.strip()
        lines.append(f"{'>' * depth} {line}".rstrip() if depth and line else line)
        depth = max(0, depth - raw.count(_QUOTE_CLOSE))
    return lines
