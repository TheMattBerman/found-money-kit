"""Activation launch-pack and honest handoff exports (FM-031 / FM-032).

These writers produce approval-only artifacts. They never send, schedule,
create audiences, or mutate CRM/payment/ad systems.
"""

from found_money.activation.intake import (
    HandoffActionState,
    HandoffActions,
    IntakeConfigError,
    build_handoff_actions,
    coerce_handoff_actions,
    html_for_public_scan,
    local_intake_fixture_html,
    parse_handoff_intake_config,
    project_handoff_intake,
    validate_export_href,
    validate_intake_url,
)
from found_money.activation.pack import (
    LaunchPackBuild,
    LaunchPackInputs,
    build_launch_pack,
    public_safe_launch_pack_projection,
    segment_id_for,
    sha256_bytes,
)
from found_money.activation.validate import (
    LaunchPackValidationError,
    parse_launch_pack_manifest,
    validate_launch_pack_payloads,
    validate_launch_pack_tree,
    validate_private_launch_pack_payloads,
)
from found_money.activation.writers import write_launch_pack, write_launch_pack_from_inputs

__all__ = [
    "HandoffActionState",
    "HandoffActions",
    "IntakeConfigError",
    "LaunchPackBuild",
    "LaunchPackInputs",
    "LaunchPackValidationError",
    "build_handoff_actions",
    "build_launch_pack",
    "coerce_handoff_actions",
    "html_for_public_scan",
    "local_intake_fixture_html",
    "parse_handoff_intake_config",
    "project_handoff_intake",
    "parse_launch_pack_manifest",
    "public_safe_launch_pack_projection",
    "segment_id_for",
    "sha256_bytes",
    "validate_export_href",
    "validate_intake_url",
    "validate_launch_pack_payloads",
    "validate_launch_pack_tree",
    "validate_private_launch_pack_payloads",
    "write_launch_pack",
    "write_launch_pack_from_inputs",
]
