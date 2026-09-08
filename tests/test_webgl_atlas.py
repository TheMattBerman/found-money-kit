"""Unit tests for Three.js 3D WebGL Opportunity Atlas Generator (Layer 2)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


from found_money.rendering import _public_safe_scan
from found_money.rendering.webgl_atlas import render_webgl_atlas_html
from found_money.strategy import build_thin_slice_strategized_money_map


def _demo_saas_atlas_payload() -> dict[str, Any]:
    """Demo SaaS benchmark dataset ($444,694, 3 tiers, unpriced deals)."""
    return {
        "schema_version": "money-map.v1",
        "run_id": "run_demo_saas_gym",
        "built_at": "2026-09-02T12:00:00.000Z",
        "strategy_stage": "completed",
        "identified_opportunity_minor": {"usd": Decimal("44469400")},
        "basis_counts_by_currency": {
            "usd": {
                "observed_event_count": 5,
                "modeled_event_count": 189,
                "unquantified_event_count": 14,
            }
        },
        "piles": [
            {
                "pile_id": "payment_rescue",
                "rank": 1,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("307800"),
                "customer_count": 5,
                "economic_unit_count": 5,
                "confidence_class": "observed",
                "why_recoverable": "5 loyal customers whose monthly charges failed in the last 14 days. They didn't cancel—their cards expired.",
                "navigation": {
                    "target_id": "pile/payment_rescue/usd",
                    "state": "available",
                    "play_id": "play_payment_rescue",
                    "next_action": "launch_recovery_campaign",
                },
            },
            {
                "pile_id": "closed_lost_stale_deal",
                "rank": 2,
                "value_basis": "modeled_opportunity",
                "currency": "usd",
                "selected_value_minor": Decimal("24314400"),
                "customer_count": 89,
                "economic_unit_count": 89,
                "confidence_class": "recorded",
                "why_recoverable": "89 prospects who reached a late sales stage over the last 18 months and went cold.",
                "navigation": {
                    "target_id": "pile/closed_lost_stale_deal/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review_when_primary_completes",
                },
            },
            {
                "pile_id": "canceled_customer",
                "rank": 3,
                "value_basis": "modeled_opportunity",
                "currency": "usd",
                "selected_value_minor": Decimal("19847200"),
                "customer_count": 189,
                "economic_unit_count": 189,
                "confidence_class": "modeled",
                "why_recoverable": "189 quiet churned accounts multiplied by the business's verified 16-month average tenure.",
                "navigation": {
                    "target_id": "pile/canceled_customer/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review_when_primary_completes",
                },
            },
        ],
    }


def _service_payload() -> dict[str, Any]:
    """Service scenario ($387.00 across 4 piles, 1 lit)."""
    return {
        "schema_version": "money-map.v1",
        "run_id": "run_service_studio",
        "built_at": "2026-09-02T12:00:00.000Z",
        "identified_opportunity_minor": {"usd": Decimal("38700")},
        "basis_counts_by_currency": {
            "usd": {
                "observed_event_count": 5,
                "modeled_event_count": 0,
                "unquantified_event_count": 1,
            }
        },
        "piles": [
            {
                "pile_id": "disappeared_high_value_customer",
                "rank": 1,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("22000"),
                "customer_count": 1,
                "economic_unit_count": 1,
                "confidence_class": "observed",
                "why_recoverable": "1 high-value member who quietly went inactive after regular studio bookings.",
                "navigation": {
                    "target_id": "pile/disappeared_high_value_customer/usd",
                    "state": "available",
                    "play_id": "play_vip_return",
                    "next_action": "launch_recovery_campaign",
                },
            },
            {
                "pile_id": "silent_proposal",
                "rank": 2,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("12500"),
                "customer_count": 1,
                "economic_unit_count": 1,
                "confidence_class": "observed",
                "why_recoverable": "1 corporate proposal sent with no decision recorded.",
                "navigation": {
                    "target_id": "pile/silent_proposal/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review",
                },
            },
            {
                "pile_id": "canceled_customer",
                "rank": 3,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("2400"),
                "customer_count": 1,
                "economic_unit_count": 1,
                "confidence_class": "observed",
                "why_recoverable": "1 recently canceled studio membership.",
                "navigation": {
                    "target_id": "pile/canceled_customer/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review",
                },
            },
            {
                "pile_id": "trial_no_convert",
                "rank": 4,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("1800"),
                "customer_count": 2,
                "economic_unit_count": 2,
                "confidence_class": "observed",
                "why_recoverable": "2 intro pass attendees who never converted to full membership.",
                "navigation": {
                    "target_id": "pile/trial_no_convert/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review",
                },
            },
        ],
    }


def _ecommerce_payload() -> dict[str, Any]:
    """Ecommerce scenario ($291.00 across 2 piles)."""
    return {
        "schema_version": "money-map.v1",
        "run_id": "run_ecom_apparel",
        "built_at": "2026-09-02T12:00:00.000Z",
        "identified_opportunity_minor": {"usd": Decimal("29100")},
        "piles": [
            {
                "pile_id": "disappeared_high_value_customer",
                "rank": 1,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("18500"),
                "customer_count": 2,
                "economic_unit_count": 2,
                "confidence_class": "observed",
                "why_recoverable": "Top repeat buyers who stopped ordering past their 45-day replenishment window.",
                "navigation": {
                    "target_id": "pile/vip/usd",
                    "state": "available",
                    "play_id": "play_vip",
                    "next_action": "launch",
                },
            },
            {
                "pile_id": "overdue_reorder",
                "rank": 2,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("10600"),
                "customer_count": 2,
                "economic_unit_count": 2,
                "confidence_class": "observed",
                "why_recoverable": "Consumable product reorders overdue by 30 days.",
                "navigation": {
                    "target_id": "pile/reorder/usd",
                    "state": "deferred",
                    "play_id": None,
                    "next_action": "review",
                },
            },
        ],
    }


def _single_pile_degenerate_payload() -> dict[str, Any]:
    """Degenerate single-pile scenario ($1,250.00, 1 pile Citadel)."""
    return {
        "schema_version": "money-map.v1",
        "run_id": "run_single_citadel",
        "built_at": "2026-09-02T12:00:00.000Z",
        "identified_opportunity_minor": {"usd": Decimal("125000")},
        "piles": [
            {
                "pile_id": "disappeared_high_value_customer",
                "rank": 1,
                "value_basis": "observed_face_value",
                "currency": "usd",
                "selected_value_minor": Decimal("125000"),
                "customer_count": 1,
                "economic_unit_count": 1,
                "confidence_class": "observed",
                "why_recoverable": "Single high-value enterprise subscriber account in delinquent churn.",
                "navigation": {
                    "target_id": "pile/vip/usd",
                    "state": "available",
                    "play_id": "play_vip",
                    "next_action": "launch",
                },
            },
        ],
    }


def test_render_webgl_atlas_saas_benchmark():
    """Verify $444,694 headline, 3 confidence tiers, 3D pins, action beacon, and inspection card."""
    payload = _demo_saas_atlas_payload()
    html = render_webgl_atlas_html(payload)

    assert "<!DOCTYPE html>" in html
    assert "Found Money — 3D Opportunity Atlas" in html
    assert "$444,694" in html
    assert "Opportunity Atlas" in html
    assert "opportunity areas" in html
    assert "opportunity areas" in html
    assert "14 unpriced records" in html

    # 3 Confidence Tiers
    assert "OBSERVED" in html
    assert "$3,078" in html
    assert "MODELED" in html
    assert "$198,472" in html
    assert "RECORDED" in html
    assert "$243,144" in html

    # Biome pins
    assert "PAYMENT RESCUE CHASM" in html
    assert "CLOSED-LOST RIDGE" in html
    assert "CHURNED SUBSCRIBER SHALLOWS" in html
    assert "UNPRICED EXPEDITION WILDERNESS" in html

    # Pulsing Hero Action Beacon
    assert "hero-beacon" in html
    assert "beacon-pill" in html
    assert "START HERE" in html
    assert "$3,078" in html

    # Founder-Friendly Inspection Card
    assert "inspection-card" in html
    assert "card-badge" in html
    assert "Campaign available" in html
    assert "THE SITUATION" in html
    assert "THE PLAN" in html

    assert "WHY IT'S QUEUED" in html
    assert "MISSING VALUE" in html

    # Navigation and unpriced values remain grounded in the supplied artifacts.
    assert "Open campaign &rarr;" in html
    assert "14 Unpriced Records" in html
    assert "deal-size-input" not in html
    assert "applyDealSize()" not in html
    assert "Scheduled Sequence:" not in html
    assert "cash sitting on the table today" not in html

    # Strict Rule: NO TECHNICAL AUDIT JARGON anywhere in the UI
    assert "Withheld (no proof)" not in html
    assert "Value Weight" not in html
    assert "factors-matrix" not in html
    assert "factor-row" not in html


def test_render_webgl_atlas_service_scenario():
    """Verify 4-pile service scenario renders with 1 available action beacon."""
    payload = _service_payload()
    html = render_webgl_atlas_html(payload)

    assert "$387" in html
    assert "SOVEREIGN VIP CITADEL" in html
    assert "SILENT PROPOSAL BASIN" in html
    assert "CHURNED SUBSCRIBER SHALLOWS" in html
    assert "hero-beacon" in html
    assert "$220" in html
    assert "START HERE" in html
    assert "VIP Disappeared Accounts • $220" in html


def test_render_webgl_atlas_ecommerce_scenario():
    """Verify 2-pile ecommerce scenario renders cleanly."""
    payload = _ecommerce_payload()
    html = render_webgl_atlas_html(payload)

    assert "$291" in html
    assert "SOVEREIGN VIP CITADEL" in html
    assert "OVERDUE REORDER SHOALS" in html
    assert "hero-beacon" in html
    assert "$185" in html


def test_render_webgl_atlas_single_pile_degenerate():
    """Verify degenerate 1-pile citadel scenario renders without error."""
    payload = _single_pile_degenerate_payload()
    html = render_webgl_atlas_html(payload)

    assert "$1,250" in html
    assert "SOVEREIGN VIP CITADEL" in html
    assert "hero-beacon" in html
    assert "START HERE" in html
    assert "opportunity areas" in html


def test_render_webgl_atlas_thin_slice_money_map():
    """Verify thin-slice MoneyMapV1 object compiles into WebGL atlas."""
    money_map, _packet, _play_set = build_thin_slice_strategized_money_map()
    html = render_webgl_atlas_html(money_map)

    assert money_map.run_id is not None
    assert "Found Money — 3D Opportunity Atlas" in html
    assert "hero-beacon" in html
    assert "inspection-card" in html


def test_render_webgl_atlas_zero_network_calls():
    """Verify 100% offline guarantee: zero external CDN scripts or stylesheet calls."""
    payload = _demo_saas_atlas_payload()
    html = render_webgl_atlas_html(payload)

    # Must have inlined scripts, no external scripts
    assert "<script src=" not in html.lower()
    assert 'href="http://' not in html.lower()
    assert 'href="https://' not in html.lower()
    assert "cdnjs.cloudflare.com" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html

    # Base64 texture must be present
    assert "data:image/jpeg;base64," in html

    # Three.js, OrbitControls, Tween.js must be present inline
    assert "THREE.WebGLRenderer" in html
    assert "THREE.OrbitControls" in html
    assert "TWEEN.Tween" in html


def test_render_webgl_atlas_public_safety():
    """Verify generated HTML passes strict repository public safety scan."""
    for payload in [
        _demo_saas_atlas_payload(),
        _service_payload(),
        _ecommerce_payload(),
        _single_pile_degenerate_payload(),
    ]:
        html = render_webgl_atlas_html(payload)
        _public_safe_scan(html)

        for token in (
            "customer_token",
            "economic_unit_key",
            "external_ids",
            "inv_failed_001",
            "cus_synth_001",
            "qualifying_evidence",
        ):
            assert token not in html


def test_render_webgl_atlas_cinematic_motion_and_responsive():
    """Verify camera controls, fog lifting magic, and responsive viewport support."""
    payload = _demo_saas_atlas_payload()
    html = render_webgl_atlas_html(payload)

    # Camera setup & swoop-in routine
    assert "camera.position.set(0, 52, 58)" in html
    assert "function cinematicCloudSwoop()" in html
    assert "THREE.FogExp2(" in html

    # Pan to region, card update, and reset controls
    assert "function focusRegion(" in html
    assert "function updateInspectionCard(" in html
    assert "function resetView()" in html

    # State 3 Magic: Fog lifting & headline update
    assert "window.applyDealSize" not in html
    assert "newTotalMinor" not in html

    # Responsive mobile layout
    assert "@media (max-width: 768px)" in html
    assert "sheet-handle" in html


def test_render_webgl_atlas_fragment_and_recovery_room_integration():
    """Verify the embedded 3D Opportunity Atlas fragment integrates into the Recovery Room."""
    from found_money.rendering import build_thin_slice_recovery_room, render_recovery_room_html
    from found_money.rendering.webgl_atlas import render_webgl_atlas_fragment

    money_map, play_set, _, _ = build_thin_slice_recovery_room(run_id="run_atlas_test")

    # 1. Fragment generation
    fragment = render_webgl_atlas_fragment({"money_map": money_map, "play_set": play_set})
    assert '<div id="recovery-room-atlas"' in fragment
    assert 'class="recovery-room-atlas"' in fragment
    assert (
        '<script src="assets/three.min.js"></script>' in fragment
    )  # sibling asset, still zero network
    assert "ICP_REGIONS" in fragment  # pile data injected at runtime

    # 2. Integrated recovery room HTML
    room_html = render_recovery_room_html(money_map, play_set)
    assert '<div id="recovery-room-atlas"' in room_html
    assert 'section id="room-find"' in room_html
    assert 'section id="room-play"' in room_html
    assert "Open campaign &rarr;" in room_html
    # With fake fallback emails removed, a play set without generated plays has no email deck
    assert '"emails": []' in fragment


def test_atlas_fallback_removed_for_all_13_pile_ids():
    """AC-5: for any pile without a generated play, no BIOME_ANCHORS email appears in rendered atlas."""
    from typing import get_args
    from found_money.contracts.map import PileId
    from found_money.contracts.strategy import RecoveryPlaySetV1
    from found_money.rendering.webgl_atlas import (
        _extract_atlas_context,
        render_webgl_atlas_fragment,
    )

    all_pile_ids = get_args(PileId)
    assert len(all_pile_ids) == 13

    for pile_id in all_pile_ids:
        raw_piles = [
            {
                "pile_id": pile_id,
                "selected_value_minor": 10000,
                "currency": "usd",
                "rank": 1,
                "value_basis": "observed_face_value",
                "confidence_class": "observed",
                "why_recoverable": "Test situation.",
            }
        ]
        empty_play_set = RecoveryPlaySetV1(
            run_id="run_atlas_13_test",
            built_at="2026-08-10T12:00:00Z",
            provider="stub",
            plays=[],
        )
        ctx = _extract_atlas_context(
            {
                "money_map": {"piles": raw_piles, "run_id": "run_atlas_13_test"},
                "play_set": empty_play_set,
            }
        )
        matched = next(p for p in ctx["piles"] if p["id"] == pile_id)
        assert matched["emails"] == [], (
            f"Pile {pile_id} must have empty email deck without generated play"
        )

        fragment = render_webgl_atlas_fragment(
            {
                "money_map": {"piles": raw_piles, "run_id": "run_atlas_13_test"},
                "play_set": empty_play_set,
            }
        )
        # Check that no email subject from the old BIOME_ANCHORS appears
        for forbidden_subject in (
            "billing glitch?",
            "the billing link",
            "closing this out",
            "quick question",
            "how others handled it",
            "closing this file",
            "feedback",
            "new updates",
            "quick note",
            "checking in",
            "more time?",
            "setup question",
            "annual review",
            "proposal question",
            "restock?",
            "still looking?",
            "reschedule?",
        ):
            assert f'"subject": "{forbidden_subject}"' not in fragment, (
                f"Found fallback email '{forbidden_subject}' in fragment for pile {pile_id}"
            )
            assert f'"subject":"{forbidden_subject}"' not in fragment, (
                f"Found fallback email '{forbidden_subject}' in fragment for pile {pile_id}"
            )


def test_reduced_motion_map_keeps_labels_visible_and_mobile_content_reachable(tmp_path):
    from playwright.sync_api import sync_playwright

    path = tmp_path / "map.html"
    path.write_text(render_webgl_atlas_html(_demo_saas_atlas_payload()))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": 1440, "height": 844}, reduced_motion="reduce"
            )
            page.goto(path.as_uri())
            page.wait_for_selector('[data-texture-ready="true"]')
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 844})
                # Resizing can return before viewport units and ResizeObserver
                # update the atlas. Wait for that state, not a wall-clock delay.
                page.wait_for_function(
                    """() => {
                      const atlas = document.querySelector('#recovery-room-atlas');
                      const frame = document.querySelector('.atlas-canvas-container');
                      const canvas = frame.querySelector('canvas');
                      return atlas.clientWidth === innerWidth &&
                        canvas.clientWidth === frame.clientWidth &&
                        document.documentElement.scrollWidth <= innerWidth;
                    }""",
                    timeout=5000,
                )
                assert page.locator(".region-pin:visible").count() > 0
                assert page.locator(".atlas-canvas-container").evaluate(
                    """canvas => {
                      const frame = canvas.getBoundingClientRect();
                      return [...canvas.querySelectorAll('.region-pin')].every(pin => {
                        const rect = pin.getBoundingClientRect();
                        return !rect.width || (rect.left >= frame.left && rect.right <= frame.right);
                      });
                    }"""
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator('.atlas-choice[aria-pressed="true"]').count() == 1
                page.locator(".atlas-choice").last.click()
                assert page.locator('.atlas-choice[aria-pressed="true"]').count() == 1
                card = page.locator(".inspection-card")
                card.scroll_into_view_if_needed()
                assert card.is_visible()
                assert card.evaluate("e => e.getBoundingClientRect().top < innerHeight")
        finally:
            browser.close()
