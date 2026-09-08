"""Public compatibility surface for the FM-021 source stage."""

from .source_stage import (
    DEFAULT_RETRIEVED_AT,
    NativeSourceAdapter,
    SourceArtifact,
    SourceStageError,
    SourceStageResult,
    load_source_manifest,
    run_source_stage,
)

__all__ = [
    "DEFAULT_RETRIEVED_AT",
    "NativeSourceAdapter",
    "SourceArtifact",
    "SourceStageError",
    "SourceStageResult",
    "load_source_manifest",
    "run_source_stage",
]
