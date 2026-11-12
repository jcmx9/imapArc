"""Render the non-mail-body pages as self-contained HTML strings.

Everything here is a pure ``model -> str`` function, so it is testable without
a browser via HTML snapshots. The browser turns these strings into PDF pages
later, using the same Chromium as the mail body — one font stack, one engine.
"""

from __future__ import annotations

import functools
from html import escape
from importlib.resources import files

from jinja2 import Environment, PackageLoader, select_autoescape
from lxml import html as lxml_html
from lxml.etree import ParserError

from imaparc.mail.models import MailHeaders


@functools.lru_cache(maxsize=1)
def _base_css() -> str:
    return (files("imaparc.html.templates") / "base.css").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("imaparc.html", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
    )
    return env


def _render(template: str, **context: object) -> str:
    return _env().get_template(template).render(base_css=_base_css(), **context)


def render_separator(index: int, filename: str, pages: int, size: str) -> str:
    """Render the separator page shown before an attachment's pages."""
    return _render(
        "separator.html.j2",
        index=index,
        filename=filename,
        pages=pages,
        size=size,
    )


def render_info(
    index: int,
    filename: str,
    mime: str,
    size: str,
    reason: str,
    *,
    embedded: bool,
    eml_name: str,
) -> str:
    """Render the info page shown in place of a non-convertible attachment.

    ``embedded`` says whether the attachment itself is embedded in the PDF; it
    is always contained in the embedded ``.eml`` called ``eml_name``.
    """
    return _render(
        "info.html.j2",
        index=index,
        filename=filename,
        mime=mime,
        size=size,
        reason=reason,
        embedded=embedded,
        eml_name=eml_name,
    )


def render_text(
    headers: MailHeaders, text: str, *, show_bcc: bool, date_str: str
) -> str:
    """Render the plain-text version page, with the header block on top."""
    return _render(
        "text.html.j2",
        header_html=_header_fragment(headers, show_bcc=show_bcc, date_str=date_str),
        text=text,
    )


def render_section_separator(title: str) -> str:
    """Render a simple titled separator page (between the body renditions)."""
    return _render("section_separator.html.j2", title=title)


def render_text_attachment(filename: str, text: str) -> str:
    """Render a plain-text/markdown attachment as a page.

    The content is shown verbatim (pre-wrapped, monospace) under the filename —
    no Markdown interpretation, so the attachment is preserved, not reformatted.
    """
    return _render("text_attachment.html.j2", filename=filename, text=text)


# Two shaded bands, as Thunderbird prints a mail: subject, sender and date on a
# light grey, the recipients beneath on a darker one. Inline styles, because the
# block is inserted into arbitrary mail HTML whose own CSS must not reach it.
_HEADER_BANDS = ("#efefef", "#dedede")
_HEADER_STYLE = (
    "margin:0 0 1.2em 0;font-size:11pt;line-height:1.35;color:#1a1a1a;"
    "font-family:-apple-system,'Helvetica Neue',Helvetica,Arial,sans-serif"
)


def _header_fragment(headers: MailHeaders, *, show_bcc: bool, date_str: str) -> str:
    """Build the header block shown above every mail body.

    Subject, sender and date form the first band; To, CC and (if shown) BCC the
    second. Empty fields and an empty band are left out.
    """
    bands: tuple[list[tuple[str, str]], list[tuple[str, str]]] = (
        [("Betreff", headers.subject), ("Von", headers.from_), ("Datum", date_str)],
        [("An", headers.to), ("CC", headers.cc)],
    )
    if show_bcc:
        bands[1].append(("BCC", headers.bcc))

    blocks = []
    for colour, rows in zip(_HEADER_BANDS, bands, strict=True):
        lines = "".join(
            f'<div><span style="font-weight:700">{escape(label)}:</span> '
            f"{escape(value)}</div>"
            for label, value in rows
            if value
        )
        if lines:
            blocks.append(f'<div style="background:{colour}">{lines}</div>')
    return f'<div class="m2p-header" style="{_HEADER_STYLE}">{"".join(blocks)}</div>'


def prepend_header(
    mail_html: str, headers: MailHeaders, *, show_bcc: bool, date_str: str
) -> str:
    """Insert the metadata header block at the top of the mail body.

    Robust against arbitrary mail markup: the header uses inline styles so it
    never depends on — or collides with — the mail's own CSS.
    """
    fragment_html = _header_fragment(headers, show_bcc=show_bcc, date_str=date_str)
    try:
        tree = lxml_html.fromstring(mail_html)
    except (ParserError, ValueError):
        return f"<html><body>{fragment_html}</body></html>"

    body = tree.find(".//body")
    if body is None:
        body = tree
    header_el = lxml_html.fragment_fromstring(fragment_html)
    body.insert(0, header_el)
    return str(lxml_html.tostring(tree, encoding="unicode"))
