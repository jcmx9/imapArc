"""Convert an image attachment into A4 PDF pages, losslessly where possible."""

from __future__ import annotations

import io

import img2pdf
import pillow_heif
from PIL import Image, ImageOps

from imaparc.exceptions import ImapArcError

# Let Pillow open HEIC/HEIF (and AVIF); img2pdf cannot decode these itself, so
# they always go through the preflight path below.
pillow_heif.register_heif_opener()

# Decompression-bomb guard: a tiny file can decode into a huge pixel array in
# the Pillow preflight path (img2pdf.convert never decodes pixels itself).
# Pillow raises DecompressionBombError above 2x this cap; we treat that as an
# unconvertible attachment rather than letting it exhaust memory.
Image.MAX_IMAGE_PIXELS = 128_000_000

_A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
# Keep a minimum print margin so an image never bleeds to the sheet edge —
# printers cannot render the outermost few mm, and it matches the archival,
# meant-to-be-printed look of the other pages. The image is scaled to fit inside
# this bordered area (aspect preserved).
_IMAGE_MARGIN_MM = 10.0
_BORDER = (img2pdf.mm_to_pt(_IMAGE_MARGIN_MM), img2pdf.mm_to_pt(_IMAGE_MARGIN_MM))
_LAYOUT = img2pdf.get_layout_fun(
    _A4, border=_BORDER, fit=img2pdf.FitMode.into, auto_orient=True
)

# img2pdf failures that mean "these raw bytes need a Pillow preflight first".
_PREFLIGHT_TRIGGERS = (
    img2pdf.AlphaChannelError,
    img2pdf.UnsupportedColorspaceError,
    img2pdf.JpegColorspaceError,
    img2pdf.ImageOpenError,
    img2pdf.ExifOrientationError,
)


class ImageConversionError(ImapArcError):
    """An image attachment could not be converted to PDF."""


def image_to_pdf(data: bytes) -> bytes:
    """Return an A4 PDF (bytes) rendering the image.

    JPEG/PNG without an alpha channel are embedded losslessly by img2pdf.
    Images img2pdf rejects (alpha, 16-bit, HEIC, exotic colorspaces) are
    flattened to an 8-bit RGB JPEG via Pillow first — a lossy but necessary
    transcode. Portrait/landscape follows the image aspect ratio.

    Raises:
        ImageConversionError: If the bytes are not a decodable image.
    """
    if not data:
        raise ImageConversionError("empty image")
    try:
        return bytes(
            img2pdf.convert(data, layout_fun=_LAYOUT, rotation=img2pdf.Rotation.ifvalid)
        )
    except _PREFLIGHT_TRIGGERS:
        prepared = _preflight(data)
        try:
            return bytes(img2pdf.convert(prepared, layout_fun=_LAYOUT))
        except _PREFLIGHT_TRIGGERS as exc:
            raise ImageConversionError(str(exc)) from exc


def _preflight(data: bytes) -> bytes:
    """Flatten to an 8-bit RGB JPEG, baking in EXIF rotation, removing alpha."""
    try:
        opened = Image.open(io.BytesIO(data))
        opened.load()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageConversionError(str(exc)) from exc
    img: Image.Image = ImageOps.exif_transpose(opened) or opened
    img = _flatten(img)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _flatten(img: Image.Image) -> Image.Image:
    """Composite any transparency onto white and reduce to 8-bit RGB."""
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img
