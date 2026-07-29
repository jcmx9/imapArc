"""Tests for HTML resource inlining and sanitisation."""

from __future__ import annotations

from imaparc.html.inline import inline_resources
from imaparc.mail.models import AttachmentPart

PNG = b"\x89PNG\r\n\x1a\n"


def _inline_part(cid: str | None = None, location: str | None = None) -> AttachmentPart:
    return AttachmentPart(
        filename="logo.png",
        content_type="image/png",
        content=PNG,
        content_id=cid,
        content_location=location,
        is_inline=True,
    )


def test_cid_resolved_to_data_uri() -> None:
    html = '<html><body><img src="cid:logo123"></body></html>'
    result = inline_resources(html, [_inline_part(cid="logo123")])
    assert "data:image/png;base64," in result.html
    assert "cid:" not in result.html
    assert result.unresolved_cids == []


def test_cid_with_percent_encoding() -> None:
    # cid:image001%2Ejpg@host must unquote to image001.jpg@host.
    html = '<html><body><img src="cid:image001%2Ejpg"></body></html>'
    result = inline_resources(html, [_inline_part(cid="image001.jpg")])
    assert "data:image/png;base64," in result.html


def test_unresolved_cid_gets_placeholder_and_is_recorded() -> None:
    html = '<html><body><img src="cid:missing"></body></html>'
    result = inline_resources(html, [])
    assert "cid:missing" in result.unresolved_cids
    assert "cid:" not in result.html.replace("cid:missing", "")  # attr rewritten
    assert "data:image/gif" in result.html  # placeholder pixel


def test_remote_image_blocked() -> None:
    html = '<html><body><img src="https://tracker.example.com/p.gif"></body></html>'
    result = inline_resources(html, [])
    assert "https://tracker.example.com/p.gif" in result.blocked_remote
    assert "tracker.example.com" not in result.html


def test_data_uri_left_untouched() -> None:
    html = '<html><body><img src="data:image/png;base64,AAAA"></body></html>'
    result = inline_resources(html, [])
    assert "data:image/png;base64,AAAA" in result.html
    assert result.blocked_remote == []


def test_script_removed() -> None:
    html = "<html><body><script>evil()</script><p>ok</p></body></html>"
    result = inline_resources(html, [])
    assert "evil" not in result.html
    assert "ok" in result.html


def test_iframe_removed() -> None:
    html = '<html><body><iframe src="https://x.com"></iframe><p>ok</p></body></html>'
    result = inline_resources(html, [])
    assert "iframe" not in result.html.lower()


def test_event_handlers_removed() -> None:
    html = '<html><body><p onclick="steal()">text</p></body></html>'
    result = inline_resources(html, [])
    assert "onclick" not in result.html
    assert "steal" not in result.html


def test_srcset_removed() -> None:
    html = (
        '<html><body><img src="data:," srcset="https://x.com/a.jpg 2x"></body></html>'
    )
    result = inline_resources(html, [])
    assert "srcset" not in result.html


def test_meta_refresh_removed() -> None:
    html = (
        '<html><head><meta http-equiv="refresh" content="0;url=https://x.com">'
        "</head><body></body></html>"
    )
    result = inline_resources(html, [])
    assert "refresh" not in result.html.lower()


def test_remote_stylesheet_removed() -> None:
    html = (
        '<html><head><link rel="stylesheet" href="https://x.com/s.css">'
        "</head><body></body></html>"
    )
    result = inline_resources(html, [])
    assert "x.com" not in result.html


def test_remote_preload_link_removed() -> None:
    html = (
        '<html><head><link rel="preload" as="image" '
        'href="https://tracker.example.com/p.png"></head><body></body></html>'
    )
    result = inline_resources(html, [])
    assert "tracker.example.com" not in result.html
    assert "https://tracker.example.com/p.png" in result.blocked_remote


def test_svg_image_xlink_href_remote_removed() -> None:
    html = (
        "<html><body><svg><image "
        'xlink:href="https://tracker.example.com/pixel.png"/></svg></body></html>'
    )
    result = inline_resources(html, [])
    assert "tracker.example.com" not in result.html
    assert "https://tracker.example.com/pixel.png" in result.blocked_remote


def test_anchor_href_is_left_untouched() -> None:
    # A hyperlink is not a resource load — it must survive.
    html = '<html><body><a href="https://example.com/page">link</a></body></html>'
    result = inline_resources(html, [])
    assert "https://example.com/page" in result.html
    assert result.blocked_remote == []


def test_css_bare_import_remote_removed() -> None:
    html = (
        '<html><head><style>@import "https://x.com/evil.css";</style>'
        "</head><body></body></html>"
    )
    result = inline_resources(html, [])
    assert "x.com" not in result.html
    assert "https://x.com/evil.css" in result.blocked_remote


