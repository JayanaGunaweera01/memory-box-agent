"""Getting a photo of a keepsake ready to keep and to show a model.

A phone photo of a letter is often 4,000 px wide and carries the GPS position of the house it was
taken in. Before anything is stored, the photo is turned upright, scaled down so the long side is at
most `max_edge`, and re-encoded as a plain JPEG. Re-encoding from pixels drops every metadata block
(EXIF, GPS, XMP), so nothing about where or when the picture was taken is kept.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

QUALITY = 92  # high enough that faded handwriting survives


@dataclass(frozen=True)
class Prepared:
    jpeg: bytes
    width: int
    height: int


def prepare(data: bytes, max_edge: int) -> Prepared:
    with Image.open(io.BytesIO(data)) as original:
        upright = ImageOps.exif_transpose(original)
        rgb = upright.convert("RGB")
    scale = min(1.0, max_edge / max(rgb.size))
    if scale < 1.0:
        rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=QUALITY, optimize=True)  # no exif= argument: nothing is copied over
    return Prepared(out.getvalue(), rgb.width, rgb.height)


def has_metadata(jpeg: bytes) -> bool:
    """Whether a JPEG still carries EXIF or XMP. Used by the tests to prove `prepare` removed them."""
    with Image.open(io.BytesIO(jpeg)) as im:
        return bool(im.getexif()) or "xmp" in im.info or "exif" in im.info
