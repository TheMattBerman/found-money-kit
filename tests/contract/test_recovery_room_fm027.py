"""FM-027 four-room offline Recovery Room contract tests."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from found_money.contracts.rendering import RecoveryRoomManifestV1
from found_money.contracts.strategy import RecoveryPlaySetV1, RecoveryPlayV1
from found_money.map import build_thin_slice_money_map
from found_money.redaction import assert_public_safe
from found_money.rendering import (
    build_recovery_room_manifest,
    capture_four_room_screenshots,
    recovery_room_static_assets,
    render_recovery_room_html,
)
from found_money.rendering.proof import FOUR_ROOM_PNGS, PNG_SIGNATURE
from found_money.strategy import (
    apply_recovery_plays_to_money_map,
    build_canonical_saas_recovery_strategy,
    build_thin_slice_strategized_money_map,
)

RENDER_TEST_CONTRACT = "found-money-render-v1"
ROOMS = (
    ("room-find", "The Find"),
    ("room-evidence", "The Evidence"),
    ("room-play", "The Play"),
    ("room-launch", "The Launch"),
)


def _full_room() -> tuple[str, object, object]:
    money_map = build_thin_slice_money_map(run_id="run_fm027_room")
    strategy = build_canonical_saas_recovery_strategy(money_map)
    primary = min(strategy.recovery_plays.plays, key=lambda play: play.rank)
    bridge = RecoveryPlaySetV1(
        run_id=money_map.run_id,
        built_at=strategy.recovery_plays.built_at,
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
    html = render_recovery_room_html(
        enriched,
        strategy.recovery_plays,
        launch_status="not_started",
        withheld_assets=["complete_copy", "creative_handoff"],
    )
    return html, enriched, strategy.recovery_plays


def _materialize(tmp_path: Path, html: str) -> Path:
    index = tmp_path / "index.html"
    index.write_text(html, encoding="utf-8")
    for relative, payload in recovery_room_static_assets().items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    for relative in (
        "money-map.json",
        "recovery-plays.json",
        "provenance/run-manifest.json",
        "launch-pack/manifest.json",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    return index.resolve()


def test_four_visible_rooms_have_exact_structure_and_complete_three_play_content():
    html, money_map, play_set = _full_room()
    assert html.count("<main>") == 1
    assert html.count('<nav aria-label="Recovery Room">') == 1
    for room_id, heading in ROOMS:
        assert html.count(f'id="{room_id}"') == 1
        section = re.search(rf'<section id="{room_id}".*?</section>', html, re.DOTALL)
        assert section is not None
        assert f">{heading}</h2>" in section.group(0)
        assert section.group(0).count("data-recommended-action") == 1
        markup_only = re.sub(r"<style[^>]*>.*?</style>", "", section.group(0), flags=re.DOTALL)
        # The atlas inspection card toggles interactive sub-sections via inline
        # display:none; that is UI state, not hidden room prose.
        markup_only = re.sub(
            r'<div class="card-section[^"]*" id="section-[^"]*"[^>]*>.*?(?=<div class="card-section|<div id="|</section>)',
            "",
            markup_only,
            flags=re.DOTALL,
        )
        assert ' hidden" ' not in markup_only
        assert 'style="display:none;" aria-hidden="true"' not in markup_only.replace(" ", "")
        assert "<details" not in markup_only

    for play in play_set.plays:
        assert len(re.findall(rf'\sid="{re.escape(play.play_id)}"', html)) == 1
        for marker in (
            play.campaign_strategy.text,
            play.value_basis.text,
            next(rung.mechanism.text for rung in play.offer_ladder.rungs if rung.role == "core"),
            play.primary_cta.text,
            play.tracking.success_event.text,
            play.creative_big_idea.text,
        ):
            assert marker in html
        for card in play.concept_cards:
            assert len(re.findall(rf'\sid="{re.escape(card.card_id)}"', html)) == 1
            assert card.hook.text in html
            assert card.production_requirements[0] in html
    assert "not_started" in html
    assert "complete_copy" in html
    assert_public_safe(html)
    assert money_map.to_canonical_json()


def test_thin_and_deferred_states_are_explicit_without_fabricated_content():
    thin_map, _packet, thin_plays = build_thin_slice_strategized_money_map(run_id="run_fm027_thin")
    before_map = thin_map.to_canonical_json()
    before_plays = thin_plays.to_canonical_json()
    thin_html = render_recovery_room_html(thin_map, thin_plays)
    assert "Thin fixture play; complete campaign assets remain unavailable." in thin_html
    assert thin_plays.plays[0].play_id in thin_html
    assert thin_map.to_canonical_json() == before_map
    assert thin_plays.to_canonical_json() == before_plays

    plain = build_thin_slice_money_map(run_id="run_fm027_deferred")
    empty = RecoveryPlaySetV1(
        run_id=plain.run_id,
        built_at=thin_plays.built_at,
        provider="fixture",
        plays=[],
    )
    deferred_html = render_recovery_room_html(plain, empty)
    assert "Strategy status: deferred. No generic fallback play was created." in deferred_html
    assert "strategy deferred" in deferred_html
    assert "payment_rescue_dunning" not in deferred_html


def test_manifest_is_stable_hash_bound_and_rejects_unsafe_paths():
    html, _money_map, _play_set = _full_room()
    assets = recovery_room_static_assets()
    first = build_recovery_room_manifest("run_fm027_room", html, assets)
    second = build_recovery_room_manifest("run_fm027_room", html, assets)
    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.room_ids == [room_id for room_id, _heading in ROOMS]
    assert first.room_headings == dict(ROOMS)
    assert [asset.path for asset in first.local_assets] == sorted(assets)
    for asset in first.local_assets:
        assert asset.sha256 == hashlib.sha256(assets[asset.path]).hexdigest()
    payload = first.model_dump(mode="json")
    payload["local_assets"][0]["path"] = "../outside.css"
    with pytest.raises(ValidationError, match="traversal-free"):
        RecoveryRoomManifestV1.model_validate(payload)


@pytest.mark.parametrize("java_script_enabled", [True, False])
def test_file_navigation_and_local_resources_work_with_javascript_on_or_off(
    tmp_path: Path, java_script_enabled: bool
):
    html, _money_map, play_set = _full_room()
    index = _materialize(tmp_path, html)
    requests: list[str] = []
    failures: list[str] = []
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--allow-file-access-from-files", "--force-prefers-reduced-motion"]
        )
        context = browser.new_context(java_script_enabled=java_script_enabled)
        page = context.new_page()
        page.on("request", lambda request: requests.append(request.url))
        page.on("requestfailed", lambda request: failures.append(request.url))
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith("file:") else route.abort()
            ),
        )
        page.goto(index.as_uri(), wait_until="load")
        room_texts: list[str] = []
        for room_id, _heading in ROOMS:
            page.locator(f'nav a[href="#{room_id}"]').click(force=True)
            assert unquote(urlparse(page.url).fragment) == room_id
            room = page.locator(f"#{room_id}")
            assert room.count() == 1
            # The chooser may display one play, but every complete play remains in the DOM.
            room_texts.append(
                " ".join(
                    (room.text_content() if room_id == "room-play" else room.inner_text()).split()
                )
            )
        assert "We found $49.00" in room_texts[0]
        assert "Payment Rescue" in room_texts[0]
        assert all(
            word in room_texts[0].lower() for word in ("observed", "modeled", "unquantified")
        )
        assert "source lineage" in room_texts[1].lower()
        assert all(play.play_id in room_texts[2] for play in play_set.plays)
        for play in play_set.plays:
            for email in play.email_sequence:
                assert email.subject.text in room_texts[2]
                assert email.body.text in room_texts[2]
                assert email.cta.text in room_texts[2]
        assert "not_started" in room_texts[3]
        for link in page.locator("[href], [src]").all():
            target = link.get_attribute("href") or link.get_attribute("src")
            assert target is not None
            assert not target.startswith(("http:", "https:", "//", "/"))
            assert "?" not in target and ".." not in Path(target).parts
            if target.startswith("#"):
                assert page.locator(target).count() == 1
            else:
                assert (tmp_path / target).resolve().is_file()
        assert requests and all(url.startswith("file:") for url in requests)
        assert failures == []
        assert errors == []
        context.close()
        browser.close()


def test_four_room_named_browser_proof(tmp_path: Path):
    html, _money_map, _play_set = _full_room()
    index = _materialize(tmp_path, html)
    requests: list[str] = []
    artifacts = capture_four_room_screenshots(index, request_log=requests)
    assert tuple(artifacts) == FOUR_ROOM_PNGS
    assert all(payload.startswith(PNG_SIGNATURE) for payload in artifacts.values())
    assert requests and all(url.startswith("file:") for url in requests)


def test_javascript_adds_convenience_without_changing_core_room_text(tmp_path: Path):
    html, _money_map, _play_set = _full_room()
    index = _materialize(tmp_path, html)
    snapshots: list[list[str]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--allow-file-access-from-files", "--force-prefers-reduced-motion"]
        )
        for enabled in (True, False):
            context = browser.new_context(java_script_enabled=enabled)
            page = context.new_page()
            page.goto(index.as_uri(), wait_until="load")
            # The Atlas is an optional interactive view of the same money map.
            page.evaluate("document.getElementById('recovery-room-atlas')?.remove()")
            page.evaluate("delete document.documentElement.dataset.playChooser")
            snapshots.append(
                [
                    " ".join(page.locator(f"#{room_id}").inner_text().split())
                    for room_id, _heading in ROOMS
                ]
            )
            context.close()
        browser.close()
    assert snapshots[0] == snapshots[1]
