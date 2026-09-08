"""Offline Recovery Room manifest contracts."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class RecoveryRoomLinkV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_room_id: str
    target: str


class RecoveryRoomAssetV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str


class RecoveryRoomManifestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["recovery-room-manifest.v1"] = "recovery-room-manifest.v1"
    run_id: str
    room_ids: list[str]
    room_headings: dict[str, str]
    recommended_actions: dict[str, str]
    links: list[RecoveryRoomLinkV1]
    local_assets: list[RecoveryRoomAssetV1]
    linked_artifacts: list[str]

    @model_validator(mode="after")
    def _rooms(self) -> Self:
        expected = ["room-find", "room-evidence", "room-play", "room-launch"]
        if self.room_ids != expected:
            raise ValueError("Recovery Room requires the four canonical room ids")
        if sorted(self.recommended_actions) != sorted(expected):
            raise ValueError("every room requires exactly one recommended action")
        expected_headings = {
            "room-find": "The Find",
            "room-evidence": "The Evidence",
            "room-play": "The Play",
            "room-launch": "The Launch",
        }
        if self.room_headings != expected_headings:
            raise ValueError("Recovery Room headings must match the canonical rooms")
        if {link.source_room_id for link in self.links} != set(expected):
            raise ValueError("every room requires one recorded link")
        if len(self.links) != 4:
            raise ValueError("Recovery Room requires exactly four room links")
        paths = [asset.path for asset in self.local_assets]
        if len(paths) != len(set(paths)):
            raise ValueError("local asset paths must be unique")
        expected_artifacts = [
            "money-map.json",
            "recovery-plays.json",
            "provenance/run-manifest.json",
            "launch-pack/manifest.json",
        ]
        if self.linked_artifacts != expected_artifacts:
            raise ValueError("Recovery Room linked artifacts are incomplete or unordered")
        for path in [*paths, *self.linked_artifacts]:
            normalized = path.replace("\\", "/")
            if (
                normalized.startswith(("/", "~/"))
                or re.match(r"^[A-Za-z]:/", normalized)
                or ".." in PurePosixPath(normalized).parts
                or "?" in normalized
            ):
                raise ValueError("manifest paths must be relative and traversal-free")
        return self

    def to_canonical_json(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return (text + "\n").encode("utf-8")
