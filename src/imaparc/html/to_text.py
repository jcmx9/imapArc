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
_SPACES = re.compile(r"[ \t]+")

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
        "ul",
        "ol",
        "li",
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
        "blockquote",
        "pre",
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


def html_is_trivial_wrapper(html: str) -> bool:
    """True if the HTML carries no visual meaning beyond its plain text.

    Conservative on purpose: it returns True only when the HTML is *provably*
    just text wrapped in structural tags (``p``/``div``/``span``/``br`` …) with
    no image, table, link, list, heading, emphasis, background or border. Then the
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
        if local in _RICH_TAGS:
            return False
        style = element.get("style")
        if style and _BOX_STYLE.search(style):
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
    for tag in _BLOCK_TAGS:
        for element in tree.iter(tag):
            element.tail = "\n" + (element.tail or "")

    text = tree.text_content()
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()
