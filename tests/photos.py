"""Test photos made in memory, so the tests need no image files."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

GPS_TAG = 0x8825


def keepsake(width: int = 900, height: int = 700) -> Image.Image:
    """A pale card with dark lines on it, roughly what a photographed letter looks like."""
    im = Image.new("RGB", (width, height), (232, 226, 210))
    draw = ImageDraw.Draw(im)
    for i, y in enumerate(range(height // 8, height - height // 8, max(6, height // 20))):
        draw.line((width // 10, y, width - width // 10 - (i * 37) % (width // 4), y), fill=(40, 45, 90), width=3)
    return im


def jpeg(im: Image.Image, *, gps: bool = False) -> bytes:
    """Encode `im`; with `gps`, attach a GPS block the way a phone camera would."""
    exif = Image.Exif()
    if gps:
        exif[0x010F] = "PhoneCam"  # camera make
        exif.get_ifd(GPS_TAG).update({1: "N", 2: (5.0, 56.0, 0.0), 3: "E", 4: (80.0, 32.0, 0.0)})
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, exif=exif if gps else Image.Exif())
    return out.getvalue()
