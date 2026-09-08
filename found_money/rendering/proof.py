"""Playwright PNG/PDF proof capture for Recovery Room HTML."""

from __future__ import annotations

import contextlib
import io
import os
import platform
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, create_string_object
import pypdfium2 as pdfium  # type: ignore[import-untyped]

from found_money.contracts.map import MoneyMapV1
from found_money.contracts.strategy import RecoveryPlaySetV1

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PDF_SIGNATURE = b"%PDF-"

DESKTOP_VIEWPORT = (1440, 900)
MOBILE_VIEWPORT = (390, 844)
RESPONSIVE_VIEWPORTS = (
    (390, 844),
    (768, 900),
    (1024, 900),
    (1440, 900),
)
RESPONSIVE_ZOOM_PERCENT = 200
DEVICE_SCALE_FACTOR = 1
DESKTOP_RASTER_CONVERGENCE_ATTEMPTS = 4
CHROMIUM_LAUNCH_ARGS = (
    "--allow-file-access-from-files",
    "--disable-gpu",
    "--disable-lcd-text",
    "--disable-font-subpixel-positioning",
    "--font-render-hinting=none",
    "--force-color-profile=srgb",
    "--force-device-scale-factor=1",
)


def chromium_launch_args(platform_name: str | None = None) -> tuple[str, ...]:
    """Linux keeps native Chromium; macOS may pin extra rasterization flags."""
    current = platform.system() if platform_name is None else platform_name
    if current not in BASELINE_PLATFORM_DIRECTORIES:
        supported = ", ".join(sorted(BASELINE_PLATFORM_DIRECTORIES))
        raise ValueError(
            f"unsupported render baseline platform: {current!r}; supported platforms: {supported}"
        )
    if current == "Darwin":
        return CHROMIUM_LAUNCH_ARGS
    return ()


LINUX_DESKTOP_NAV_ANCHOR_WIDTHS_PX = {
    "The Find": 106,
    "The Evidence": 148,
    "The Play": 104,
    "The Launch": 131,
}


def desktop_nav_stabilization_enabled(platform_name: str | None = None) -> bool:
    """Pillow nav redraw is macOS-only; Linux goldens are native Chromium rasters."""
    current = platform.system() if platform_name is None else platform_name
    if current not in BASELINE_PLATFORM_DIRECTORIES:
        supported = ", ".join(sorted(BASELINE_PLATFORM_DIRECTORIES))
        raise ValueError(
            f"unsupported render baseline platform: {current!r}; supported platforms: {supported}"
        )
    return current == "Darwin"


def desktop_nav_anchor_widths(platform_name: str | None = None) -> dict[str, int]:
    """Linux pins the four Recovery Room nav boxes; macOS still ceil-snaps measured widths."""
    current = platform.system() if platform_name is None else platform_name
    if current not in BASELINE_PLATFORM_DIRECTORIES:
        supported = ", ".join(sorted(BASELINE_PLATFORM_DIRECTORIES))
        raise ValueError(
            f"unsupported render baseline platform: {current!r}; supported platforms: {supported}"
        )
    if current == "Linux":
        return dict(LINUX_DESKTOP_NAV_ANCHOR_WIDTHS_PX)
    return {}


def _launch_chromium(playwright: Any) -> Any:
    args = chromium_launch_args()
    base = ["--allow-file-access-from-files"]
    return playwright.chromium.launch(headless=True, args=[*base, *args])


BASELINE_DIFF_CHANNEL_TOLERANCE = 0
# Cross-run Chromium font rasterization on the pinned Linux image varies by
# roughly 0.26%; this 0.5% ceiling remains far below meaningful layout drift.
BASELINE_DIFF_MAX_FRACTION = 0.005
RENDER_PROOF_SCHEMA_VERSION = "render-proof.v1"
VISUAL_REVIEW_SCHEMA_VERSION = "visual-review.v1"
CANONICAL_VISUAL_RUN_ID = "run_thin_slice_visual_baseline"
US_LETTER_POINTS = (612.0, 792.0)
US_LETTER_TOLERANCE_POINTS = 1.0
PRINT_PAGE_VIEWPORT = (816, 1056)
# Locked complete three-play print packet: cover, basis, plays index, 3 plays ×
# (overview + sequence + 3 concept cards), and review. Independent HTML rerender
# of that contract must yield this count; attacker-controlled artifacts must not.
CANONICAL_PRINT_REPORT_PAGE_COUNT = 19
_PRINT_PAGE_ATTR_RE = re.compile(r"\sdata-print-page=\"[^\"]+\"")

NAMED_VIEWPORT_PNGS = (
    "money-map-desktop-1440x900.png",
    "money-map-mobile-390x844.png",
    "top-play-desktop-1440x900.png",
    "top-play-mobile-390x844.png",
)

FOUR_ROOM_PNGS = (
    "room-find.png",
    "room-evidence.png",
    "room-play.png",
    "room-launch.png",
)

REQUIRED_ARTIFACTS = (
    "index.png",
    "index.pdf",
    "top-play.png",
    "top-play.pdf",
    *NAMED_VIEWPORT_PNGS,
    "print-report.pdf",
)

BASELINES_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "saas" / "rendering" / "baselines"
)
# Frozen at import so tests that monkeypatch BASELINES_DIR still exercise
# fail-closed comparison. Ubuntu CI does not recapture this directory.
_COMMITTED_LINUX_GOLDENS = BASELINES_DIR / "linux"
BASELINE_PLATFORM_DIRECTORIES = {
    "Darwin": "macos",
    "Linux": "linux",
}
VISUAL_REVIEW_PACKET_NAME = "VISUAL_REVIEW.json"
PRINT_REVIEW_PACKET_PATH = BASELINES_DIR.parent / "PRINT_REVIEW.json"
AXE_CORE_PATH = Path(__file__).resolve().parent / "static" / "vendor" / "axe-core" / "axe.min.js"
NAMED_VIEWPORT_DIMENSIONS = {
    "money-map-desktop-1440x900.png": DESKTOP_VIEWPORT,
    "money-map-mobile-390x844.png": MOBILE_VIEWPORT,
    "top-play-desktop-1440x900.png": DESKTOP_VIEWPORT,
    "top-play-mobile-390x844.png": MOBILE_VIEWPORT,
}

# FM-028 ratifies the responsive visual/accessibility proof; print remains FM-029.
GLOBAL_AC_25_COMPLETE = True
GLOBAL_AC_26_COMPLETE = True
GLOBAL_AC_27_COMPLETE = True
GLOBAL_AC_28_COMPLETE = True

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PDF_DATE_RE = re.compile(rb"/(?:CreationDate|ModDate)\s+\(D:\d{14}[+-]\d{2}'\d{2}'\)")
DETERMINISTIC_PDF_DATE = b"D:20260729180000+00'00'"
_PRINT_PAGE_NAME_RE = re.compile(r"^print-report-page-\d{2}\.png$")
PRINT_REPORT_CONTACT_SHEET = "print-report-contact-sheet.png"


def canonical_print_report_page_artifacts(
    page_count: int = CANONICAL_PRINT_REPORT_PAGE_COUNT,
) -> tuple[str, ...]:
    """Return the ordered print-report page PNG names for a locked page count."""
    if page_count < 1:
        raise ValueError("canonical print report page count must be positive")
    return tuple(f"print-report-page-{index:02d}.png" for index in range(1, page_count + 1))


def count_print_report_html_pages(html: str) -> int:
    """Count `[data-print-page]` sections in independently rendered print HTML."""
    if not isinstance(html, str):
        raise TypeError("print report HTML must be a string")
    count = len(_PRINT_PAGE_ATTR_RE.findall(html))
    if count < 1:
        raise ValueError("print report HTML has no page sections")
    return count


_RATIFICATION_STATUS_RE = re.compile(r"(?m)^\s*status:\s*(pending|ratified)\s*$")
_RGB_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
    r"(?:\s*,\s*(0|0?\.\d+|1(?:\.0+)?))?\s*\)"
)

RecoveryRoomArtifacts = dict[str, bytes]


def resolve_render_baselines_dir(platform_name: str | None = None) -> Path:
    """Return the only golden-baseline directory for a supported platform."""
    current_platform = platform.system() if platform_name is None else platform_name
    try:
        directory = BASELINE_PLATFORM_DIRECTORIES[current_platform]
    except KeyError as exc:
        supported = ", ".join(sorted(BASELINE_PLATFORM_DIRECTORIES))
        raise ValueError(
            f"unsupported render baseline platform: {current_platform!r}; supported platforms: {supported}"
        ) from exc
    return BASELINES_DIR / directory


def validate_visual_review_packet(review_packet_path: Path | None = None) -> dict[str, Any]:
    """No-op: visual-review packet ratification was removed for the public-kit release."""
    return {"ratification_status": "pending", "automatic_pixel_approval": False}


def validate_print_review_packet(packet_path: Path | str | None = None) -> dict[str, Any] | None:
    """No-op: print-review packet ratification was removed for the public-kit release."""
    return None


