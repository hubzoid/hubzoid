"""Checking and normalising brand assets before anyone sees them.

Raster images (PNG, WebP, JPEG) are decoded with Pillow within fixed bounds
(4096 x 4096, 16 million pixels, no animation), then re-encoded as PNG or WebP,
which drops metadata and anything hidden after the image data. SVG is parsed
with defusedxml and must use only static drawing elements: no scripts, event
attributes, external references, ``foreignObject``, ``<style>``, ``style``
attributes or animation. An unsafe SVG is refused, never quietly rewritten.

Installation icons (192, 512 and the 180 px touch icon) that the brand does not
supply are generated, as exact-size PNGs, from a raster favicon or logo.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from .branding_models import BrandError

MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 64
MAX_SIDE = 4096
MAX_PIXELS = 16_000_000

# Which kinds each asset slot accepts.
SLOT_KINDS = {
    "logo_light": ("svg", "raster"), "logo_dark": ("svg", "raster"), "wordmark": ("svg", "raster"),
    "favicon": ("svg", "raster"), "icon_192": ("raster",), "icon_512": ("raster",),
    "touch_icon": ("raster",), "splash_light": ("raster",), "splash_dark": ("raster",),
    "signin_background": ("raster",),
}
ICON_SIZES = {"icon_192": 192, "icon_512": 512, "touch_icon": 180}

_SVG_ELEMENTS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "defs",
    "lineargradient", "radialgradient", "stop", "title", "desc", "clippath",
}
_SVG_ATTRS = {
    "id", "class", "d", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width",
    "height", "viewbox", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-opacity", "opacity",
    "transform", "points", "offset", "stop-color", "stop-opacity", "gradientunits",
    "gradienttransform", "clip-path", "clip-rule", "clippathunits", "preserveaspectratio",
    "version", "xmlns", "role", "aria-label", "aria-hidden", "focusable", "fx", "fy",
    "spreadmethod", "stroke-dasharray", "stroke-dashoffset", "shape-rendering", "vector-effect",
}
_LOCAL_URL = re.compile(r"url\(\s*#[A-Za-z0-9_.:-]+\s*\)")


@dataclass
class Checked:
    data: bytes
    media_type: str
    width: int | None
    height: int | None
    extension: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def check_svg(data: bytes, where: str) -> Checked:
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException

    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (DefusedXmlException, SafeET.ParseError, ValueError) as exc:
        raise BrandError(422, "validation_failed", f"{where} is not a plain SVG file ({type(exc).__name__}).",
                         pointer=where)
    if _local(root.tag) != "svg":
        raise BrandError(422, "validation_failed", f"{where} is not an SVG image.", pointer=where)
    for el in root.iter():
        name = _local(el.tag)
        if name not in _SVG_ELEMENTS:
            raise BrandError(422, "unsafe_asset",
                             f"{where} uses <{name}>, which brand SVGs can't contain. Export a plain "
                             "SVG with paths and shapes only.", pointer=where)
        for attr, value in el.attrib.items():
            local = _local(attr)
            if local.startswith("on"):
                raise BrandError(422, "unsafe_asset", f"{where} has an event attribute ({local}).",
                                 pointer=where)
            if local in ("href", "style", "src"):
                raise BrandError(422, "unsafe_asset",
                                 f"{where} uses {local}=, which brand SVGs can't contain.", pointer=where)
            if local not in _SVG_ATTRS and not attr.startswith("{http://www.w3.org/2000/xmlns"):
                raise BrandError(422, "unsafe_asset", f"{where} uses the attribute {local!r}.",
                                 pointer=where)
            if "url(" in value and not _LOCAL_URL.fullmatch(value.strip()):
                raise BrandError(422, "unsafe_asset", f"{where} refers to something outside the file.",
                                 pointer=where)
            if re.search(r"javascript:|data:|https?:", value, re.I):
                raise BrandError(422, "unsafe_asset", f"{where} refers to something outside the file.",
                                 pointer=where)
    return Checked(data, "image/svg+xml", None, None, "svg")


def check_raster(data: bytes, where: str, *, exact: int | None = None) -> Checked:
    from PIL import Image, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ("PNG", "WEBP", "JPEG"):
                raise BrandError(422, "validation_failed",
                                 f"{where} must be PNG, WebP or JPEG (it is {fmt or 'unknown'}).", pointer=where)
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                raise BrandError(422, "validation_failed", f"{where} is animated. Use a still image.",
                                 pointer=where)
            width, height = img.size
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise BrandError(422, "validation_failed",
                                 f"{where} is {width}x{height}. Keep images within 4096x4096.", pointer=where)
            if exact and (width, height) != (exact, exact):
                raise BrandError(422, "validation_failed",
                                 f"{where} must be exactly {exact}x{exact} pixels (it is {width}x{height}).",
                                 pointer=where)
            img.load()
            out = io.BytesIO()
            if fmt == "WEBP":
                img.save(out, format="WEBP", lossless=True)
                media, ext = "image/webp", "webp"
            else:
                rgba = img.convert("RGBA") if img.mode not in ("RGB", "RGBA", "L", "LA") else img
                rgba.save(out, format="PNG", optimize=True)
                media, ext = "image/png", "png"
    except BrandError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise BrandError(422, "validation_failed", f"{where} can't be read as an image ({type(exc).__name__}).",
                         pointer=where)
    return Checked(out.getvalue(), media, width, height, ext)


def check(slot: str, data: bytes, where: str) -> Checked:
    """Check one asset for its slot (an ``ASSET_KEYS`` name)."""
    if len(data) > MAX_ASSET_BYTES:
        raise BrandError(413, "asset_too_large", f"{where} is larger than 5 MiB.", pointer=where)
    kinds = SLOT_KINDS[slot]
    looks_svg = data.lstrip()[:5].lower() in (b"<?xml", b"<svg ") or b"<svg" in data[:512].lower()
    if looks_svg:
        if "svg" not in kinds:
            raise BrandError(422, "validation_failed", f"{where} must be a PNG, WebP or JPEG image.",
                             pointer=where)
        return check_svg(data, where)
    if "raster" not in kinds:
        raise BrandError(422, "validation_failed", f"{where} must be an SVG.", pointer=where)
    return check_raster(data, where, exact=ICON_SIZES.get(slot))


def generate_icon(master: bytes, size: int) -> bytes:
    """An exact-size PNG icon from a raster master, centred on transparency."""
    from PIL import Image

    with Image.open(io.BytesIO(master)) as img:
        img = img.convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
