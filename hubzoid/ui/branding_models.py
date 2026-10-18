"""The ``branding.yaml`` manifest: one customer brand per deployment.

``branding.yaml`` (``version: 1``) lives in the deployment folder, with its
assets and optional ``theme.json`` under ``branding/``. It holds identity,
assets, sign-in and welcome copy, links, optional terms, an announcement and a
few presentation switches. Theme tokens use the shared chat theme schema
(``hubzoid.chat.theme``) unchanged. Appearance modes and the language are chat
configuration (``chat/config.yaml``), not branding.

Every field is plain data: copy is plain text, links are HTTPS or
deployment-relative, and the only styling hook is ``decorative_css``, a narrow
subset checked by ``branding_css``. Unknown keys are refused.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

VERSION = 1
FILE = "branding.yaml"
THEME_FILE = "branding/theme.json"
#: Bumped when validation rules change: a review binds to it.
VALIDATOR_VERSION = "branding-v1"

ASSET_KEYS = ("logo_light", "logo_dark", "wordmark", "favicon", "icon_192", "icon_512",
              "touch_icon", "splash_light", "splash_dark", "signin_background")
LOCALES = ("en",)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _plain(value: str, limit: int, what: str) -> str:
    if len(value) > limit:
        raise ValueError(f"{what} is at most {limit} characters")
    if "\x00" in value:
        raise ValueError(f"{what} can't contain control characters")
    return value


class Identity(_Strict):
    name: str = "Hubzoid"
    short_name: str = "Hubzoid"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name needs some text")
        return _plain(v, 80, "name")

    @field_validator("short_name")
    @classmethod
    def _short(cls, v: str) -> str:
        return _plain(v, 24, "short_name")

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return _plain(v, 240, "description")


class Assets(_Strict):
    logo_light: str | None = None
    logo_dark: str | None = None
    wordmark: str | None = None
    favicon: str | None = None
    icon_192: str | None = None
    icon_512: str | None = None
    touch_icon: str | None = None
    splash_light: str | None = None
    splash_dark: str | None = None
    signin_background: str | None = None

    @field_validator("*")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("branding/") or ".." in v.split("/") or "\\" in v:
            raise ValueError("asset paths are relative paths under branding/")
        return v


class Copy(_Strict):
    welcome_title: str | None = None
    welcome_body: str | None = None
    signin_title: str | None = None
    signin_body: str | None = None
    footer: str | None = None
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("welcome_title", "signin_title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        return None if v is None else _plain(v, 160, "titles")

    @field_validator("welcome_body", "signin_body", "footer")
    @classmethod
    def _body(cls, v: str | None) -> str | None:
        return None if v is None else _plain(v, 2000, "text")

    @field_validator("suggestions")
    @classmethod
    def _suggestions(cls, v: list[str]) -> list[str]:
        if len(v) > 12:
            raise ValueError("at most 12 suggestions")
        return [_plain(s, 500, "each suggestion") for s in v]


_URL = re.compile(r"^(https://[^\s<>\"']+|/[^\s<>\"']*)$")


class Links(_Strict):
    help: str | None = None
    privacy: str | None = None
    terms: str | None = None

    @field_validator("*")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if len(v) > 2000 or not _URL.match(v) or v.startswith("//"):
            raise ValueError("links are https:// URLs or paths starting with /")
        return v


class Legal(_Strict):
    version: str | None = None
    require_acceptance: bool = False

    @field_validator("version")
    @classmethod
    def _version(cls, v: str | None) -> str | None:
        return None if v is None else _plain(v, 80, "version")


class Announcement(_Strict):
    id: str
    text_by_locale: dict[Literal["en"], str]
    dismissible: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", v):
            raise ValueError("announcement id is 1 to 80 letters, digits, '.', '-' or '_'")
        return v

    @field_validator("text_by_locale")
    @classmethod
    def _text(cls, v: dict) -> dict:
        if "en" not in v or not v["en"].strip():
            raise ValueError("the announcement needs English text")
        return {k: _plain(t, 2000, "announcement text") for k, t in v.items()}


HIDEABLE = ("connections", "schedules", "console_link", "sharing", "files", "voice",
            "models", "spaces", "feedback")


class Presentation(_Strict):
    hidden_features: list[str] = Field(default_factory=list)
    response_watermark: str | None = None
    decorative_css: str | None = None

    @field_validator("hidden_features")
    @classmethod
    def _hidden(cls, v: list[str]) -> list[str]:
        for item in v:
            if item not in HIDEABLE:
                raise ValueError(f"hidden_features entries are among: {', '.join(HIDEABLE)}")
        return sorted(set(v))

    @field_validator("response_watermark")
    @classmethod
    def _wm(cls, v: str | None) -> str | None:
        return None if v is None else _plain(v, 240, "response_watermark")

    @field_validator("decorative_css")
    @classmethod
    def _css(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith("branding/") or not v.endswith(".css") or ".." in v.split("/"):
            raise ValueError("decorative_css is a .css path under branding/")
        return v


class BrandManifest(_Strict):
    version: Literal[1]
    identity: Identity = Field(default_factory=Identity)
    assets: Assets = Field(default_factory=Assets)
    copy_: dict[Literal["en"], Copy] = Field(default_factory=dict, alias="copy")
    links: Links = Field(default_factory=Links)
    legal: Legal = Field(default_factory=Legal)
    announcement: Announcement | None = None
    presentation: Presentation = Field(default_factory=Presentation)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _terms(self):
        if self.legal.require_acceptance and not (self.legal.version and self.links.terms):
            raise ValueError("require_acceptance needs legal.version and links.terms")
        return self


class BrandError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _pointer(loc) -> str:
    out = ""
    for part in loc:
        part = "copy" if part == "copy_" else part
        out += "/" + str(part).replace("~", "~0").replace("/", "~1")
    return out


def validate(configuration: Any) -> dict:
    """The manifest normalised (defaults filled). Raises ``BrandError``."""
    if not isinstance(configuration, dict):
        raise BrandError(422, "validation_failed", "branding.yaml must be a mapping.")
    if configuration.get("version") != VERSION:
        raise BrandError(422, "unsupported_schema", "branding.yaml needs \"version: 1\".",
                         pointer="/version")
    try:
        model = BrandManifest.model_validate(configuration)
    except ValidationError as exc:
        err = exc.errors()[0]
        pointer = _pointer(err.get("loc") or ())
        if err.get("type") == "extra_forbidden":
            raise BrandError(422, "unknown_key", f"Unknown key at {pointer}.", pointer=pointer)
        msg = err.get("msg", "invalid value").removeprefix("Value error, ")
        raise BrandError(422, "validation_failed", f"{pointer or '/'}: {msg}.", pointer=pointer)
    return model.model_dump(mode="json", by_alias=True)


def default() -> dict:
    return validate({"version": VERSION})


def referenced_paths(manifest: dict) -> dict[str, str]:
    """asset key (or ``decorative_css``) -> relative path, for set fields."""
    out = {k: v for k, v in (manifest.get("assets") or {}).items() if v}
    css = (manifest.get("presentation") or {}).get("decorative_css")
    if css:
        out["decorative_css"] = css
    return out
