"""Tests for page geometry — pure arithmetic, no browser needed."""

from __future__ import annotations

import pytest

from imaparc.render.geometry import (
    A4_WIDTH_MM,
    faithful_rendition,
    mm_to_css_px,
    reflowed_rendition,
)


def test_mm_to_css_px() -> None:
    # 25.4 mm == 1 inch == 96 CSS px
    assert mm_to_css_px(25.4) == pytest.approx(96.0)


def test_a4_width_in_css_px() -> None:
    assert mm_to_css_px(A4_WIDTH_MM) == pytest.approx(793.7, abs=0.5)


def test_faithful_rendition_is_fit_page() -> None:
    r = faithful_rendition()
    assert r.name == "faithful"
    assert r.fit_page is True
    assert r.scale == 1.0  # inert: scaling happens via the pikepdf overlay
    assert "Original" in r.title


def test_reflowed_rendition_does_not_scale() -> None:
    r = reflowed_rendition()
    assert r.name == "reflowed"
    assert r.scale == 1.0
    assert r.fit_page is False


def test_renditions_have_wider_left_margin() -> None:
    # Letter-style margins: left wider than the other three (filing edge).
    for r in (reflowed_rendition(), faithful_rendition()):
        assert r.left_mm > r.margin_mm
        assert r.margin_mm == 20.0
        assert r.left_mm == 25.0
