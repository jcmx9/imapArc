"""Tests for the HTML page renderers (pure model -> str, no browser)."""

from __future__ import annotations

from imaparc.html.render_html import (
    prepend_header,
    render_info,
    render_section_separator,
    render_separator,
    render_text,
)
from imaparc.humanize import format_file_size
from imaparc.mail.models import MailHeaders


def test_separator_contains_index_and_filename() -> None:
    html = render_separator(3, "invoice.pdf", 5, "1.2 MB")
    assert "Anhang 3" in html
    assert "invoice.pdf" in html
    assert "5 Seiten" in html
    assert "1.2 MB" in html


def test_separator_singular_page() -> None:
    html = render_separator(1, "one.pdf", 1, "10 KB")
    assert "1 Seite " in html
    assert "1 Seiten" not in html


def test_info_page_contains_reason_and_type() -> None:
    html = render_info(
        2, "contract.docx", "application/vnd...", "45 KB", "Dieser Anhang ..."
    )
    assert "Anhang 2" in html
    assert "contract.docx" in html
    assert "Dieser Anhang ..." in html
    assert "application/vnd..." in html
    assert "45 KB" in html
    assert "eingebettet" in html  # reassurance line


def test_separator_escapes_html_in_filename() -> None:
    html = render_separator(1, "<script>x</script>.pdf", 1, "1 KB")
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


def test_text_version_has_header_and_body() -> None:
    headers = MailHeaders(from_="a@x.com", to="b@y.com", subject="Hi")
    html = render_text(
        headers, "line one\nline two", show_bcc=True, date_str="2026-03-23"
    )
    assert "a@x.com" in html
    assert "Betreff:" in html
    assert "line one" in html


def test_base_css_is_injected_unescaped() -> None:
    # Regression: base.css is trusted static content and must be embedded raw.
    # Jinja autoescape would turn the quotes in `font-family: "SF Mono", …` into
    # `&#34;`, which is invalid inside <style> — Chromium then drops the whole
    # font-family and the plain-text page renders in the default serif, not
    # monospace. The quotes must survive intact.
    headers = MailHeaders(from_="a@x.com", subject="Hi")
    html = render_text(headers, "body", show_bcc=True, date_str="")
    assert '"SF Mono"' in html  # quoted font names intact
    assert "&#34;" not in html  # no HTML-escaped quotes leaked into the CSS


def test_text_version_bcc_hidden_when_disabled() -> None:
    headers = MailHeaders(from_="a@x.com", bcc="secret@z.com", subject="Hi")
    html = render_text(headers, "body", show_bcc=False, date_str="")
    assert "secret@z.com" not in html


def test_prepend_header_inserts_into_body() -> None:
    headers = MailHeaders(from_="a@x.com", subject="Test")
    mail = "<html><body><p>Original mail content</p></body></html>"
    result = prepend_header(mail, headers, show_bcc=True, date_str="2026-03-23")
    assert "a@x.com" in result
    assert "Original mail content" in result
    # Header must come before the original content.
    assert result.index("a@x.com") < result.index("Original mail content")


def test_prepend_header_survives_bodyless_html() -> None:
    headers = MailHeaders(from_="a@x.com", subject="Test")
    result = prepend_header("<p>loose</p>", headers, show_bcc=True, date_str="")
    assert "a@x.com" in result


def test_prepend_header_escapes_values() -> None:
    headers = MailHeaders(subject="<b>bold</b>")
    result = prepend_header(
        "<html><body></body></html>", headers, show_bcc=True, date_str=""
    )
    assert "<b>bold</b>" not in result
    assert "&lt;b&gt;bold" in result


def test_section_separator_shows_title() -> None:
    html = render_section_separator("Nur-Text-Version")
    assert "Nur-Text-Version" in html
    assert "Content-Security-Policy" in html


def test_format_file_size() -> None:
    assert format_file_size(512) == "512 B"
    assert format_file_size(1536) == "1.5 KB"
    assert format_file_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_file_size(3 * 1024**3) == "3.0 GB"