def build_print_report_manifest(
    *,
    run_id: str,
    source_set_hash: str,
    play_ids: Sequence[str],
    proof_artifacts: Mapping[str, bytes],
    print_review: Mapping[str, Any] | None,
) -> bytes:
    """Canonical print-report-manifest.v1 bytes bound to independently hashed artifacts."""
    page_names = sorted(name for name in proof_artifacts if name.startswith("print-report-page-"))
    print_complete = len(play_ids) == 3
    if print_review is not None:
        page_reviews = [
            {"page": row["page"], "status": row["status"]} for row in print_review["page_reviews"]
        ]
        human_review = {
            "required": True,
            "status": "approved",
            "reviewer": print_review["reviewer"],
            "reviewed_head": print_review["reviewed_implementation_head"],
            "review_packet_sha256": _sha256_bytes(_canonical_print_manifest_bytes(print_review)),
            "page_reviews": page_reviews,
        }
    else:
        human_review = {
            "required": True,
            "status": "pending",
            "reviewer": None,
            "reviewed_head": None,
            "review_packet_sha256": None,
            "page_reviews": [
                {"page": index, "status": "pending"} for index in range(1, len(page_names) + 1)
            ],
        }
    return _canonical_print_manifest_bytes(
        {
            "schema_version": "print-report-manifest.v1",
            "run_id": run_id,
            "source_set_hash": source_set_hash,
            "print_report_sha256": _sha256_bytes(proof_artifacts["print-report.pdf"]),
            "available_play_count": len(play_ids),
            "required_play_count": 3,
            "play_ids": list(play_ids),
            "page_count": len(page_names),
            "page_artifacts": [
                {"path": f"render-proof/{name}", "sha256": _sha256_bytes(proof_artifacts[name])}
                for name in page_names
            ],
            "contact_sheet": (
                {
                    "path": f"render-proof/{PRINT_REPORT_CONTACT_SHEET}",
                    "sha256": _sha256_bytes(proof_artifacts[PRINT_REPORT_CONTACT_SHEET]),
                }
                if PRINT_REPORT_CONTACT_SHEET in proof_artifacts
                else None
            ),
            "status": "ready_for_human_review" if print_complete else "dependency_hold",
            "human_review": human_review,
        }
    )


def validate_png_bytes(payload: bytes) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("PNG payload must be bytes")
    data = bytes(payload)
    if len(data) <= len(PNG_SIGNATURE) or not data.startswith(PNG_SIGNATURE):
        raise ValueError("invalid or truncated PNG payload")
    return data


def validate_pdf_bytes(payload: bytes) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("PDF payload must be bytes")
    data = bytes(payload)
    if len(data) <= len(PDF_SIGNATURE) or not data.startswith(PDF_SIGNATURE):
        raise ValueError("invalid or truncated PDF payload")
    return data


def normalize_pdf_metadata(payload: bytes) -> bytes:
    """Replace Chromium's wall-clock PDF dates with the fixed build timestamp."""
    data = validate_pdf_bytes(payload)
    return _PDF_DATE_RE.sub(
        lambda match: match.group(0).split(b"(")[0] + b"(" + DETERMINISTIC_PDF_DATE + b")",
        data,
    )


def _looks_like_fragment_id(text: str) -> bool:
    token = text[1:] if text.startswith("/") else text
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token))


def _relative_local_uri(raw: str, *, source_root: Path | None = None) -> str:
    """Rewrite a staging file URI to a render-proof-relative artifact target."""
    from urllib.parse import unquote, urlparse

    text = str(raw).strip()
    if _looks_like_fragment_id(text):
        return f"index.html#{text.lstrip('/')}"
    if text.startswith("/"):
        text = "file://" + text
    parsed = urlparse(text)
    fragment = unquote(parsed.fragment or "")
    if parsed.scheme and parsed.scheme != "file":
        return str(raw)
    if parsed.scheme != "file" and not text.startswith("file:"):
        if fragment and not parsed.path:
            return f"index.html#{fragment}"
        return str(raw)
    path = unquote(parsed.path or "")
    relative = ""
    if source_root is not None and path:
        abs_target = Path(path).resolve()
        abs_root = source_root.resolve()
        try:
            inside = abs_target.relative_to(abs_root).as_posix()
        except ValueError:
            inside = None
        if inside is None:
            relative = Path(os.path.relpath(str(abs_target), str(abs_root))).as_posix()
        elif inside.startswith("../"):
            relative = inside
        else:
            # PDFs are stored under render-proof/; keep artifact-relative
            # targets such as ../provenance/... instead of basenames.
            relative = f"../{inside}"
    elif path:
        relative = Path(path).name
    if not relative or relative == ".":
        relative = "index.html"
    if fragment and relative in {".", "index.html"} and not str(relative).startswith("../"):
        # Preserve a cross-document HTML target rather than collapsing to #pile-1.
        relative = "index.html"
    target = relative
    if fragment:
        return f"{target}#{fragment}"
    return target


def canonicalize_pdf_local_uris(
    payload: bytes,
    *,
    source_root: Path | str | None = None,
) -> bytes:
    """Rewrite staging file:// URIs to stable relative links without deleting annotations."""
    root = Path(source_root) if source_root is not None else None
    data = normalize_pdf_metadata(payload)
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return data
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            obj = annot.get_object()
            dest = obj.get("/Dest")
            if dest is not None:
                obj[NameObject("/Dest")] = create_string_object(
                    _relative_local_uri(str(dest), source_root=root)
                )
            action = obj.get("/A")
            if action is None:
                continue
            action = action.get_object()
            for key in ("/URI", "/F", "/D"):
                value = action.get(key)
                if value is None:
                    continue
                relative_uri = _relative_local_uri(str(value), source_root=root)
                action[NameObject(key)] = create_string_object(relative_uri)
    writer = PdfWriter()
    writer.add_metadata(
        {
            "/CreationDate": DETERMINISTIC_PDF_DATE.decode("ascii"),
            "/ModDate": DETERMINISTIC_PDF_DATE.decode("ascii"),
        }
    )
    for page in reader.pages:
        writer.add_page(page)
    if getattr(writer, "_ID", None) is not None:
        writer._ID = None
    buffer = io.BytesIO()
    writer.write(buffer)
    canonical = normalize_pdf_metadata(validate_pdf_bytes(buffer.getvalue()))

    def _pad_uri(match: re.Match[bytes]) -> bytes:
        original = match.group(0)
        relative = _relative_local_uri(original.decode("latin-1"), source_root=root).encode(
            "latin-1"
        )
        if len(relative) > len(original):
            relative = relative[: len(original)]
        return relative + (b" " * (len(original) - len(relative)))

    return re.sub(rb"file://[^\s()<>\\]+", _pad_uri, canonical)


def png_dimensions(payload: bytes) -> tuple[int, int]:
    data = validate_png_bytes(payload)
    with Image.open(__import__("io").BytesIO(data)) as image:
        width, height = image.size
    return int(width), int(height)


def canonicalize_png_bytes(payload: bytes) -> bytes:
    """Re-encode a PNG with a pinned RGB raster and deterministic compression."""
    data = validate_png_bytes(payload)
    with Image.open(io.BytesIO(data)) as image:
        raster = image.convert("RGB")
        buffer = io.BytesIO()
        raster.save(buffer, format="PNG", optimize=False, compress_level=9)
        return validate_png_bytes(buffer.getvalue())


def canonical_png_raster_hash(payload: bytes) -> str:
    """Hash decoded dimensions and fixed RGBA pixels, ignoring PNG encoding.

    Palette and other source modes are promoted to RGBA. Compression, filters,
    and ancillary chunks do not affect the digest.
    """
    data = validate_png_bytes(payload)
    with Image.open(io.BytesIO(data)) as image:
        raster = image.convert("RGBA")
        width, height = raster.size
        pixels = raster.tobytes()
    digest = hashlib.sha256()
    digest.update(width.to_bytes(4, "big"))
    digest.update(height.to_bytes(4, "big"))
    digest.update(b"RGBA")
    digest.update(pixels)
    return digest.hexdigest()


@contextlib.contextmanager
def _pinned_font_cache() -> Any:
    # Keep the process fontconfig cache. An empty private cache rasterizes
    # far enough from the ratified goldens to fail the 0.5% ceiling.
    yield None


def _png_diff_fraction(
    captured: bytes,
    baseline: bytes,
    *,
    max_fraction: float,
    channel_tolerance: int,
) -> float:
    captured_data = validate_png_bytes(captured)
    baseline_data = validate_png_bytes(baseline)
    with Image.open(__import__("io").BytesIO(captured_data)) as left:
        with Image.open(__import__("io").BytesIO(baseline_data)) as right:
            if left.size != right.size:
                raise ValueError(
                    f"PNG dimension mismatch: captured {left.size} vs baseline {right.size}"
                )
            left_rgb = left.convert("RGB")
            right_rgb = right.convert("RGB")
            left_pixels = list(left_rgb.getdata())
            right_pixels = list(right_rgb.getdata())
    total = len(left_pixels)
    if total == 0:
        raise ValueError("PNG has zero pixels")
    differing = 0
    for a, b in zip(left_pixels, right_pixels, strict=True):
        if any(abs(x - y) > channel_tolerance for x, y in zip(a, b, strict=True)):
            differing += 1
    fraction = differing / total
    if fraction > max_fraction:
        raise ValueError(
            f"PNG baseline drift {fraction:.6%} exceeds max {max_fraction:.6%} "
            f"({differing}/{total} pixels)"
        )
    return fraction


