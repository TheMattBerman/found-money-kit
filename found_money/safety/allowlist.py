"""Centralized explicit network allowlist for HubSpot/Stripe reads and model requests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

NetworkFamily = Literal["hubspot_read", "stripe_read", "model_request"]

ALLOWLIST_VERSION = "found-money-network-allowlist.v1"

HUBSPOT_HOST = "api.hubapi.com"
STRIPE_HOST = "api.stripe.com"
MODEL_HOST = "api.openai.com"

HUBSPOT_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/crm/v3/objects/(?:contacts|deals)$"),
    re.compile(r"^/crm/v3/properties/(?:contacts|deals)$"),
    re.compile(r"^/crm/v4/objects/deals/[A-Za-z0-9_-]+/associations/contacts$"),
    re.compile(r"^/crm/v4/objects/contacts/[A-Za-z0-9_-]+/associations/deals$"),
)
STRIPE_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^/v1/(?:customers|subscriptions|invoices|payment_intents|charges|refunds|products|prices)$"
    ),
    re.compile(r"^/v1/invoices/[A-Za-z0-9_-]+/lines$"),
)
MODEL_PATH_RES: tuple[re.Pattern[str], ...] = (re.compile(r"^/v1/responses$"),)

_DISALLOWED_PATH_PREFIXES = (
    "/audiences",
    "/marketing/",
    "/crm/v3/objects/contacts/batch",
    "/crm/v3/objects/deals/batch",
    "/v1/subscription_items",
    "/v1/files",
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class NetworkAllowlistRule:
    family: NetworkFamily
    method: Literal["GET", "POST"]
    host: str
    path_patterns: tuple[re.Pattern[str], ...]


NETWORK_ALLOWLIST_RULES: tuple[NetworkAllowlistRule, ...] = (
    NetworkAllowlistRule("hubspot_read", "GET", HUBSPOT_HOST, HUBSPOT_PATH_RES),
    NetworkAllowlistRule("stripe_read", "GET", STRIPE_HOST, STRIPE_PATH_RES),
    NetworkAllowlistRule("model_request", "POST", MODEL_HOST, MODEL_PATH_RES),
)


class NetworkAllowlistError(ValueError):
    """Raised when a method/host/path is outside the explicit V1 allowlist."""


def allowlist_canonical_payload() -> dict[str, object]:
    """Stable description of the allowlist for hashing and evidence packets."""
    return {
        "schema_version": ALLOWLIST_VERSION,
        "rules": [
            {
                "family": rule.family,
                "method": rule.method,
                "host": rule.host,
                "path_patterns": [pattern.pattern for pattern in rule.path_patterns],
            }
            for rule in NETWORK_ALLOWLIST_RULES
        ],
        "disallowed_path_prefixes": list(_DISALLOWED_PATH_PREFIXES),
    }


def allowlist_hash() -> str:
    payload = json.dumps(
        allowlist_canonical_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256((payload + "\n").encode("utf-8")).hexdigest()


def _normalize_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise NetworkAllowlistError("network path is missing")
    text = path.strip()
    if "?" in text or "#" in text:
        raise NetworkAllowlistError("network path must be query-free")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise NetworkAllowlistError("network path contains control characters")
    if not text.startswith("/"):
        raise NetworkAllowlistError("network path must be absolute")
    return text


def _normalize_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise NetworkAllowlistError("network host is missing")
    text = host.strip().casefold()
    if ":" in text:
        raise NetworkAllowlistError("network host must not include a port")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise NetworkAllowlistError("network host contains control characters")
    return text


def _normalize_method(method: str) -> str:
    if not isinstance(method, str) or not method.strip():
        raise NetworkAllowlistError("network method is missing")
    return method.strip().upper()


def classify_network_request(method: str, host: str, path: str) -> NetworkFamily:
    """Return the allowlist family or raise if the request is not allowed."""
    normalized_method = _normalize_method(method)
    normalized_host = _normalize_host(host)
    normalized_path = _normalize_path(path)
    lowered = normalized_path.casefold()
    for prefix in _DISALLOWED_PATH_PREFIXES:
        if lowered.startswith(prefix.casefold()):
            raise NetworkAllowlistError("network path is explicitly disallowed")

    for rule in NETWORK_ALLOWLIST_RULES:
        if (
            normalized_method == rule.method
            and normalized_host == rule.host
            and any(pattern.fullmatch(normalized_path) for pattern in rule.path_patterns)
        ):
            return rule.family
    raise NetworkAllowlistError("network request is outside the explicit allowlist")


def assert_network_allowed(method: str, host: str, path: str) -> NetworkFamily:
    return classify_network_request(method, host, path)


def is_network_allowed(method: str, host: str, path: str) -> bool:
    try:
        classify_network_request(method, host, path)
    except NetworkAllowlistError:
        return False
    return True


def parse_url_components(url: str) -> tuple[str, str, str]:
    """Split a URL into scheme, host, and path; reject credentials and non-HTTPS."""
    if not isinstance(url, str) or not url.strip():
        raise NetworkAllowlistError("URL is missing")
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise NetworkAllowlistError("URL is malformed") from exc
    if parsed.scheme != "https":
        raise NetworkAllowlistError("only https network origins are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkAllowlistError("URL must not embed credentials")
    if parsed.port is not None:
        raise NetworkAllowlistError("URL must not include a non-default port")
    if not parsed.hostname:
        raise NetworkAllowlistError("URL host is missing")
    path = parsed.path or "/"
    if parsed.query or parsed.fragment:
        raise NetworkAllowlistError("URL must be query/fragment-free for allowlist checks")
    return parsed.scheme, parsed.hostname.casefold(), path


def assert_url_allowed(method: str, url: str) -> NetworkFamily:
    _scheme, host, path = parse_url_components(url)
    return assert_network_allowed(method, host, path)


def allowed_methods_for_family(family: NetworkFamily) -> frozenset[str]:
    return frozenset(rule.method for rule in NETWORK_ALLOWLIST_RULES if rule.family == family)


def summarize_allowlist() -> dict[str, object]:
    return {
        "version": ALLOWLIST_VERSION,
        "hash": allowlist_hash(),
        "families": {
            rule.family: {
                "method": rule.method,
                "host": rule.host,
                "path_count": len(rule.path_patterns),
            }
            for rule in NETWORK_ALLOWLIST_RULES
        },
        "mutating_methods_blocked_outside_model": sorted(_MUTATING_METHODS - {"POST"}),
    }
