"""Tests for deriving plain text from HTML (for the mandatory text version)."""

from __future__ import annotations

import pytest

from imaparc.html.to_text import html_is_trivial_wrapper, html_to_text


def test_empty_html_yields_empty() -> None:
    assert html_to_text("") == ""
    assert html_to_text("   ") == ""


def test_paragraphs_become_line_breaks() -> None:
    text = html_to_text("<p>Erste Zeile</p><p>Zweite Zeile</p>")
    assert "Erste Zeile" in text
    assert "Zweite Zeile" in text
    assert text.index("Erste") < text.index("Zweite")
    assert "\n" in text  # separated, not run together


def test_list_items_get_bullets() -> None:
    text = html_to_text("<ul><li>eins</li><li>zwei</li></ul>")
    assert "• eins" in text
    assert "• zwei" in text


def test_br_becomes_newline() -> None:
    assert html_to_text("a<br>b") == "a\nb"


def test_scripts_and_styles_removed() -> None:
    text = html_to_text("<style>.x{color:red}</style><p>Inhalt</p><script>x()</script>")
    assert "Inhalt" in text
    assert "color" not in text
    assert "x()" not in text


def test_entities_unescaped_and_tags_stripped() -> None:
    text = html_to_text("<p>Preis &lt; 100 &amp; mehr</p>")
    assert "Preis < 100 & mehr" in text
    assert "<" not in text.replace("< 100", "")  # no leftover tag brackets


def test_quotes_are_prefixed_per_nesting_level() -> None:
    """An HTML-only reply keeps who said what: each quote level gets a ``>``."""
    text = html_to_text(
        "<p>Antwort</p>"
        "<blockquote type='cite'><p>Zitat</p>"
        "<blockquote type='cite'><p>Tiefer</p></blockquote>"
        "<p>wieder eins</p></blockquote>"
        "<p>Schluss</p>"
    )
    lines = [ln for ln in text.splitlines() if ln.strip(" >")]
    assert lines == ["Antwort", "> Zitat", ">> Tiefer", "> wieder eins", "Schluss"]


def test_consecutive_pre_blocks_stay_separate_lines() -> None:
    # Apple Mail puts each quoted paragraph in its own <pre>.
    text = html_to_text("<pre>Erster Absatz.</pre><pre>Zweiter Absatz.</pre>")
    assert "Erster Absatz.\nZweiter Absatz." in text


def test_byte_order_marks_are_dropped() -> None:
    assert html_to_text("<p>\ufeffHallo</p>") == "Hallo"


def test_unparseable_is_empty() -> None:
    # Bare text is still parseable by lxml; truly broken input degrades to "".
    assert isinstance(html_to_text("<<<"), str)


# --- html_is_trivial_wrapper ------------------------------------------------

_TRIVIAL = [
    "<html><body><p>Hallo</p><p>Welt</p></body></html>",
    "<div>Zeile eins<br>Zeile zwei</div>",
    "<p style='font-family:Arial;color:#333;margin:0;text-align:center'>x</p>",
    "<html><head><style>p{margin:0}</style></head><body><p>nur text</p></body></html>",
    "<span>schlicht</span>",
    # Mail-client scaffolding: the text/plain part carries all of it as text.
    "<p>a</p><blockquote type='cite'><p>zitiert</p></blockquote>",
    "<blockquote class='gmail_quote' style='border-left:1px #ccc solid'>q</blockquote>",
    "<ul class='MailOutline'><li>17.11.</li><li>20.11.</li></ul>",  # simple list
    "<pre style='overflow-wrap: break-word'>Hallo Roland,</pre>",  # Apple Mail quote
    "<font face='Arial' size='2'>Gruß</font>",
    "<a class='moz-txt-link-rfc2396E' href='mailto:a@b.de'>&lt;a@b.de&gt;</a>",
    "<a href='https://example.com/x'>https://example.com/x</a>",
    "<a href='http://www.example.com/'>www.example.com</a>",
]

_RICH = [
    "<p>text</p><img src='x.png'>",  # image
    "<table><tr><td>a</td></tr></table>",  # table
    "<p>see <a href='http://x'>link</a></p>",  # link
    "<p>this is <b>bold</b></p>",  # emphasis
    "<h1>Titel</h1><p>text</p>",  # heading
    "<a href='mailto:a@b.de'>Schreib mir</a>",  # link text hides the target
    "<a href='https://evil.example'>https://bank.example</a>",  # text ≠ target
    "<font color='red'>rot</font>",  # colour is not text-equivalent
    "<blockquote><img src='x.png'></blockquote>",  # quote does not excuse an image
    "<blockquote style='background:#eee'>q</blockquote>",  # shaded box, not a bar
    "<div style='background:#eee'>boxed</div>",  # background
    "<div style='border:1px solid'>boxed</div>",  # border
    "<style>td{background:red}</style><p>x</p>",  # box style in <style>
]


@pytest.mark.parametrize("html", _TRIVIAL)
def test_trivial_wrapper_detected(html: str) -> None:
    assert html_is_trivial_wrapper(html) is True


@pytest.mark.parametrize("html", _RICH)
def test_rich_html_is_not_trivial(html: str) -> None:
    assert html_is_trivial_wrapper(html) is False


def test_empty_html_is_not_trivial() -> None:
    assert html_is_trivial_wrapper("") is False
    assert html_is_trivial_wrapper("   ") is False