def test_css_url_cid_resolved_in_style_attr() -> None:
    html = '<html><body><div style="background:url(cid:bg)"></div></body></html>'
    result = inline_resources(html, [_inline_part(cid="bg")])
    assert "data:image/png;base64," in result.html


def test_css_url_remote_removed_in_style_element() -> None:
    html = (
        "<html><head><style>.x{background:url(https://x.com/b.png)}</style>"
        "</head><body></body></html>"
    )
    result = inline_resources(html, [])
    assert "x.com" not in result.html
    assert "https://x.com/b.png" in result.blocked_remote


def test_content_location_resolved() -> None:
    html = '<html><body><img src="http://cid.local/logo.png"></body></html>'
    part = _inline_part(location="http://cid.local/logo.png")
    result = inline_resources(html, [part])
    assert "data:image/png;base64," in result.html
    assert result.blocked_remote == []


def test_background_attribute_rewritten() -> None:
    html = '<html><body background="cid:bg"></body></html>'
    result = inline_resources(html, [_inline_part(cid="bg")])
    assert "data:image/png;base64," in result.html


def test_allow_remote_keeps_url_and_records_it() -> None:
    html = '<html><body><img src="https://cdn.example.com/logo.png"></body></html>'
    result = inline_resources(html, [], allow_remote=True)
    assert "cdn.example.com/logo.png" in result.html
    assert "https://cdn.example.com/logo.png" in result.kept_remote
    assert result.blocked_remote == []


def test_allow_remote_keeps_css_url() -> None:
    html = (
        "<html><head><style>.x{background:url(https://cdn.example.com/b.png)}"
        "</style></head><body></body></html>"
    )
    result = inline_resources(html, [], allow_remote=True)
    assert "cdn.example.com" in result.html
    assert result.blocked_remote == []


def test_allow_remote_still_strips_scripts() -> None:
    # Permissive mode is about images, not about executing content.
    html = "<html><body><script>evil()</script><p>ok</p></body></html>"
    result = inline_resources(html, [], allow_remote=True)
    assert "evil" not in result.html


def test_default_is_blocking() -> None:
    html = '<html><body><img src="https://cdn.example.com/logo.png"></body></html>'
    result = inline_resources(html, [])
    assert result.allow_remote is False
    assert result.blocked_remote == ["https://cdn.example.com/logo.png"]
    assert result.kept_remote == []


def test_empty_html_yields_safe_document() -> None:
    result = inline_resources("", [])
    assert "body" in result.html
    assert result.unresolved_cids == []


# --- audit regressions (RFC 2392/2557 + srcset policy) ----------------------


def test_cid_is_case_sensitive() -> None:
    # Content-ID local part is case-sensitive (RFC 2392 / RFC 5322 msg-id): two
    # distinct IDs must not collapse onto the same part.
    parts = [
        AttachmentPart("a.png", "image/png", b"AAApng", content_id="ImageA"),
        AttachmentPart("b.gif", "image/gif", b"BBBgif", content_id="imagea"),
    ]
    result = inline_resources('<img src="cid:ImageA"><img src="cid:imagea">', parts)
    import re

    assert len(set(re.findall(r"data:[^\"]+", result.html))) == 2


def test_srcset_kept_when_remote_allowed() -> None:
    html = '<img src="data:image/gif;base64,R0lGODlh" srcset="https://cdn.x/a.jpg 2x">'
    result = inline_resources(html, [], allow_remote=True)
    assert "cdn.x/a.jpg" in result.html
    assert result.kept_remote == ["https://cdn.x/a.jpg"]


def test_srcset_blocked_and_recorded_in_strict_mode() -> None:
    result = inline_resources('<img srcset="https://cdn.x/a.jpg 2x">', [])
    assert "srcset" not in result.html
    assert result.blocked_remote == ["https://cdn.x/a.jpg"]


def test_srcset_cid_resolved() -> None:
    result = inline_resources('<img srcset="cid:logo 1x">', [_inline_part(cid="logo")])
    assert "data:image/png" in result.html


def test_content_location_relative_reference_resolves() -> None:
    part = _inline_part(location="image001.png")
    result = inline_resources('<img src="./image001.png">', [part])
    assert "data:image/png;base64," in result.html


def test_link_stylesheet_cid_resolved() -> None:
    css = AttachmentPart("s.css", "text/css", b"p{color:red}", content_id="styles")
    result = inline_resources('<link rel="stylesheet" href="cid:styles">', [css])
    assert "data:text/css" in result.html
    assert "cid:styles" not in result.html


def test_link_stylesheet_unresolvable_cid_dropped() -> None:
    result = inline_resources('<link rel="stylesheet" href="cid:missing">', [])
    assert "<link" not in result.html
