"""Fake transports that fail closed outside the explicit network allowlist."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from found_money.safety.allowlist import (
    HUBSPOT_HOST,
    MODEL_HOST,
    NetworkAllowlistError,
    STRIPE_HOST,
    assert_network_allowed,
    assert_url_allowed,
)
from found_money.safety.audit import SafeRequestAuditor


class FakeAllowlistTransport:
    """Reject every non-allowlisted method/host/path before any I/O.

    Approved reads and model requests are recorded as safe audits and then
    delegated to an optional response factory. Headers and payloads are never
    retained on the audit log.
    """

    def __init__(
        self,
        *,
        default_host: str,
        responder: Callable[[str, str, str], Mapping[str, Any]] | None = None,
        auditor: SafeRequestAuditor | None = None,
    ) -> None:
        self.default_host = default_host.casefold()
        self._responder = responder
        self.auditor = auditor or SafeRequestAuditor()
        self.rejected: list[dict[str, str]] = []

    def request(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        host: str | None = None,
    ) -> Mapping[str, Any]:
        # Intentionally ignore headers/query payloads for audit retention.
        _ = query
        _ = headers
        target_host = (host or self.default_host).casefold()
        try:
            assert_network_allowed(method, target_host, path)
        except NetworkAllowlistError as exc:
            self.rejected.append(
                {"method": method.upper(), "host": target_host, "path": path, "reason": str(exc)}
            )
            raise
        self.auditor.record(method=method, host=target_host, path=path)
        if self._responder is None:
            return {"ok": True, "path": path}
        return self._responder(method.upper(), target_host, path)


class FakeHubSpotTransport(FakeAllowlistTransport):
    def __init__(
        self,
        *,
        responder: Callable[[str, str, str], Mapping[str, Any]] | None = None,
        auditor: SafeRequestAuditor | None = None,
    ) -> None:
        super().__init__(default_host=HUBSPOT_HOST, responder=responder, auditor=auditor)


class FakeStripeTransport(FakeAllowlistTransport):
    def __init__(
        self,
        *,
        responder: Callable[[str, str, str], Mapping[str, Any]] | None = None,
        auditor: SafeRequestAuditor | None = None,
    ) -> None:
        super().__init__(default_host=STRIPE_HOST, responder=responder, auditor=auditor)


class FakeModelTransport(FakeAllowlistTransport):
    """Allowlisted model request path only; never records headers or bodies."""

    def __init__(
        self,
        *,
        responder: Callable[[str, str, str], Mapping[str, Any]] | None = None,
        auditor: SafeRequestAuditor | None = None,
    ) -> None:
        super().__init__(default_host=MODEL_HOST, responder=responder, auditor=auditor)

    def invoke(self, *, method: str, url: str) -> Mapping[str, Any]:
        assert_url_allowed(method, url)
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        assert parsed.hostname is not None
        return self.request(method=method, path=parsed.path or "/", host=parsed.hostname)


NEGATIVE_MATRIX: tuple[tuple[str, str, str, str], ...] = (
    ("POST", HUBSPOT_HOST, "/crm/v3/objects/contacts", "hubspot_write"),
    ("PUT", HUBSPOT_HOST, "/crm/v3/objects/deals", "hubspot_update"),
    ("PATCH", HUBSPOT_HOST, "/crm/v3/objects/contacts", "hubspot_patch"),
    ("DELETE", HUBSPOT_HOST, "/crm/v3/objects/contacts", "hubspot_delete"),
    ("POST", HUBSPOT_HOST, "/crm/v3/objects/contacts/batch/create", "hubspot_batch"),
    ("POST", HUBSPOT_HOST, "/marketing/v3/marketing-events/audiences", "hubspot_audience"),
    ("GET", "evil.example", "/crm/v3/objects/contacts", "hubspot_wrong_host"),
    ("POST", STRIPE_HOST, "/v1/customers", "stripe_write"),
    ("DELETE", STRIPE_HOST, "/v1/customers", "stripe_delete"),
    ("POST", STRIPE_HOST, "/v1/payment_intents", "stripe_create_payment"),
    ("GET", "evil.example", "/v1/customers", "stripe_wrong_host"),
    ("GET", MODEL_HOST, "/v1/responses", "model_get_forbidden"),
    ("POST", MODEL_HOST, "/v1/files", "model_files_forbidden"),
    ("POST", "evil.example", "/v1/responses", "model_wrong_host"),
    ("POST", HUBSPOT_HOST, "/v1/responses", "model_on_hubspot_host"),
)


def run_negative_matrix(
    transport: FakeAllowlistTransport | None = None,
) -> list[dict[str, str]]:
    """Exercise the negative matrix; every case must raise NetworkAllowlistError."""
    probe = transport or FakeAllowlistTransport(default_host=HUBSPOT_HOST)
    outcomes: list[dict[str, str]] = []
    for method, host, path, label in NEGATIVE_MATRIX:
        try:
            probe.request(method=method, path=path, host=host)
        except NetworkAllowlistError:
            outcomes.append({"label": label, "result": "rejected"})
            continue
        raise AssertionError(f"negative matrix case {label} was not rejected")
    return outcomes
