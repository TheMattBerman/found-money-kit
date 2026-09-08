"""Deployment-specific privacy terms and pipeline ids stay outside source defaults."""

import pytest

from found_money.contracts.public_proof import _reject_banned_blob
from found_money.hubspot import candidate_deal_stages
from found_money.release import _findings_for_text


def test_configured_client_names_are_literal_case_insensitive_and_runtime_bound(monkeypatch):
    monkeypatch.setenv("FOUND_MONEY_CLIENT_NOUNS", "PrivateCanary LLC, Example+Private, PrivateCo.")
    for name in ("privatecanary llc", "EXAMPLE+PRIVATE", "PrivateCo."):
        with pytest.raises(ValueError, match="client names"):
            _reject_banned_blob(name)
        assert any(f.category == "proper_noun" for f in _findings_for_text("README.md", name))
    assert not any(
        f.category == "proper_noun" for f in _findings_for_text("README.md", "ExamplePrivate")
    )
    monkeypatch.delenv("FOUND_MONEY_CLIENT_NOUNS")
    _reject_banned_blob("PrivateCanary LLC")
    with pytest.raises(ValueError, match="client names"):
        _reject_banned_blob("ExampleClientCo")


def test_custom_pipeline_stages_are_opt_in_and_keep_standard_defaults(monkeypatch):
    monkeypatch.delenv("FOUND_MONEY_EXTRA_DEAL_STAGES", raising=False)
    assert candidate_deal_stages() == {"qualifiedtobuy", "closedlost"}
    monkeypatch.setenv(
        "FOUND_MONEY_EXTRA_DEAL_STAGES", " custom_open_stage, CUSTOM_TWO, ,closedlost "
    )
    assert candidate_deal_stages() == {
        "qualifiedtobuy",
        "closedlost",
        "custom_open_stage",
        "custom_two",
    }
