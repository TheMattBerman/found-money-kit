"""Caller-root writers for activation launch packs (FM-031)."""

from __future__ import annotations

from pathlib import Path

from found_money.activation.pack import LaunchPackBuild, build_launch_pack
from found_money.activation.validate import validate_launch_pack_payloads
from found_money.safety.writers import assert_caller_root_write, write_artifact_set_atomic


def write_launch_pack(
    output_root: Path | str,
    build: LaunchPackBuild,
) -> dict[str, Path]:
    """Write a validated launch pack beneath the caller-owned output root."""
    root = Path(output_root)
    # Prove every relative path is confined before the atomic write helper runs.
    for relative in build.payloads:
        assert_caller_root_write(root, relative)
    existing = {
        path.relative_to(root).as_posix()
        for path in (root / "launch-pack").rglob("*")
        if path.is_file()
    }
    if existing - set(build.payloads):
        raise ValueError("existing launch-pack contains stale files")
    validate_launch_pack_payloads(build.payloads)
    return write_artifact_set_atomic(root, build.payloads)


def write_launch_pack_from_inputs(
    output_root: Path | str,
    inputs,
) -> tuple[LaunchPackBuild, dict[str, Path]]:
    """Build then write a launch pack from frozen upstream inputs."""
    pack = build_launch_pack(inputs)
    written = write_launch_pack(output_root, pack)
    return pack, written
