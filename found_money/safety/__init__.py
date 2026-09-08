"""FM-030 whole-package no-mutation capability and network allowlist proof."""

from found_money.safety.allowlist import (
    ALLOWLIST_VERSION,
    HUBSPOT_HOST,
    MODEL_HOST,
    NETWORK_ALLOWLIST_RULES,
    NetworkAllowlistError,
    STRIPE_HOST,
    allowlist_hash,
    assert_network_allowed,
    assert_url_allowed,
    is_network_allowed,
    summarize_allowlist,
)
from found_money.safety.audit import (
    SafeRequestAuditor,
    audit_digest,
    build_no_mutation_assertion,
    empty_fixture_assertion,
)
from found_money.safety.capability import (
    import_graph_modules,
    scan_cli_help,
    scan_forbidden_imports,
    scan_package_capabilities,
)
from found_money.safety.evidence import (
    build_release_safety_evidence_packet,
    validate_release_safety_evidence_packet,
    write_no_mutation_assertion,
    write_release_safety_evidence_packet,
)
from found_money.safety.output_scan import scan_output_tree, scan_pdf_bytes, scan_png_bytes
from found_money.safety.transports import (
    NEGATIVE_MATRIX,
    FakeAllowlistTransport,
    FakeHubSpotTransport,
    FakeModelTransport,
    FakeStripeTransport,
    run_negative_matrix,
)
from found_money.safety.writers import (
    assert_caller_root_write,
    prove_writer_rejects_escapes,
    scan_writers_use_root_validation,
    write_artifact_set_atomic,
)

__all__ = [
    "ALLOWLIST_VERSION",
    "HUBSPOT_HOST",
    "MODEL_HOST",
    "NEGATIVE_MATRIX",
    "NETWORK_ALLOWLIST_RULES",
    "NetworkAllowlistError",
    "STRIPE_HOST",
    "FakeAllowlistTransport",
    "FakeHubSpotTransport",
    "FakeModelTransport",
    "FakeStripeTransport",
    "SafeRequestAuditor",
    "allowlist_hash",
    "assert_caller_root_write",
    "assert_network_allowed",
    "assert_url_allowed",
    "audit_digest",
    "build_no_mutation_assertion",
    "build_release_safety_evidence_packet",
    "empty_fixture_assertion",
    "import_graph_modules",
    "is_network_allowed",
    "prove_writer_rejects_escapes",
    "run_negative_matrix",
    "scan_cli_help",
    "scan_forbidden_imports",
    "scan_output_tree",
    "scan_package_capabilities",
    "scan_pdf_bytes",
    "scan_png_bytes",
    "scan_writers_use_root_validation",
    "summarize_allowlist",
    "validate_release_safety_evidence_packet",
    "write_no_mutation_assertion",
    "write_artifact_set_atomic",
    "write_release_safety_evidence_packet",
]