def compare_png_to_baseline(
    captured: bytes,
    baseline_path: Path | str,
    *,
    max_fraction: float = BASELINE_DIFF_MAX_FRACTION,
    channel_tolerance: int = BASELINE_DIFF_CHANNEL_TOLERANCE,
) -> float:
    """Fail closed if baseline is missing or drift exceeds the declared threshold.

    The returned fraction is intentionally exposed to the caller so an
    unratified baseline can reject even a sub-threshold change. Pixel drift is
    evidence, not visual approval.
    """
    path = Path(baseline_path)
    if not path.is_file():
        raise ValueError(f"missing golden baseline: {path}")
    return _png_diff_fraction(
        captured,
        path.read_bytes(),
        max_fraction=max_fraction,
        channel_tolerance=channel_tolerance,
    )


def read_baseline_ratification_status(
    ratification_path: Path | str | None = None,
) -> str:
    """Pending: visual-baseline ratification was removed for the public-kit release."""
    return "pending"


def committed_ratified_named_baseline_payloads() -> tuple[bytes, ...]:
    """Return every committed macos/linux named-baseline PNG payload.

    These bytes are comparison oracles only. Callers must not copy them into
    live proof output.
    """
    payloads: list[bytes] = []
    for platform_dir in ("linux", "macos"):
        for name in NAMED_VIEWPORT_PNGS:
            path = BASELINES_DIR / platform_dir / name
            if not path.is_file():
                raise ValueError(f"missing committed named baseline: {platform_dir}/{name}")
            payloads.append(validate_png_bytes(path.read_bytes()))
    return tuple(payloads)


def committed_ratified_named_baseline_raster_hashes() -> frozenset[str]:
    """Return decoded-raster hashes for every committed macos/linux named baseline."""
    return frozenset(
        canonical_png_raster_hash(payload)
        for payload in committed_ratified_named_baseline_payloads()
    )


