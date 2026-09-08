"""Honest StealAds / Matt-Emerald intake-link boundary (FM-032).

Configured intake URLs open as ordinary links with no payload, query, or
fragment customer data. This module never contacts an external intake, never
submits a handoff, and never claims an automatic integration.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping
from urllib.parse import unquote, urlsplit

from found_money.contracts.activation import HandoffIntakeConfigV1

HandoffKind = Literal["stealads", "matt_emerald"]

STEALADS_CONFIGURED_LABEL = "Build in StealAds"
MATT_EMERALD_CONFIGURED_LABEL = "Have Matt/Emerald Build This"
MATT_EMERALD_UNCONFIGURED_LABEL = "Export production brief"

STEALADS_MANUAL_INSTRUCTIONS = (
    "Manually import this approval-only creative handoff into StealAds. "
    "No automatic integration or audience creation is performed."
)
MATT_EMERALD_EXPORT_INSTRUCTIONS = (
    "Export this approval-only production brief for Matt/Emerald. "
    "No automatic integration, finished video script, or media-buying plan is included."
)

_UNSAFE_SCHEMES = frozenset(
    {
        "javascript",
        "data",
        "file",
        "ftp",
        "blob",
        "about",
        "mailto",
        "tel",
        "ws",
        "wss",
        "http",
    }
)
_IDENTITY_PATH_RE = re.compile(
    r"(?i)(?:@|[?&#=]|cus_|hs_contact_|inv_|src_|email|phone|customer|"
    r"contact_id|source_id|record_id|customer_token)"
)
_PHONE_PAYLOAD_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9a-fA-F])"
    r"|\+\d{8,15}(?!\d)"
)
_SECRET_PAYLOAD_RE = re.compile(
    r"(?i)(?:\b(?:sk_live_|sk_test_|rk_live_|rk_test_|ghp_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|credentials|bearer|client[_-]?secret|token)\b)"
)
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_AUTO_CLAIM_RE = re.compile(
    r"(?i)\b(?:automatic(?:ally)?\s+integrat(?:e|ion|es|ed)|live\s+integration|"
    r"api\s+sync|auto[- ](?:submit|send|import|sync))\b"
)
_NEGATION_PREFIX_RE = re.compile(r"(?i)\b(?:no|not|never|without)\s+$")
_INVALID_PCT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CREATIVE_HREF_RE = re.compile(r"^launch-pack/creative-handoff/[A-Za-z0-9][A-Za-z0-9_-]*\.json$")
_BRIEF_HREF_RE = re.compile(r"^launch-pack/production-brief/[A-Za-z0-9][A-Za-z0-9_-]*\.json$")
_MARKED_INTAKE_RE = re.compile(r"data-handoff-intake", re.IGNORECASE)
_INTAKE_ANCHOR_IDS = frozenset({"stealads-intake", "matt-emerald-intake"})
_REQUIRED_INTAKE_ATTRS = frozenset({"id", "href", "data-handoff-intake", "data-integration"})
_MAX_DECODE_ROUNDS = 8
_VALIDATED_HREF_PLACEHOLDER = "validated-intake"


class IntakeConfigError(ValueError):
    """Fail-closed intake URL or handoff configuration error."""


def _claims_automatic_integration(text: str) -> bool:
    for match in _AUTO_CLAIM_RE.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if _NEGATION_PREFIX_RE.search(prefix):
            continue
        return True
    return False


def _fully_unquote(text: str, *, field_name: str) -> str:
    current = text
    for _ in range(_MAX_DECODE_ROUNDS):
        if _INVALID_PCT_RE.search(current):
            raise IntakeConfigError(f"{field_name} contains invalid percent encoding")
        decoded = unquote(current, encoding="utf-8", errors="strict")
        if decoded == current:
            if "%" in current:
                raise IntakeConfigError(f"{field_name} contains invalid percent encoding")
            return decoded
        current = decoded
    raise IntakeConfigError(f"{field_name} percent encoding is not canonical")


def _valid_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    if host.startswith("[") or host.endswith("]"):
        return False
    if _IPV4_RE.fullmatch(host):
        return all(part.isdigit() and 0 <= int(part) <= 255 for part in host.split("."))
    if ".." in host or host.startswith(".") or host.endswith("."):
        return False
    labels = host.split(".")
    return all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


def _normalized_path_is_safe(path: str) -> bool:
    if not path:
        return True
    normalized = path.replace("\\", "/")
    if _TRAVERSAL_RE.search(normalized) or "//" in normalized:
        return False
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    return ".." not in parts


def _reject_intake_payloads(text: str, *, field_name: str) -> None:
    if (
        _IDENTITY_PATH_RE.search(text)
        or _PHONE_PAYLOAD_RE.search(text)
        or _SECRET_PAYLOAD_RE.search(text)
    ):
        raise IntakeConfigError(
            f"{field_name} must not embed identity, phone, or credential payloads"
        )


def validate_intake_url(url: str, *, field_name: str = "intake_url") -> str:
    """Accept https origins or relative local fixture paths with no payload embedding."""
    if not isinstance(url, str):
        raise IntakeConfigError(f"{field_name} must be a string")
    text = url.strip()
    if not text:
        raise IntakeConfigError(f"{field_name} must be non-empty when configured")
    if any(ch.isspace() for ch in text):
        raise IntakeConfigError(f"{field_name} must not contain whitespace")
    decoded = _fully_unquote(text, field_name=field_name)
    _reject_intake_payloads(text, field_name=field_name)
    _reject_intake_payloads(decoded, field_name=field_name)

    parsed = urlsplit(decoded)
    if parsed.scheme:
        scheme = parsed.scheme.casefold()
        if scheme in _UNSAFE_SCHEMES:
            raise IntakeConfigError(f"{field_name} scheme is unsafe")
        if scheme != "https":
            raise IntakeConfigError(f"{field_name} must use https or a relative local path")
        if parsed.username is not None or parsed.password is not None:
            raise IntakeConfigError(f"{field_name} must not include credentials")
        if "@" in parsed.netloc or parsed.netloc.endswith(":"):
            raise IntakeConfigError(f"{field_name} must include a host")
        host = parsed.hostname
        if not host or not _valid_hostname(host):
            raise IntakeConfigError(f"{field_name} hostname is invalid")
        try:
            port = parsed.port
        except ValueError as exc:
            raise IntakeConfigError(f"{field_name} port is invalid") from exc
        if port is not None and not (1 <= port <= 65535):
            raise IntakeConfigError(f"{field_name} port is invalid")
        if parsed.query or parsed.fragment:
            raise IntakeConfigError(f"{field_name} must not include query or fragment")
        if not _normalized_path_is_safe(parsed.path):
            raise IntakeConfigError(f"{field_name} path must be traversal-free")
        if text != decoded and "%" in text:
            # Re-check the original encoded form did not hide separators.
            original_parsed = urlsplit(text)
            if original_parsed.query or original_parsed.fragment:
                raise IntakeConfigError(f"{field_name} must not include query or fragment")
        return decoded if decoded.startswith("https://") else text

    if decoded != text and "%" in text:
        raise IntakeConfigError(f"{field_name} relative path is invalid")
    relative = decoded.replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    if (
        not relative
        or relative.startswith("/")
        or relative.startswith("~/")
        or relative.endswith("/")
        or "//" in relative
        or "?" in relative
        or "#" in relative
        or "%" in relative
        or ":" in relative
        or _WINDOWS_ABS_RE.match(relative)
        or relative == ".."
        or not _normalized_path_is_safe(relative)
    ):
        raise IntakeConfigError(f"{field_name} relative path is invalid")
    return relative


def validate_export_href(
    href: str | None,
    *,
    kind: Literal["creative_handoff", "production_brief"],
) -> str | None:
    """Accept only caller-root-bounded relative launch-pack JSON artifact paths."""
    if href is None:
        return None
    if not isinstance(href, str):
        raise IntakeConfigError(f"{kind} href must be a string")
    text = href.strip()
    if not text or any(ch.isspace() for ch in text) or text != href.strip():
        raise IntakeConfigError(f"{kind} href is invalid")
    if "%" in text or "?" in text or "#" in text or "\\" in text or "://" in text:
        raise IntakeConfigError(f"{kind} href is invalid")
    relative = text.replace("\\", "/")
    if relative.startswith("./"):
        relative = relative[2:]
    pattern = _CREATIVE_HREF_RE if kind == "creative_handoff" else _BRIEF_HREF_RE
    candidate = PurePosixPath(relative)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or not pattern.fullmatch(relative)
        or _WINDOWS_ABS_RE.match(relative)
    ):
        raise IntakeConfigError(
            f"{kind} href must be a caller-root-bounded relative launch-pack JSON path"
        )
    return relative


def project_handoff_intake(config: HandoffIntakeConfigV1) -> dict[str, Any]:
    """Manifest-safe configured/hash/status projection with no raw URLs."""

    def _entry(url: str | None) -> dict[str, Any]:
        if url is None:
            return {"configured": False, "status": "unconfigured"}
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return {"configured": True, "status": "configured", "url_sha256": digest}

    return {
        "schema_version": "handoff-intake-config.v1",
        "stealads": _entry(config.stealads_intake_url),
        "matt_emerald": _entry(config.matt_emerald_intake_url),
    }


def parse_handoff_intake_config(payload: Mapping[str, Any] | None) -> HandoffIntakeConfigV1:
    """Parse optional intake config and validate any configured URLs."""
    if payload is None:
        return HandoffIntakeConfigV1()
    if not isinstance(payload, Mapping):
        raise IntakeConfigError("handoff intake config must be an object")
    raw = dict(payload)
    if (
        "handoff_intake" in raw
        and isinstance(raw["handoff_intake"], Mapping)
        and {
            "stealads_intake_url",
            "matt_emerald_intake_url",
            "schema_version",
        }.isdisjoint(raw)
    ):
        raw = dict(raw["handoff_intake"])
    config = HandoffIntakeConfigV1.model_validate(raw)
    stealads = (
        None
        if config.stealads_intake_url is None
        else validate_intake_url(config.stealads_intake_url, field_name="stealads_intake_url")
    )
    matt = (
        None
        if config.matt_emerald_intake_url is None
        else validate_intake_url(
            config.matt_emerald_intake_url, field_name="matt_emerald_intake_url"
        )
    )
    return HandoffIntakeConfigV1(stealads_intake_url=stealads, matt_emerald_intake_url=matt)


def assert_honest_handoff_copy(text: str, *, label: str) -> None:
    if _claims_automatic_integration(text):
        raise IntakeConfigError(f"{label} claims automatic integration")


@dataclass(frozen=True)
class HandoffActionState:
    kind: HandoffKind
    configured: bool
    label: str
    intake_url: str | None
    export_href: str | None
    instructions: str

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "configured": self.configured,
            "label": self.label,
            "intake_url": self.intake_url,
            "export_href": self.export_href,
            "instructions": self.instructions,
            "live_integration": False,
            "automatic_integration": False,
        }


@dataclass(frozen=True)
class HandoffActions:
    stealads: HandoffActionState
    matt_emerald: HandoffActionState

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "handoff-actions.v1",
            "stealads": self.stealads.canonical_dict(),
            "matt_emerald": self.matt_emerald.canonical_dict(),
        }


def build_handoff_actions(
    *,
    intake: HandoffIntakeConfigV1 | Mapping[str, Any] | None = None,
    creative_handoff_href: str | None,
    production_brief_href: str | None,
) -> HandoffActions:
    """Build configured/unconfigured StealAds and Matt/Emerald action states."""
    if isinstance(intake, HandoffIntakeConfigV1):
        config = parse_handoff_intake_config(intake.canonical_dict())
    else:
        config = parse_handoff_intake_config(intake)
    stealads_url = config.stealads_intake_url
    matt_url = config.matt_emerald_intake_url
    creative_href = validate_export_href(creative_handoff_href, kind="creative_handoff")
    brief_href = validate_export_href(production_brief_href, kind="production_brief")

    stealads = HandoffActionState(
        kind="stealads",
        configured=stealads_url is not None,
        label=STEALADS_CONFIGURED_LABEL,
        intake_url=stealads_url,
        export_href=creative_href,
        instructions=STEALADS_MANUAL_INSTRUCTIONS,
    )
    if matt_url is None:
        matt = HandoffActionState(
            kind="matt_emerald",
            configured=False,
            label=MATT_EMERALD_UNCONFIGURED_LABEL,
            intake_url=None,
            export_href=brief_href,
            instructions=MATT_EMERALD_EXPORT_INSTRUCTIONS,
        )
    else:
        matt = HandoffActionState(
            kind="matt_emerald",
            configured=True,
            label=MATT_EMERALD_CONFIGURED_LABEL,
            intake_url=matt_url,
            export_href=brief_href,
            instructions=MATT_EMERALD_EXPORT_INSTRUCTIONS,
        )
    assert_honest_handoff_copy(stealads.instructions, label="stealads instructions")
    assert_honest_handoff_copy(matt.instructions, label="matt/emerald instructions")
    assert_honest_handoff_copy(stealads.label, label="stealads label")
    assert_honest_handoff_copy(matt.label, label="matt/emerald label")
    return HandoffActions(stealads=stealads, matt_emerald=matt)


def coerce_handoff_actions(value: Any | None) -> HandoffActions:
    """Rebuild and revalidate any supplied handoff-action object."""
    if value is None:
        return build_handoff_actions(
            intake=None,
            creative_handoff_href=None,
            production_brief_href=None,
        )
    stealads = getattr(value, "stealads", None)
    matt = getattr(value, "matt_emerald", None)
    if stealads is None or matt is None:
        raise IntakeConfigError("handoff actions are malformed")
    return build_handoff_actions(
        intake={
            "stealads_intake_url": getattr(stealads, "intake_url", None),
            "matt_emerald_intake_url": getattr(matt, "intake_url", None),
        },
        creative_handoff_href=getattr(stealads, "export_href", None),
        production_brief_href=getattr(matt, "export_href", None),
    )


class _MarkedIntakeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.errors: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map: dict[str, str] = {}
        for key, raw in attrs:
            folded = key.casefold()
            if folded in attr_map:
                self.errors.append("duplicate attribute")
            attr_map[folded] = "" if raw is None else raw
        if "data-handoff-intake" in attr_map:
            if tag.casefold() != "a":
                self.errors.append("marked non-anchor")
                return
            self._current = {"attrs": attr_map, "text": []}
            self._validate_anchor(attr_map)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._current is None:
            return
        self.anchors.append(
            {
                **self._current["attrs"],
                "#text": "".join(self._current["text"]),
            }
        )
        self._current = None

    def _validate_anchor(self, attr_map: dict[str, str]) -> None:
        names = set(attr_map)
        extra = names - _REQUIRED_INTAKE_ATTRS
        missing = _REQUIRED_INTAKE_ATTRS - names
        if extra:
            self.errors.append("unexpected intake attributes")
        if missing:
            self.errors.append("missing required intake attributes")
        if attr_map.get("data-handoff-intake") != "true":
            self.errors.append("intake marker must be true")
        if attr_map.get("data-integration") != "manual":
            self.errors.append("intake integration marker must be manual")
        if attr_map.get("id") not in _INTAKE_ANCHOR_IDS:
            self.errors.append("intake id is unknown")


def html_for_public_scan(html: str) -> str:
    """Parse marked intake anchors fail-closed and redact only validated hrefs."""
    if not isinstance(html, str):
        raise IntakeConfigError("html must be a string")
    marker_count = len(_MARKED_INTAKE_RE.findall(html))
    parser = _MarkedIntakeParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise IntakeConfigError("handoff intake markup is malformed") from exc
    if parser.errors:
        raise IntakeConfigError(parser.errors[0])
    if marker_count != len(parser.anchors):
        raise IntakeConfigError("handoff intake markup is spoofed or malformed")
    seen_ids: set[str] = set()
    redacted = html
    for anchor in parser.anchors:
        anchor_id = anchor.get("id", "")
        if anchor_id in seen_ids:
            raise IntakeConfigError("duplicate handoff intake control")
        seen_ids.add(anchor_id)
        href = anchor.get("href", "")
        if "?" in href or "#" in href:
            raise IntakeConfigError("intake href must not include query or fragment")
        validated = validate_intake_url(href, field_name="intake href")
        pattern = re.compile(
            r'(<a\b[^>]*\bhref\s*=\s*)([\'"])' + re.escape(href) + r"\2",
            re.IGNORECASE,
        )
        redacted, count = pattern.subn(
            r"\1\2" + _VALIDATED_HREF_PLACEHOLDER + r"\2",
            redacted,
            count=1,
        )
        if count != 1:
            raise IntakeConfigError("handoff intake href could not be isolated")
        if validated != href and href not in html:
            raise IntakeConfigError("handoff intake href is malformed")
    return redacted


def local_intake_fixture_html(*, title: str) -> str:
    """Safe local intake landing page used only for browser proof."""
    safe_title = title.strip() or "Manual intake fixture"
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n<title>{safe_title}</title>\n'
        "</head>\n<body>\n"
        f"<h1>{safe_title}</h1>\n"
        "<p>Manual intake fixture. No automatic integration and no payload submitted.</p>\n"
        "</body>\n</html>\n"
    )
