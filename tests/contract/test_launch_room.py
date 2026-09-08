"""Launch regressions exercised in an ordinary offline browser."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from found_money.build import _fixture_snapshots, _run_id_for, _safe_source_config
from found_money.rendering import recovery_room_static_assets, render_recovery_room_html
from found_money.rendering.webgl_atlas import _extract_atlas_context
from found_money.scenarios import SYNTHETIC_SERVICE_V1, run_scenario_engine


@pytest.fixture
def room(tmp_path: Path):
    safe = _safe_source_config(
        source_mode="fixture", run_mode="public", fixture=SYNTHETIC_SERVICE_V1
    )
    engine = run_scenario_engine(
        run_id=_run_id_for(_fixture_snapshots(SYNTHETIC_SERVICE_V1), safe),
        safe_config=safe,
        fixture_id=SYNTHETIC_SERVICE_V1,
    )
    html = render_recovery_room_html(
        engine.enriched_money_map,
        engine.strategy_run.recovery_plays,
        contribution_ledger=engine.ledger,
    )
    index = tmp_path / "index.html"
    index.write_text(html)
    for relative, payload in recovery_room_static_assets().items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return index, engine


def test_offline_texture_and_responsive_controls_without_security_bypass(room):
    index, _ = room
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda e: errors.append(e.text) if e.type == "error" else None)
        page.goto(index.as_uri())
        page.wait_for_timeout(700)
        assert page.locator("canvas").count() == 1
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert "kind='product'" not in page.locator("main").inner_text()
        choice = page.get_by_role("button", name="Silent Proposals", exact=True)
        choice.click()
        assert choice.get_attribute("aria-pressed") == "true"
        assert "campaign is not available" in page.locator("#card-why-queued").inner_text().lower()
        assert not page.locator("#btn-launch-play").is_visible()
        assert not page.locator("#deal-size-input").count()
        assert not page.locator("#btn-queue-play").is_visible()
        assert errors == []
        browser.close()


@pytest.mark.parametrize("clipboard_success", [True, False])
def test_copy_reports_actual_result_and_keeps_exact_rendered_payload(room, clipboard_success):
    index, _ = room
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        page.add_init_script(
            """Object.defineProperty(navigator, 'clipboard', {value: {
            writeText: async text => { window.copiedPayload = text; %s }
        }});"""
            % ("" if clipboard_success else "throw new Error('denied');")
        )
        page.goto(index.as_uri())
        email = page.locator(".play-selected .play-email").first
        expected = "\n\n".join(
            email.locator(f".play-email-{part}").inner_text().strip()
            for part in ("subject", "body", "cta")
        )
        button = email.get_by_role("button", name="Copy email 1", exact=True)
        button.click()
        assert page.evaluate("window.copiedPayload") == expected
        status = email.get_by_role("status")
        if clipboard_success:
            assert button.inner_text() == "Copied"
            assert "copied to clipboard" in status.inner_text()
        else:
            assert button.inner_text() == "Copy email"
            assert "manually" in status.inner_text()
        browser.close()


def test_chooser_keeps_all_copy_readable_without_javascript(room):
    index, engine = room
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for enabled in (False, True):
            page = browser.new_page(java_script_enabled=enabled, reduced_motion="reduce")
            page.goto(index.as_uri())
            for play in engine.strategy_run.recovery_plays.plays:
                if enabled:
                    page.locator(f'.play-angles a[href="#{play.play_id}"]').click()
                article = page.locator(f"#{play.play_id}")
                assert article.is_visible()
                for email in play.email_sequence:
                    assert email.subject.text in article.inner_text()
                    assert email.body.text in article.inner_text()
            page.close()
        browser.close()


def test_atlas_does_not_combine_currencies_or_turn_data_gaps_into_deals():
    ctx = _extract_atlas_context(
        {
            "identified_opportunity_minor": {"usd": 10000, "eur": 20000},
            "piles": [],
            "named_data_gaps": ["missing activity date"],
        }
    )
    assert ctx["headline"] == "EUR 200.00 / USD 100.00"
    assert [t["amount"] for t in ctx["tiers"]] == ["EUR 200.00", "USD 100.00"]
    assert ctx["unpriced_deals"] == 0
    assert ctx["active_target"] is None