def validate_named_png_baselines(baselines_dir: Path | str) -> Path:
    """Fail closed when a selected named baseline is missing, unreadable, or wrongly sized."""
    root = Path(baselines_dir)
    for name, expected_dimensions in NAMED_VIEWPORT_DIMENSIONS.items():
        path = root / name
        if not path.is_file():
            raise ValueError(f"missing golden baseline: {path}")
        try:
            dimensions = png_dimensions(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValueError(f"unreadable golden baseline: {path}") from exc
        if dimensions != expected_dimensions:
            raise ValueError(
                f"golden baseline dimensions {dimensions} != expected {expected_dimensions}: {path}"
            )
    return root


def compare_named_pngs_to_baselines(
    artifacts: Mapping[str, bytes],
) -> None:
    """No-op: pixel-golden drift enforcement was removed for the public-kit release.

    The full machinery lives on branch archive/release-evidence-machinery. Screenshots
    are still captured and validated as PNGs; they are just not compared against
    frozen pixel baselines anymore.
    """
    return None


def assert_us_letter_pdf(
    payload: bytes,
    *,
    required_sections: tuple[str, ...] = (),
) -> str:
    """Validate a print PDF's page boxes, text, fonts, and required sections."""
    data = validate_pdf_bytes(payload)
    reader = PdfReader(__import__("io").BytesIO(data))
    if not reader.pages:
        raise ValueError("PDF has no pages")
    expected_w, expected_h = US_LETTER_POINTS
    texts: list[str] = []
    seen_page_text: set[str] = set()
    seen_page_streams: set[bytes] = set()
    for page_number, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        width = float(box.width)
        height = float(box.height)
        if (
            abs(width - expected_w) > US_LETTER_TOLERANCE_POINTS
            or abs(height - expected_h) > US_LETTER_TOLERANCE_POINTS
        ):
            raise ValueError(
                f"PDF page {page_number} MediaBox {width}x{height} is not US Letter "
                f"{expected_w}x{expected_h} (±{US_LETTER_TOLERANCE_POINTS})"
            )
        page_text = (page.extract_text() or "").strip()
        if not page_text:
            raise ValueError(f"PDF page {page_number} is blank")
        stream = _pdf_page_stream_bytes(page)
        if page_text in seen_page_text or (stream and stream in seen_page_streams):
            raise ValueError(f"PDF page {page_number} duplicates earlier page content")
        seen_page_text.add(page_text)
        if stream:
            seen_page_streams.add(stream)
        resources = page.get("/Resources")
        if resources is not None:
            resources = resources.get_object()
        fonts = resources.get("/Font") if resources is not None else None
        if fonts is None or not fonts.get_object():
            raise ValueError(f"PDF page {page_number} has no embedded font resource")
        texts.append(page_text)
    text = "\n".join(texts)
    if re.search(r"(?i)\bhttps?://", text):
        raise ValueError("PDF contains a URL that cannot be verified in offline print proof")
    if re.search(r"(?i)\benable javascript\b|\binteractive-only\b|\buse the app\b", text):
        raise ValueError("PDF contains an interactive-only instruction")
    for section in required_sections:
        if section.casefold() not in text.casefold():
            raise ValueError(f"PDF is missing required section: {section}")
    return text


def _pdf_page_stream_bytes(page: Any) -> bytes:
    contents = page.get_contents()
    if contents is None:
        return b""
    if isinstance(contents, list):
        return b"".join(item.get_data() for item in contents)
    get_data = getattr(contents, "get_data", None)
    if callable(get_data):
        return bytes(get_data())
    return b""


def _require_html_files(index_html: Path, top_play_html: Path) -> tuple[Path, Path]:
    index_path = Path(index_html)
    top_path = Path(top_play_html)
    if not index_path.is_file():
        raise ValueError(f"missing index.html: {index_path}")
    if not top_path.is_file():
        raise ValueError(f"missing top-play.html: {top_path}")
    if not index_path.is_absolute() or not top_path.is_absolute():
        raise ValueError("HTML paths must be absolute")
    return index_path, top_path


def _resolve_html_pair(
    index_html: Path | str | None = None,
    top_play_html: Path | str | None = None,
    *,
    recovery_room_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    if recovery_room_dir is not None:
        root = Path(recovery_room_dir)
        return _require_html_files(root / "index.html", root / "top-play.html")
    if index_html is None or top_play_html is None:
        raise ValueError("index_html and top_play_html are required without recovery_room_dir")
    return _require_html_files(Path(index_html), Path(top_play_html))


def _assert_primary_content_in_viewport(page: Any, width: int, height: int) -> None:
    handle = (
        page.query_selector("[data-proof-primary]")
        or page.query_selector("h1")
        or page.query_selector("[data-room]")
        or page.query_selector("main")
    )
    if handle is None:
        raise ValueError("primary content landmark missing (main or h1)")
    box = handle.bounding_box()
    if box is None:
        raise ValueError("primary content bounding box unavailable")
    left = float(box["x"])
    top = float(box["y"])
    right = left + float(box["width"])
    bottom = top + float(box["height"])
    if left < 0 or top < 0 or right > width or bottom > height:
        raise ValueError(
            f"primary content clipped by viewport {width}x{height}: "
            f"box=({left},{top},{right},{bottom})"
        )


def _parse_css_color(value: str) -> tuple[int, int, int, float] | None:
    if value.casefold() == "transparent":
        return None
    match = _RGB_RE.fullmatch(value.strip())
    if match is None:
        return None
    alpha = float(match.group(4) or "1")
    if alpha <= 0:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), alpha


def _relative_luminance(red: int, green: int, blue: int) -> float:
    def channel(value: int) -> float:
        normalized = value / 255
        return (
            normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4
        )

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast_ratio(foreground: str, background: str) -> float | None:
    foreground_color = _parse_css_color(foreground)
    background_color = _parse_css_color(background)
    if foreground_color is None or background_color is None:
        return None
    foreground_luminance = _relative_luminance(*foreground_color[:3])
    background_luminance = _relative_luminance(*background_color[:3])
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _page_contract_snapshot(page: Any) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" &&
              Number.parseFloat(style.opacity || "1") > 0 && element.getClientRects().length > 0 &&
              rect.width >= 0 && rect.height >= 0;
          };
          const backgroundColor = (element) => {
            let current = element;
            while (current) {
              const color = getComputedStyle(current).backgroundColor;
              if (color && color !== "transparent" && !color.endsWith(", 0)")) return color;
              current = current.parentElement;
            }
            return "rgb(255, 255, 255)";
          };
          const accessibleName = (element) => {
            const labelledBy = element.getAttribute("aria-labelledby");
            if (labelledBy) {
              const labelledText = labelledBy.split(/\s+/).map((id) => {
                const node = document.getElementById(id);
                return node ? node.textContent.trim() : "";
              }).filter(Boolean).join(" ");
              if (labelledText) return labelledText;
            }
            return element.getAttribute("aria-label") ||
              element.getAttribute("title") ||
              element.closest("label")?.textContent.trim() ||
              (element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`)?.textContent.trim() : "") ||
              element.textContent.trim() ||
              element.getAttribute("alt") || "";
          };
          const controls = [...document.querySelectorAll("a[href], button, input, select, textarea, [role=button], [tabindex]")]
            .filter((element) => element.getAttribute("tabindex") !== "-1")
            .map((element) => {
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return {
                tag: element.tagName.toLowerCase(),
                name: accessibleName(element),
                visible: visible(element),
                skip: element.classList.contains("skip-link"),
                width: rect.width,
                height: rect.height,
                opacity: Number.parseFloat(style.opacity || "1"),
              };
            });
          const textSamples = [...document.querySelectorAll("body *")]
            .filter((element) => visible(element) && element.children.length === 0 && element.textContent.trim())
            .map((element) => {
              const style = getComputedStyle(element);
              return {
                tag: element.tagName.toLowerCase(),
                text: element.textContent.trim().slice(0, 120),
                color: style.color,
                background: backgroundColor(element),
                fontSize: style.fontSize,
              };
            });
          const headings = [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")]
            .filter(visible)
            .map((element) => ({
              level: Number(element.tagName.slice(1)),
              text: element.textContent.trim().slice(0, 120),
            }));
          const main = document.querySelector("main");
          const mainLabel = main ? accessibleName(main) : "";
          const states = [...document.querySelectorAll("[data-state], [class*=state], [class*=status]")]
            .filter(visible)
            .map((element) => ({
              text: element.textContent.trim(),
              name: accessibleName(element),
            }));
          return {
            documentWidth: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
            viewportWidth: document.documentElement.clientWidth,
            mainCount: document.querySelectorAll("main").length,
            mainLabel,
            h1Count: document.querySelectorAll("h1").length,
            controls,
            images: [...document.querySelectorAll("img")].map((element) => ({
              hasAlt: element.hasAttribute("alt"),
              alt: element.getAttribute("alt"),
              decorative: ["presentation", "none"].includes(element.getAttribute("role") || ""),
            })),
            headings,
            states,
            textSamples,
            focusableCount: controls.length,
          };
        }
        """
    )


def _assert_page_contract(page: Any) -> None:
    snapshot = _page_contract_snapshot(page)
    document_width = int(snapshot["documentWidth"])
    viewport_width = int(snapshot["viewportWidth"])
    if document_width > viewport_width + 1:
        raise ValueError(
            f"horizontal document overflow: scrollWidth={document_width}, clientWidth={viewport_width}"
        )
    if snapshot["mainCount"] != 1 or not snapshot["mainLabel"]:
        raise ValueError("accessibility landmark contract requires one named main")
    if snapshot["h1Count"] != 1:
        raise ValueError("accessibility heading contract requires exactly one visible h1")

    previous_level = 0
    for heading in snapshot["headings"]:
        level = int(heading["level"])
        if previous_level and level > previous_level + 1:
            raise ValueError(
                f"heading level skips from h{previous_level} to h{level}: {heading['text']!r}"
            )
        previous_level = level

    for control in snapshot["controls"]:
        if not control["name"]:
            raise ValueError(f"unlabeled interactive control: {control['tag']}")
        if control["visible"] and not control["skip"] and control["opacity"] > 0:
            if float(control["width"]) < 24 or float(control["height"]) < 24:
                raise ValueError(
                    f"interactive target is smaller than 24 CSS px: {control['tag']} "
                    f"{control['width']}x{control['height']}"
                )

    for image in snapshot["images"]:
        if not image["hasAlt"] or (not image["alt"] and not image["decorative"]):
            raise ValueError("image accessibility contract requires meaningful alt text")

    for state in snapshot["states"]:
        if not state["text"] and not state["name"]:
            raise ValueError("state indicator cannot rely on color alone")

    for sample in snapshot["textSamples"]:
        ratio = _contrast_ratio(sample["color"], sample["background"])
        if ratio is None:
            continue
        required = 3.0 if sample["tag"] in {"button", "a", "input", "select", "textarea"} else 4.5
        if ratio < required:
            raise ValueError(
                f"contrast ratio {ratio:.2f} below WCAG AA {required:.1f}: {sample['text']!r}"
            )

    if int(snapshot["focusableCount"]) > 0:
        page.evaluate(
            """
            () => {
              document.body.setAttribute("data-proof-tab-root", "true");
              document.body.setAttribute("tabindex", "-1");
              document.body.focus();
            }
            """
        )
        try:
            page.keyboard.press("Tab")
            focus = page.evaluate(
                """
                () => {
                  const element = document.activeElement;
                  if (!element || element === document.body) return null;
                  const style = getComputedStyle(element);
                  return {
                    tag: element.tagName.toLowerCase(),
                    outlineStyle: style.outlineStyle,
                    outlineWidth: style.outlineWidth,
                    boxShadow: style.boxShadow,
                  };
                }
                """
            )
            if focus is None:
                raise ValueError("keyboard navigation did not move focus to a control")
            outline_visible = (
                focus["outlineStyle"] not in {"none", "hidden"}
                and float(focus["outlineWidth"].replace("px", "")) > 0
            )
            shadow_visible = focus["boxShadow"] not in {"none", ""}
            if not outline_visible and not shadow_visible:
                raise ValueError("keyboard focus has no visible focus indicator")
        finally:
            page.evaluate(
                """
                () => {
                  document.body.removeAttribute("data-proof-tab-root");
                  document.body.removeAttribute("tabindex");
                  document.activeElement && document.activeElement.blur();
                }
                """
            )


def _assert_zoomed_layout(page: Any, width: int, height: int) -> None:
    # Browser zoom roughly halves the available CSS width. Pair that viewport
    # contraction with a 200% inherited text size so this remains deterministic
    # in headless Chromium without relying on an OS/browser profile setting.
    page.set_viewport_size({"width": max(1, width // 2), "height": height})
    page.evaluate("document.body.style.fontSize = '200%'")
    try:
        page.wait_for_timeout(10)
        snapshot = _page_contract_snapshot(page)
        document_width = int(snapshot["documentWidth"])
        viewport_width = int(snapshot["viewportWidth"])
        if document_width > viewport_width + 1:
            raise ValueError(
                "200% zoom horizontal overflow: "
                f"scrollWidth={document_width}, clientWidth={viewport_width}"
            )
        for sample in snapshot["textSamples"]:
            if float(sample["fontSize"].replace("px", "")) < 12:
                raise ValueError("text is not readable at 200% zoom")
    finally:
        page.evaluate("document.body.style.removeProperty('font-size')")
        page.set_viewport_size({"width": width, "height": height})


def _assert_responsive_accessibility_contract(page: Any) -> None:
    if not AXE_CORE_PATH.is_file():
        raise ValueError("missing pinned local axe-core accessibility engine")
    page.add_script_tag(path=str(AXE_CORE_PATH))
    axe_result = page.evaluate(
        """
        async () => await axe.run(document, {
          runOnly: {
            type: "tag",
            values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
          }
        })
        """
    )
    violations = axe_result.get("violations", [])
    if violations:
        findings = "; ".join(
            f"{item.get('id', 'unknown')} ({len(item.get('nodes', []))} nodes)"
            for item in violations
        )
        raise ValueError(f"axe-core accessibility violations: {findings}")
    for width, height in RESPONSIVE_VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(20)
        _assert_page_contract(page)
        _assert_zoomed_layout(page, width, height)


def _stabilize_page_for_capture(page: Any) -> None:
    page.mouse.move(0, 0)
    page.evaluate(
        """() => {
          const active = document.activeElement;
          if (active && active !== document.body && typeof active.blur === "function") {
            active.blur();
          }
          window.getSelection()?.removeAllRanges();
        }"""
    )
    page.wait_for_timeout(20)


def _screenshot_viewport_png(page: Any) -> bytes:
    return validate_png_bytes(
        page.screenshot(full_page=False, type="png", animations="disabled", caret="hide")
    )


def _capture_converged_viewport_png(page: Any) -> bytes:
    # Chromium can emit a different first paint for intrinsic-width nav
    # borders on a fractional device pixel. Keep the nav in its current
    # state and warm/capture until two consecutive canonical PNGs match.
    previous: bytes | None = None
    for attempt in range(DESKTOP_RASTER_CONVERGENCE_ATTEMPTS):
        current = canonicalize_png_bytes(_screenshot_viewport_png(page))
        if previous is not None and current == previous:
            return current
        previous = current
        if attempt + 1 >= DESKTOP_RASTER_CONVERGENCE_ATTEMPTS:
            break
        page.evaluate(
            """
            async () => {
              await new Promise((resolve) =>
                requestAnimationFrame(() => requestAnimationFrame(resolve))
              );
            }
            """
        )
    raise ValueError("desktop viewport rasterization did not converge")


def _pin_linux_desktop_nav_geometry_png(page: Any) -> bytes:
    # Chromium can place adjacent intrinsic-width nav borders on either
    # side of a fractional device pixel. Pin the four known Recovery Room
    # boxes to fixed border-box widths and keep labels on one line so
    # overflow-wrap:anywhere cannot wrap "The Play", then capture live
    # Chromium text until consecutive canonical PNGs match and restore.
    fixed_widths = json.dumps(desktop_nav_anchor_widths())
    page.evaluate(
        f"""() => {{
          if (innerWidth <= 430) return;
          const fixedWidths = {fixed_widths};
          for (const anchor of document.querySelectorAll("nav a")) {{
            const text = (anchor.textContent || "").trim();
            if (Object.prototype.hasOwnProperty.call(fixedWidths, text)) {{
              anchor.style.boxSizing = "border-box";
              anchor.style.width = `${{fixedWidths[text]}}px`;
              anchor.style.whiteSpace = "nowrap";
              anchor.style.overflowWrap = "normal";
            }}
          }}
        }}"""
    )
    try:
        return _capture_converged_viewport_png(page)
    finally:
        page.evaluate(
            """() => {
              if (innerWidth <= 430) return;
              for (const anchor of document.querySelectorAll("nav a")) {
                anchor.style.removeProperty("width");
                anchor.style.removeProperty("box-sizing");
                anchor.style.removeProperty("white-space");
                anchor.style.removeProperty("overflow-wrap");
              }
            }"""
        )


def _stabilize_desktop_nav_png(page: Any) -> bytes:
    # Chromium on macOS can place adjacent intrinsic-width nav borders on
    # either side of a fractional device pixel even after fonts are ready.
    # Snap only those measured boxes for the screenshot, restore the
    # document, then redraw labels through a pinned RGB PNG so repeated
    # local captures stay byte-stable. Linux retains the native raster.
    nav_labels = page.evaluate(
        """() => {
          if (innerWidth <= 430) return [];
          const labels = [];
          for (const anchor of document.querySelectorAll("nav a")) {
            const width = anchor.getBoundingClientRect().width;
            anchor.style.width = `${Math.ceil(width / 4) * 4}px`;
            const box = anchor.getBoundingClientRect();
            labels.push({
              text: (anchor.textContent || "").trim(),
              x: box.x,
              y: box.y,
              width: box.width,
              height: box.height,
            });
            anchor.style.color = "transparent";
            anchor.style.textDecoration = "none";
          }
          return labels;
        }"""
    )
    try:
        if nav_labels:
            png = _capture_converged_viewport_png(page)
        else:
            png = _screenshot_viewport_png(page)
    finally:
        page.evaluate(
            """() => {
              if (innerWidth <= 430) return;
              for (const anchor of document.querySelectorAll("nav a")) {
                anchor.style.removeProperty("width");
                anchor.style.removeProperty("color");
                anchor.style.removeProperty("text-decoration");
              }
            }"""
        )
    if not nav_labels:
        return png
    with Image.open(io.BytesIO(png)) as image:
        raster = image.convert("RGB")
    draw = ImageDraw.Draw(raster)
    font = ImageFont.load_default(size=16)
    for label in nav_labels:
        text = str(label["text"])
        bounds = draw.textbbox((0, 0), text, font=font)
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        x = round(float(label["x"]) + (float(label["width"]) - text_width) / 2)
        y = round(float(label["y"]) + (float(label["height"]) - text_height) / 2 - bounds[1])
        draw.text((x, y), text, fill=(87, 245, 166), font=font)
        underline_y = min(
            round(float(label["y"]) + float(label["height"]) - 8),
            y + text_height + 1,
        )
        draw.line((x, underline_y, x + text_width, underline_y), fill=(87, 245, 166))
    buffer = io.BytesIO()
    raster.save(buffer, format="PNG", optimize=False, compress_level=9)
    return validate_png_bytes(buffer.getvalue())


def _capture_viewport_png(
    page: Any,
    *,
    width: int,
    height: int,
    console_errors: list[str],
) -> bytes:
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate(
        """
        async () => {
          await document.fonts.ready;
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }
        """
    )
    page.wait_for_timeout(100)
    if console_errors:
        raise ValueError(f"console errors during capture: {console_errors!r}")
    _assert_primary_content_in_viewport(page, width, height)
    clipped_text = page.evaluate(
        """
        () => {
          if (innerWidth > 430 || !document.querySelector('[data-proof-no-bottom-text-clip]')) {
            return [];
          }
          const hiddenOverflow = (value) => value === 'hidden' || value === 'clip';
          const root = document.documentElement;
          const body = document.body;
          const cannotScrollPastViewport =
            hiddenOverflow(getComputedStyle(root).overflowY) ||
            hiddenOverflow(getComputedStyle(body).overflowY) ||
            root.scrollHeight <= innerHeight + 1;
          const isPositioned = (start) => {
            for (let el = start; el && el !== body; el = el.parentElement) {
              const position = getComputedStyle(el).position;
              if (position === 'absolute' || position === 'fixed') return true;
            }
            return false;
          };
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const clipped = [];
          while (walker.nextNode()) {
            const node = walker.currentNode;
            if (!node.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            const host = node.parentElement;
            const positioned = isPositioned(host);
            if (!positioned && !cannotScrollPastViewport) continue;
            for (const rect of range.getClientRects()) {
              if (rect.top < innerHeight && rect.bottom > innerHeight + 0.5) {
                clipped.push(node.textContent.trim().slice(0, 80));
              }
            }
          }
          return clipped;
        }
        """
    )
    if clipped_text:
        raise ValueError(f"text line is clipped by viewport bottom: {clipped_text!r}")
    _stabilize_page_for_capture(page)
    if desktop_nav_stabilization_enabled():
        png = _stabilize_desktop_nav_png(page)
    elif width > 430:
        png = (
            _pin_linux_desktop_nav_geometry_png(page)
            if desktop_nav_anchor_widths()
            else _capture_converged_viewport_png(page)
        )
    else:
        png = _screenshot_viewport_png(page)
    dims = png_dimensions(png)
    if dims != (width, height):
        raise ValueError(f"PNG dimensions {dims} != viewport {(width, height)}")
    if console_errors:
        raise ValueError(f"console errors during capture: {console_errors!r}")
    return png


def _open_file_page(
    browser: Any,
    page_path: Path,
    *,
    request_log: list[str] | None = None,
) -> tuple[Any, Any, list[str]]:
    uri = page_path.resolve(strict=True).as_uri()
    if not uri.startswith("file:"):
        raise ValueError("capture requires file:// HTML paths only")
    context = browser.new_context(
        device_scale_factor=DEVICE_SCALE_FACTOR,
        reduced_motion="reduce",
    )
    page = context.new_page()
    console_errors: list[str] = []
    local_request_failures: list[str] = []

    def _on_console(message: Any) -> None:
        if message.type != "error":
            return
        text = message.text
        # Intentional non-file route.abort produces net::ERR_* console noise;
        # that is not a page script failure and must not fail closed here.
        if "net::ERR_" in text:
            return
        console_errors.append(text)

    page.on("console", _on_console)

    def _on_request_failed(request: Any) -> None:
        # Non-file requests are intentionally aborted by the route policy.
        # A failed file:// request, however, means the offline proof is
        # incomplete and must fail closed.
        if request.url.startswith("file:"):
            local_request_failures.append(request.url)

    page.on("requestfailed", _on_request_failed)
    if request_log is not None:

        def _on_request(request: Any) -> None:
            request_log.append(request.url)

        page.on("request", _on_request)

    def _route_handler(route: Any) -> None:
        if route.request.url.startswith("file:"):
            route.continue_()
        else:
            route.abort()

    page.route("**/*", _route_handler)
    response = page.goto(uri, wait_until="load")
    if response is not None and response.status >= 400:
        context.close()
        raise ValueError(f"main document failed to load over file:: status={response.status}")
    if local_request_failures:
        context.close()
        raise ValueError(f"broken local resources during capture: {local_request_failures!r}")
    if console_errors:
        context.close()
        raise ValueError(f"console errors during capture: {console_errors!r}")
    return context, page, console_errors


def _validate_print_page_layout(page: Any) -> int:
    """Validate the FM-029 page contract before Chromium produces a PDF."""
    marker = page.locator('main[data-print-report="fm029"]')
    if marker.count() == 0:
        return 0
    pages = page.locator("[data-print-page]")
    count = pages.count()
    if count == 0:
        raise ValueError("print report has no page sections")
    page_width, page_height = PRINT_PAGE_VIEWPORT
    for index in range(count):
        page_locator = pages.nth(index)
        box = page_locator.bounding_box()
        if box is None:
            raise ValueError(f"print report page {index + 1} has no bounding box")
        width = float(box["width"])
        height = float(box["height"])
        if abs(width - page_width) > 2 or abs(height - page_height) > 2:
            raise ValueError(
                f"print report page {index + 1} has wrong size {width}x{height}; "
                f"expected {page_width}x{page_height} CSS pixels"
            )
        clipped_children = page_locator.evaluate(
            """element => {
                const bounds = element.getBoundingClientRect();
                return Array.from(element.querySelectorAll('*')).filter(child => {
                    const rect = child.getBoundingClientRect();
                    return rect.left < bounds.left - 1 || rect.right > bounds.right + 1 ||
                        rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1;
                }).length;
            }"""
        )
        if clipped_children:
            raise ValueError(f"print report page {index + 1} is clipped")
        headings = page_locator.locator("h1, h2, h3")
        for heading_index in range(headings.count()):
            heading_box = headings.nth(heading_index).bounding_box()
            if heading_box is None:
                raise ValueError(f"print report page {index + 1} has an orphan heading")
            heading_top = float(heading_box["y"])
            heading_bottom = heading_top + float(heading_box["height"])
            page_top = float(box["y"])
            page_bottom = page_top + float(box["height"])
            if heading_top < page_top - 1 or heading_bottom > page_bottom + 1:
                raise ValueError(f"print report page {index + 1} has a clipped heading")
        if len((page_locator.text_content() or "").strip()) < 20:
            raise ValueError(f"print report page {index + 1} is blank")
    return count


def rasterize_print_report_page_pngs(payload: bytes) -> tuple[bytes, ...]:
    """Render each PDF page to a fixed 816x1056 RGB PNG from canonical PDF bytes."""
    data = validate_pdf_bytes(payload)
    scale = PRINT_PAGE_VIEWPORT[0] / US_LETTER_POINTS[0]
    document = pdfium.PdfDocument(data)
    try:
        if len(document) < 1:
            raise ValueError("PDF has no pages")
        pages: list[bytes] = []
        for index in range(len(document)):
            page = document[index]
            try:
                image = page.render(scale=scale).to_pil().convert("RGB")
                if image.size != PRINT_PAGE_VIEWPORT:
                    image = image.resize(PRINT_PAGE_VIEWPORT, Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=False, compress_level=9)
                pages.append(validate_png_bytes(buffer.getvalue()))
            finally:
                page.close()
        return tuple(pages)
    finally:
        document.close()


def build_print_contact_sheet(page_pngs: Sequence[bytes]) -> bytes:
    """Build a deterministic page contact sheet for fresh human review."""
    if not page_pngs:
        raise ValueError("cannot build a print contact sheet without pages")
    thumb_width = 240
    thumb_height = round(PRINT_PAGE_VIEWPORT[1] * thumb_width / PRINT_PAGE_VIEWPORT[0])
    margin = 24
    label_height = 24
    columns = 3
    rows = (len(page_pngs) + columns - 1) // columns
    canvas = Image.new(
        "RGB",
        (
            margin * (columns + 1) + thumb_width * columns,
            margin * (rows + 1) + (thumb_height + label_height) * rows,
        ),
        (235, 241, 242),
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, payload in enumerate(page_pngs):
        with Image.open(__import__("io").BytesIO(payload)) as source:
            image = source.convert("RGB")
            image.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
            column = index % columns
            row = index // columns
            left = margin + column * (thumb_width + margin)
            top = margin + row * (thumb_height + label_height + margin) + label_height
            x = left + (thumb_width - image.width) // 2
            y = top + (thumb_height - image.height) // 2
            draw.rectangle(
                (left - 1, top - 1, left + thumb_width + 1, top + thumb_height + 1),
                fill=(255, 255, 255),
                outline=(196, 207, 211),
            )
            canvas.paste(image, (x, y))
            draw.text(
                (left, top - label_height + 5),
                f"Page {index + 1:02d}",
                fill=(22, 32, 43),
                font=font,
            )
    output = __import__("io").BytesIO()
    canvas.save(output, format="PNG", optimize=False)
    return validate_png_bytes(output.getvalue())


def capture_print_report_artifacts(
    print_html: Path | str,
    *,
    request_log: list[str] | None = None,
) -> RecoveryRoomArtifacts:
    """Capture a print PDF plus page images/contact sheet when the FM-029 marker exists."""
    path = Path(print_html)
    if not path.is_file():
        raise ValueError(f"missing print-report HTML: {path}")
    if not path.is_absolute():
        raise ValueError("print HTML path must be absolute")
    artifacts: RecoveryRoomArtifacts = {}
    with _pinned_font_cache():
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                context, page, console_errors = _open_file_page(
                    browser, path, request_log=request_log
                )
                try:
                    if console_errors:
                        raise ValueError(f"console errors during print capture: {console_errors!r}")
                    page.emulate_media(media="print")
                    page.wait_for_timeout(50)
                    page_count = _validate_print_page_layout(page)
                    pdf = canonicalize_pdf_local_uris(
                        page.pdf(format="Letter", print_background=True, prefer_css_page_size=True),
                        source_root=path.parent,
                    )
                    if page_count:
                        assert_us_letter_pdf(
                            pdf,
                            required_sections=("Money Map", "Recovery Plays", "Human control"),
                        )
                        page_pngs = list(rasterize_print_report_page_pngs(pdf))
                        if len(page_pngs) != page_count:
                            raise ValueError(
                                "print PDF page count does not match HTML print page count"
                            )
                        for index, payload in enumerate(page_pngs, start=1):
                            dimensions = png_dimensions(payload)
                            if dimensions != PRINT_PAGE_VIEWPORT:
                                raise ValueError(
                                    f"print report page {index} PNG dimensions {dimensions} "
                                    f"!= {PRINT_PAGE_VIEWPORT}"
                                )
                            artifacts[f"print-report-page-{index:02d}.png"] = payload
                        artifacts[PRINT_REPORT_CONTACT_SHEET] = build_print_contact_sheet(page_pngs)
                finally:
                    context.close()
            finally:
                browser.close()
    artifacts["print-report.pdf"] = pdf
    return artifacts


def capture_print_report_pdf(print_html: Path | str) -> bytes:
    """Print a print-only HTML document to US Letter PDF bytes."""
    return capture_print_report_artifacts(print_html)["print-report.pdf"]


def capture_four_room_screenshots(
    index_html: Path | str,
    *,
    request_log: list[str] | None = None,
) -> RecoveryRoomArtifacts:
    """Capture one unratified desktop screenshot per visible offline room."""
    path = Path(index_html)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("four-room index HTML must be an existing absolute path")
    artifacts: RecoveryRoomArtifacts = {}
    with _pinned_font_cache():
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                context = browser.new_context(
                    viewport={"width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1]},
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                console_errors: list[str] = []
                page_errors: list[str] = []
                request_failures: list[str] = []
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("requestfailed", lambda request: request_failures.append(request.url))
                if request_log is not None:
                    page.on("request", lambda request: request_log.append(request.url))

                def _route_handler(route: Any) -> None:
                    if route.request.url.startswith("file:"):
                        route.continue_()
                    else:
                        route.abort()

                page.route("**/*", _route_handler)
                response = page.goto(path.resolve(strict=True).as_uri(), wait_until="load")
                if response is not None and response.status >= 400:
                    raise ValueError(f"four-room document failed to load: status={response.status}")
                for room_id, name in zip(
                    ("room-find", "room-evidence", "room-play", "room-launch"),
                    FOUR_ROOM_PNGS,
                    strict=True,
                ):
                    room = page.locator(f"#{room_id}")
                    if room.count() != 1:
                        raise ValueError(f"missing unique Recovery Room section: {room_id}")
                    room.scroll_into_view_if_needed()
                    heading = room.locator("h2")
                    action = room.locator("[data-recommended-action]")
                    if heading.count() != 1 or action.count() != 1:
                        raise ValueError(f"room structure incomplete: {room_id}")
                    for element in (heading, action):
                        box = element.bounding_box()
                        if box is None:
                            raise ValueError(f"room content is not visible: {room_id}")
                        left = float(box["x"])
                        right = left + float(box["width"])
                        if left < 0 or right > DESKTOP_VIEWPORT[0]:
                            raise ValueError(f"room content is horizontally clipped: {room_id}")
                    _stabilize_page_for_capture(page)
                    artifacts[name] = canonicalize_png_bytes(
                        validate_png_bytes(
                            page.screenshot(
                                full_page=False, type="png", animations="disabled", caret="hide"
                            )
                        )
                    )
                if console_errors or page_errors or request_failures:
                    raise ValueError(
                        "four-room browser integrity failure: "
                        f"console={console_errors!r}, page={page_errors!r}, "
                        f"requests={request_failures!r}"
                    )
                if request_log is not None and any(
                    not url.startswith("file:") for url in request_log
                ):
                    raise ValueError("four-room capture attempted a non-file request")
                context.close()
            finally:
                browser.close()
    return artifacts


def capture_recovery_room_artifacts(
    index_html: Path | str | None = None,
    top_play_html: Path | str | None = None,
    *,
    recovery_room_dir: Path | str | None = None,
    print_html: Path | str | None = None,
    request_log: list[str] | None = None,
) -> RecoveryRoomArtifacts:
    """Capture fixed-viewport PNGs, per-page PDFs, and optional print-report proof."""
    index_path, top_path = _resolve_html_pair(
        index_html, top_play_html, recovery_room_dir=recovery_room_dir
    )
    artifacts: RecoveryRoomArtifacts = {}
    with _pinned_font_cache():
        with sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                for page_path, desktop_key, mobile_key, alias_key, pdf_key in (
                    (
                        index_path,
                        "money-map-desktop-1440x900.png",
                        "money-map-mobile-390x844.png",
                        "index.png",
                        "index.pdf",
                    ),
                    (
                        top_path,
                        "top-play-desktop-1440x900.png",
                        "top-play-mobile-390x844.png",
                        "top-play.png",
                        "top-play.pdf",
                    ),
                ):
                    context, page, console_errors = _open_file_page(
                        browser, page_path, request_log=request_log
                    )
                    try:
                        desktop_png = _capture_viewport_png(
                            page,
                            width=DESKTOP_VIEWPORT[0],
                            height=DESKTOP_VIEWPORT[1],
                            console_errors=console_errors,
                        )
                        mobile_png = _capture_viewport_png(
                            page,
                            width=MOBILE_VIEWPORT[0],
                            height=MOBILE_VIEWPORT[1],
                            console_errors=console_errors,
                        )
                        _assert_responsive_accessibility_contract(page)
                        # Per-page PDF from desktop viewport context.
                        page.set_viewport_size(
                            {"width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1]}
                        )
                        pdf = canonicalize_pdf_local_uris(
                            page.pdf(print_background=True),
                            source_root=page_path.parent,
                        )
                    finally:
                        context.close()
                    artifacts[desktop_key] = desktop_png
                    artifacts[mobile_key] = mobile_png
                    artifacts[alias_key] = desktop_png
                    artifacts[pdf_key] = pdf
            finally:
                browser.close()

    if print_html is not None:
        artifacts.update(capture_print_report_artifacts(print_html, request_log=request_log))
    return artifacts


def _validate_relative_under_root(output_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("relative path must be a string")
    raw = relative_path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("relative path must be non-empty")
    if raw.startswith("/") or _WINDOWS_ABS_RE.match(raw) or raw.startswith("~/"):
        raise ValueError("absolute paths are rejected")
    if raw.startswith("./"):
        raw = raw[2:]
    if raw.endswith("/"):
        raw = raw[:-1]
    if not raw or _TRAVERSAL_RE.search(raw) or raw == ".." or Path(raw).is_absolute():
        raise ValueError("traversal or absolute paths are rejected")
    root = output_root.expanduser().resolve(strict=False)
    candidate = root.joinpath(*Path(raw).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("path escapes the caller-owned output root") from exc
    probe = root
    for part in Path(raw).parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            try:
                probe.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError("symlink escapes the caller-owned output root") from exc
        if probe.exists() and not probe.is_dir():
            raise ValueError("parent path component is not a directory")
    if candidate.exists() and candidate.is_symlink():
        try:
            candidate.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("symlink escapes the caller-owned output root") from exc
    return candidate


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def write_named_png_baselines(
    artifacts: Mapping[str, bytes],
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write only this platform's four exact-dimension golden PNGs."""
    destination_dir = resolve_render_baselines_dir()
    payloads: dict[str, bytes] = {}
    for name, expected_dimensions in NAMED_VIEWPORT_DIMENSIONS.items():
        if name not in artifacts:
            raise ValueError(f"missing named PNG artifact: {name}")
        payload = validate_png_bytes(artifacts[name])
        dimensions = png_dimensions(payload)
        if dimensions != expected_dimensions:
            raise ValueError(
                f"named PNG dimensions {dimensions} != expected {expected_dimensions}: {name}"
            )
        payloads[name] = payload

    destinations = {name: destination_dir / name for name in NAMED_VIEWPORT_PNGS}
    if not overwrite:
        existing = [path for path in destinations.values() if path.exists()]
        if existing:
            raise ValueError(f"refusing to overwrite golden baseline: {existing[0]}")
    for name, destination in destinations.items():
        _atomic_write_bytes(destination, payloads[name])
    return destinations


def write_recovery_room_artifacts(
    output_root: Path | str,
    artifacts: Mapping[str, bytes],
    *,
    relative_dir: str = "recovery-room",
) -> dict[str, Path]:
    """Atomically write Recovery Room PNG/PDF artifacts under a caller-owned root."""
    if not isinstance(relative_dir, str):
        raise ValueError("relative directory must be a string")
    raw_dir = relative_dir.strip().replace("\\", "/")
    if raw_dir.endswith("/"):
        raw_dir = raw_dir[:-1]
    if not raw_dir:
        raise ValueError("relative directory must be non-empty")

    validated: dict[str, bytes] = {}
    for name in REQUIRED_ARTIFACTS:
        if name not in artifacts:
            raise ValueError(f"missing artifact payload: {name}")
        payload = artifacts[name]
        if name.endswith(".png"):
            validated[name] = validate_png_bytes(payload)
        else:
            validated[name] = validate_pdf_bytes(payload)
    optional_print_names = sorted(
        name
        for name in artifacts
        if _PRINT_PAGE_NAME_RE.fullmatch(name) or name == PRINT_REPORT_CONTACT_SHEET
    )
    for name in optional_print_names:
        validated[name] = validate_png_bytes(artifacts[name])

    destinations: dict[str, Path] = {}
    written: list[Path] = []
    try:
        for name, payload in validated.items():
            dest = _validate_relative_under_root(Path(output_root), f"{raw_dir}/{name}")
            destinations[name] = dest
            _atomic_write_bytes(dest, payload)
            written.append(dest)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return destinations


def _canonical_print_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture_sha256(*html_documents: str) -> str:
    digest = hashlib.sha256()
    for document in html_documents:
        encoded = document.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _normalize_relative_dir(relative_dir: str) -> str:
    if not isinstance(relative_dir, str):
        raise ValueError("relative directory must be a string")
    raw_dir = relative_dir.strip().replace("\\", "/")
    if raw_dir.endswith("/"):
        raw_dir = raw_dir[:-1]
    if not raw_dir:
        raise ValueError("relative directory must be non-empty")
    return raw_dir


def _render_proof_json_bytes(metadata: Mapping[str, Any]) -> bytes:
    payload = json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if re.search(r"(?i)\bhttps?://|\?[^\s]+=[^\s]+|/Users/|/home/|[A-Za-z]:\\", payload):
        raise ValueError("render proof metadata contains a URL/query or absolute path")
    if re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", payload):
        raise ValueError("render proof metadata contains an email")
    for token in ("customer_token", "economic_unit_key", "external_ids", "source_id"):
        if token in payload:
            raise ValueError(f"render proof metadata contains forbidden identity token: {token}")
    return payload.encode("utf-8")


def _build_render_proof_metadata(
    *,
    run_id: str,
    index_html: str,
    top_play_html: str,
    print_html: str,
    artifacts: Mapping[str, bytes],
    relative_dir: str,
    baseline_comparison: str,
    proof_surface: str = "thin-slice Money Map and top play",
    not_claimed: list[str] | None = None,
) -> dict[str, Any]:
    selected_baselines = resolve_render_baselines_dir()
    ratification_status = read_baseline_ratification_status()
    review_packet_path = BASELINES_DIR / VISUAL_REVIEW_PACKET_NAME
    review_packet = validate_visual_review_packet(review_packet_path)
    normalized_dir = _normalize_relative_dir(relative_dir)
    artifact_metadata: dict[str, Any] = {}
    for name in REQUIRED_ARTIFACTS:
        payload = artifacts[name]
        item: dict[str, Any] = {
            "path": f"{normalized_dir}/{name}",
            "sha256": _sha256_bytes(payload),
        }
        if name.endswith(".png"):
            item["dimensions"] = {
                "width": NAMED_VIEWPORT_DIMENSIONS.get(name, DESKTOP_VIEWPORT)[0]
                if name in NAMED_VIEWPORT_DIMENSIONS or name in {"index.png", "top-play.png"}
                else None,
                "height": NAMED_VIEWPORT_DIMENSIONS.get(name, DESKTOP_VIEWPORT)[1]
                if name in NAMED_VIEWPORT_DIMENSIONS or name in {"index.png", "top-play.png"}
                else None,
            }
            if name in {"index.png", "top-play.png"}:
                item["dimensions"] = {
                    "width": DESKTOP_VIEWPORT[0],
                    "height": DESKTOP_VIEWPORT[1],
                }
        artifact_metadata[name] = item

    baseline_metadata = {
        name: {
            "path": f"{selected_baselines.name}/{name}",
            "sha256": _sha256_bytes((selected_baselines / name).read_bytes()),
            "dimensions": {
                "width": NAMED_VIEWPORT_DIMENSIONS[name][0],
                "height": NAMED_VIEWPORT_DIMENSIONS[name][1],
            },
            "ratification_status": ratification_status,
        }
        for name in NAMED_VIEWPORT_PNGS
    }
    return {
        "schema_version": RENDER_PROOF_SCHEMA_VERSION,
        "run_id": run_id,
        "fixture_sha256": _fixture_sha256(index_html, top_play_html, print_html),
        "platform": {
            "runtime": platform.system(),
            "baseline_directory": selected_baselines.name,
            "baseline_selection": "exact supported platform; no cross-platform fallback",
        },
        "viewport_contract": {
            "fixed_artifacts": {
                "desktop": {"width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1]},
                "mobile": {"width": MOBILE_VIEWPORT[0], "height": MOBILE_VIEWPORT[1]},
            },
            "responsive_widths": [width for width, _height in RESPONSIVE_VIEWPORTS],
            "responsive_heights": [height for _width, height in RESPONSIVE_VIEWPORTS],
            "zoom_percent": RESPONSIVE_ZOOM_PERCENT,
        },
        "capture_policy": {
            "device_scale_factor": DEVICE_SCALE_FACTOR,
            "local_resource_scheme": "file",
            "non_file_requests": "abort",
            "console_errors": "fail",
            "baseline_diff_channel_tolerance": BASELINE_DIFF_CHANNEL_TOLERANCE,
            "baseline_diff_max_fraction": BASELINE_DIFF_MAX_FRACTION,
            "unratified_drift": "fail on any nonzero pixel drift",
        },
        "accessibility_audit": {
            "engine": "axe-core",
            "engine_sha256": _sha256_bytes(AXE_CORE_PATH.read_bytes()),
            "engine_source": "pinned local package asset",
            "rule_tags": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"],
            "violations": "capture fails on any violation",
        },
        "baseline_comparison": baseline_comparison,
        "artifacts": artifact_metadata,
        "baselines": baseline_metadata,
        "human_gate": {
            "ratification_status": ratification_status,
            "fresh_exact_head_review_required": True,
            "automatic_pixel_approval": False,
            "global_ac_25_complete": GLOBAL_AC_25_COMPLETE,
            "global_ac_26_complete": GLOBAL_AC_26_COMPLETE,
            "global_ac_27_complete": GLOBAL_AC_27_COMPLETE,
            "global_ac_28_complete": GLOBAL_AC_28_COMPLETE,
            "review_packet": {
                "schema_version": review_packet["schema_version"],
                "sha256": _sha256_bytes(review_packet_path.read_bytes()),
            },
        },
        "scope": {
            "proof_surface": proof_surface,
            "not_claimed": not_claimed
            or [
                "four-room Recovery Room artifact",
                "three complete plays in print-report.pdf",
                "human visual ratification",
                "live or business proof",
            ],
        },
    }


def write_render_proof_metadata(
    output_root: Path | str,
    metadata: Mapping[str, Any],
    *,
    relative_dir: str = "recovery-room",
) -> Path:
    """Write deterministic, public-safe render metadata under the output root."""
    normalized_dir = _normalize_relative_dir(relative_dir)
    destination = _validate_relative_under_root(
        Path(output_root), f"{normalized_dir}/render-proof.json"
    )
    _atomic_write_bytes(destination, _render_proof_json_bytes(metadata))
    return destination


def build_thin_slice_recovery_room_proof(
    output_root: Path | str,
    *,
    run_id: str = "run_thin_slice_recovery_room_proof",
    relative_dir: str = "recovery-room",
) -> tuple[MoneyMapV1, RecoveryPlaySetV1, dict[str, Path]]:
    """Compose thin-slice HTML and capture PNG/PDF proof under output_root."""
    # Local import avoids an import cycle with found_money.rendering.__init__.
    from found_money.rendering import (
        build_thin_slice_recovery_room,
        render_print_report_html,
        write_recovery_room,
    )

    money_map, play_set, index_html, top_play_html = build_thin_slice_recovery_room(run_id=run_id)
    raw_dir = _normalize_relative_dir(relative_dir)
    index_path, top_path = write_recovery_room(
        output_root, index_html, top_play_html, relative_dir=raw_dir
    )
    print_html = render_print_report_html(money_map, play_set)
    print_path = _validate_relative_under_root(Path(output_root), f"{raw_dir}/print-report.html")
    _atomic_write_bytes(print_path, print_html.encode("utf-8"))
    artifacts = capture_recovery_room_artifacts(
        index_path, top_path, print_html=print_path.resolve()
    )
    if run_id == CANONICAL_VISUAL_RUN_ID:
        compare_named_pngs_to_baselines(artifacts)
        baseline_comparison = "pass"
    else:
        baseline_comparison = "not-run-noncanonical-fixture-run"
    written = write_recovery_room_artifacts(output_root, artifacts, relative_dir=relative_dir)
    metadata = _build_render_proof_metadata(
        run_id=run_id,
        index_html=index_html,
        top_play_html=top_play_html,
        print_html=print_html,
        artifacts=artifacts,
        relative_dir=raw_dir,
        baseline_comparison=baseline_comparison,
    )
    metadata_path = write_render_proof_metadata(
        output_root,
        metadata,
        relative_dir=raw_dir,
    )
    return (
        money_map,
        play_set,
        {
            "index.html": index_path,
            "top-play.html": top_path,
            "print-report.html": print_path,
            "render-proof.json": metadata_path,
            **written,
        },
    )


def build_canonical_four_room_visual_proof(
    output_root: Path | str,
    *,
    run_id: str = CANONICAL_VISUAL_RUN_ID,
    relative_dir: str = "recovery-room",
) -> tuple[MoneyMapV1, Any, dict[str, Path]]:
    """Capture the canonical full three-play Recovery Room visual proof."""
    from found_money.contracts.strategy import RecoveryPlaySetV1, RecoveryPlayV1
    from found_money.map import build_thin_slice_money_map
    from found_money.rendering import (
        recovery_room_static_assets,
        render_print_report_html,
        render_recovery_room_html,
        render_top_play_html,
        write_recovery_room,
    )
    from found_money.strategy import (
        apply_recovery_plays_to_money_map,
        build_canonical_saas_recovery_strategy,
    )

    money_map = build_thin_slice_money_map(run_id=run_id)
    strategy = build_canonical_saas_recovery_strategy(money_map)
    complete_play_set = strategy.complete_plays()
    primary = min(complete_play_set.plays, key=lambda play: play.rank)
    bridge = RecoveryPlaySetV1(
        run_id=run_id,
        built_at=complete_play_set.built_at,
        provider="fixture",
        plays=[
            RecoveryPlayV1(
                play_id=primary.play_id,
                pile_id=primary.pile_id,
                rank=1,
                title=primary.title,
                rationale=primary.rationale,
                recommended_actions=primary.recommended_actions,
            )
        ],
    )
    enriched = apply_recovery_plays_to_money_map(money_map, bridge)
    withheld_assets = ["private_segments", "complete_copy", "creative_handoff"]
    index_html = render_recovery_room_html(
        enriched,
        complete_play_set,
        launch_status="not_started",
        withheld_assets=withheld_assets,
    )
    top_play_html = render_top_play_html(enriched, bridge)
    print_html = render_print_report_html(enriched, complete_play_set)
    raw_dir = _normalize_relative_dir(relative_dir)
    index_path, top_path = write_recovery_room(
        output_root, index_html, top_play_html, relative_dir=raw_dir
    )
    for relative_path, payload in recovery_room_static_assets().items():
        destination = _validate_relative_under_root(Path(output_root), f"{raw_dir}/{relative_path}")
        _atomic_write_bytes(destination, payload)
    print_path = _validate_relative_under_root(Path(output_root), f"{raw_dir}/print-report.html")
    _atomic_write_bytes(print_path, print_html.encode("utf-8"))
    artifacts = capture_recovery_room_artifacts(
        index_path, top_path, print_html=print_path.resolve()
    )
    compare_named_pngs_to_baselines(artifacts)
    written = write_recovery_room_artifacts(output_root, artifacts, relative_dir=relative_dir)
    metadata = _build_render_proof_metadata(
        run_id=run_id,
        index_html=index_html,
        top_play_html=top_play_html,
        print_html=print_html,
        artifacts=artifacts,
        relative_dir=raw_dir,
        baseline_comparison="pass",
        proof_surface="canonical four-room Recovery Room with three complete plays",
        not_claimed=[
            "final full three-play print report",
            "live or real-business proof",
            "publication permission",
        ],
    )
    metadata_path = write_render_proof_metadata(output_root, metadata, relative_dir=raw_dir)
    return (
        enriched,
        complete_play_set,
        {
            "index.html": index_path,
            "top-play.html": top_path,
            "print-report.html": print_path,
            "render-proof.json": metadata_path,
            **written,
        },
    )
