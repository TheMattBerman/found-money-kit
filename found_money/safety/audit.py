"""Safe request audit recording without headers or payloads."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from typing import Mapping

from found_money.contracts.safety import (
    NoMutationAssertionV1,
    SafeRequestAuditLogV1,
    SafeRequestAuditRecordV1,
)
from found_money.safety.allowlist import (
    ALLOWLIST_VERSION,
    NetworkFamily,
    allowlist_hash,
    assert_network_allowed,
)


def _assert_live_http_transport(transport: object) -> object:
    from found_money.connectors.hubspot import HubSpotHttpTransport
    from found_money.connectors.stripe import StripeHttpTransport

    if type(transport) not in {HubSpotHttpTransport, StripeHttpTransport} or not getattr(
        transport, "uses_live_network", False
    ):
        raise ValueError("native audit provenance requires a live connector transport")
    return transport


class SafeRequestAuditor:
    """Collect allowlisted method/host/path records only."""

    def __init__(self) -> None:
        self._native_transports: tuple[object, ...] = ()
        self._records: list[SafeRequestAuditRecordV1] = []

    @classmethod
    def _for_native_transport(cls, transport: object) -> SafeRequestAuditor:
        """Internal constructor used only after a concrete network client is selected."""
        return cls._for_native_transports(transport)

    @classmethod
    def _for_native_transports(cls, *transports: object) -> SafeRequestAuditor:
        """Bind one or more live HubSpot/Stripe HTTP transports as exact provenance."""
        if not transports:
            raise ValueError("native audit provenance requires a live connector transport")
        auditor = cls()
        auditor._native_transports = tuple(
            _assert_live_http_transport(transport) for transport in transports
        )
        return auditor

    def record(
        self, *, method: str, host: str, path: str, provenance: object | None = None
    ) -> SafeRequestAuditRecordV1:
        if self._native_transports:
            if provenance not in self._native_transports:
                raise ValueError("native audit records require the bound transport provenance")
            from found_money.connectors.hubspot import HubSpotHttpTransport
            from found_money.connectors.stripe import StripeHttpTransport
            from found_money.safety.allowlist import HUBSPOT_HOST, STRIPE_HOST

            host_key = host.strip().casefold()
            if type(provenance) is HubSpotHttpTransport and host_key != HUBSPOT_HOST:
                raise ValueError("native audit records require the bound transport provenance")
            if type(provenance) is StripeHttpTransport and host_key != STRIPE_HOST:
                raise ValueError("native audit records require the bound transport provenance")
        family = assert_network_allowed(method, host, path)
        entry = SafeRequestAuditRecordV1(
            method=method.strip().upper(),  # type: ignore[arg-type]
            host=host.strip().casefold(),
            path=path.strip(),
            family=family,
        )
        self._records.append(entry)
        return entry

    @property
    def records(self) -> tuple[SafeRequestAuditRecordV1, ...]:
        return tuple(self._records)

    def as_log(self) -> SafeRequestAuditLogV1:
        return SafeRequestAuditLogV1(
            allowlist_version=ALLOWLIST_VERSION,
            allowlist_hash=allowlist_hash(),
            records=list(self._records),
        )

    def clear(self) -> None:
        self._records.clear()


def audit_digest(log: SafeRequestAuditLogV1) -> str:
    return hashlib.sha256(log.to_canonical_json()).hexdigest()


def build_no_mutation_assertion(
    auditor: SafeRequestAuditor,
    *,
    attached_receipt_hashes: Mapping[str, str] | None = None,
    scope_counts: Mapping[str, int] | None = None,
    built_at: datetime | None = None,
) -> NoMutationAssertionV1:
    """Build a canonical no-mutation assertion from a safe request audit."""
    log = auditor.as_log()
    methods = Counter(record.method for record in log.records)
    families = Counter(str(record.family) for record in log.records)
    scopes: dict[str, int] = (
        {str(key): int(value) for key, value in scope_counts.items()}
        if scope_counts is not None
        else dict(families)
    )
    for family, count in families.items():
        scopes.setdefault(family, count)
    return NoMutationAssertionV1(
        allowlist_version=log.allowlist_version,
        allowlist_hash=log.allowlist_hash,
        allowed_methods_summary={key: methods[key] for key in sorted(methods)},
        request_count=len(log.records),
        scope_counts={key: scopes[key] for key in sorted(scopes)},
        audit_digest=audit_digest(log),
        attached_receipt_hashes=dict(attached_receipt_hashes or {}),
        live_status=("live-unverified" if auditor._native_transports else "fixture-only"),
        built_at=built_at or datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
    )


def empty_fixture_assertion(
    *,
    attached_receipt_hashes: Mapping[str, str] | None = None,
    built_at: datetime | None = None,
) -> NoMutationAssertionV1:
    """Assertion for fixture-only sessions that performed zero network I/O."""
    return build_no_mutation_assertion(
        SafeRequestAuditor(),
        attached_receipt_hashes=attached_receipt_hashes,
        built_at=built_at,
    )


def family_host(family: NetworkFamily) -> str:
    from found_money.safety.allowlist import HUBSPOT_HOST, MODEL_HOST, STRIPE_HOST

    return {
        "hubspot_read": HUBSPOT_HOST,
        "stripe_read": STRIPE_HOST,
        "model_request": MODEL_HOST,
    }[family]
