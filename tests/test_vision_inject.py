"""Unit tests for hubzoid.vision_inject — native image injection.

Covers the security boundary (only real images inside the uploads dir are ever
read; .env / path escapes are refused), reference parsing, the max_images cap,
and the per-runtime block shapes.
"""
from __future__ import annotations

import base64
from io import BytesIO

import pytest

from hubzoid import memory as memlib
from hubzoid import uploads as uploads_lib
from hubzoid import vision_inject


def _png_bytes(word: str = "hi") -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (40, 20), "white")
    ImageDraw.Draw(img).text((2, 2), word, fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def hub(tmp_path):
    """A hub dir with one uploaded image and one secret .env in the uploads dir."""
    chat_id = "c1"
    up = memlib.chat_upload_dir(tmp_path, chat_id)
    uploads_lib.write_with_meta(up, "creative.png", _png_bytes(), mime="image/png")
    # A secret that must NEVER be injectable, even if referenced.
    uploads_lib.write_with_meta(up, "secret.env", b"DD_APP_KEY=nope", mime="text/plain")
    return tmp_path, chat_id


def test_image_names_parses_distinct_refs():
    p = "look at [Image: a.png] and [Image: b.png] and again [Image: a.png]"
    assert vision_inject.image_names(p) == ["a.png", "b.png"]


def test_no_reference_returns_plain_string(hub):
    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "hello, no image here", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    assert out == "hello, no image here"


def test_disabled_returns_plain_string(hub):
    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "[Image: creative.png]", hub_dir, chat_id,
        enabled=False, max_edge=1568, max_images=4,
    )
    assert out == "[Image: creative.png]"


def test_claude_prompt_injects_image_block(hub):
    import anyio

    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "review [Image: creative.png]", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    # An async iterable, not a string.
    assert not isinstance(out, str)

    async def _collect():
        items = []
        async for m in out:
            items.append(m)
        return items

    msgs = anyio.run(_collect)
    assert len(msgs) == 1
    content = msgs[0]["message"]["content"]
    assert content[0]["type"] == "text"
    imgs = [b for b in content if b["type"] == "image"]
    assert len(imgs) == 1
    assert imgs[0]["source"]["media_type"] == "image/png"
    # Round-trips as valid base64.
    base64.b64decode(imgs[0]["source"]["data"], validate=True)


def test_openai_input_shape(hub):
    hub_dir, chat_id = hub
    out = vision_inject.openai_input(
        "review [Image: creative.png]", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    assert isinstance(out, list) and len(out) == 1
    content = out[0]["content"]
    assert content[0]["type"] == "input_text"
    imgs = [b for b in content if b["type"] == "input_image"]
    assert len(imgs) == 1
    assert imgs[0]["image_url"].startswith("data:image/png;base64,")


def test_env_secret_is_never_injected(hub):
    """A reference to a non-image (the .env) must resolve to no image block."""
    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "[Image: secret.env]", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    # Not an image -> nothing injected -> plain string passthrough.
    assert out == "[Image: secret.env]"


def test_path_escape_is_refused(hub):
    """A traversal ref must not resolve outside the uploads dir."""
    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "[Image: ../../secret.env]", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    assert out == "[Image: ../../secret.env]"


def test_missing_file_is_skipped(hub):
    hub_dir, chat_id = hub
    out = vision_inject.claude_prompt(
        "[Image: nope.png]", hub_dir, chat_id,
        enabled=True, max_edge=1568, max_images=4,
    )
    assert out == "[Image: nope.png]"
