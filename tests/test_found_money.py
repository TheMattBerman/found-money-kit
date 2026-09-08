import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from found_money.mapping import map_row
from found_money.engine import classify, decide, evaluate
from found_money.io import load_records, public_report, write_report
from found_money.redaction import assert_public_safe
from found_money.hubspot import (
    paginate,
    associations,
    fetch_private_run,
    canonical_records,
    write_private_run,
    HubSpotError,
)


def row(**extra):
    base = {
        "id": "x",
        "email": "x@example.invalid",
        "trial_end_days": 2,
        "subscribed": True,
        "consent_email": True,
        "value": 200,
    }
    base.update(extra)
    return map_row(base)


class FoundMoneyTests(unittest.TestCase):
    def test_suppression_priority(self):
        d = decide(row(subscribed=False, dnc=True, invalid_contact=True))
        self.assertEqual(d.reasons, ["unsubscribed"])

    def test_no_send_enforcement(self):
        report = public_report([row()])
        self.assertTrue(report["no_send_enforced"])
        self.assertEqual(report["proof_tier"], "built")
        self.assertEqual(report["queue"][0]["status"], "review")

    def test_missing_value_is_explicitly_unquantified(self):
        report = public_report([row(value=None)])
        value = report["queue"][0]["opportunity_value"]
        self.assertEqual((value["low"], value["high"]), (0, 0))
        self.assertEqual(value["basis"], "unquantified: no observed source value")

    def test_deterministic_and_dedupe(self):
        records = [row(id="z"), row(id="a")]
        self.assertEqual(len(evaluate(records)), 1)
        self.assertEqual(
            [d.token for d in evaluate(records)],
            [d.token for d in evaluate(list(reversed(records)))],
        )

    def test_classification_math(self):
        self.assertEqual(classify(row(trial_end_days=None, closed_lost_days=30)), "closed_lost")
        self.assertEqual(
            classify(row(trial_end_days=None, closed_lost_days=None, last_purchase_days=90)),
            "lapsed_customer_reorder_gap",
        )
        self.assertEqual(
            classify(
                row(
                    trial_end_days=None,
                    closed_lost_days=None,
                    last_purchase_days=None,
                    last_activity_days=45,
                )
            ),
            "stale_lead",
        )

    def test_redaction_scan(self):
        report = public_report([row()])
        self.assertTrue(assert_public_safe(report))
        self.assertNotIn("x@example.invalid", json.dumps(report))

    def test_end_to_end_synthetic(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "queue.json"
            write_report(
                load_records(root / "fixtures" / "synthetic_contacts.json"),
                Path(tmp),
                "queue.json",
            )
            payload = json.loads(output.read_text())
            self.assertTrue(payload["queue"])
            self.assertTrue(payload["no_send_enforced"])

    def test_hubspot_pagination_contract(self):
        calls = []

        def transport(request):
            calls.append(request)
            return (
                {"results": [{"id": str(len(calls))}], "paging": {"next": {"after": "next"}}}
                if len(calls) == 1
                else {"results": [{"id": "2"}]}
            )

        result = paginate("/crm/v3/objects/contacts", "test-token", transport)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(c.method == "GET" for c in calls))

    def test_missing_hubspot_token(self):
        with self.assertRaises(HubSpotError):
            paginate("/crm/v3/objects/contacts", None, lambda r: {})

    def test_association_contract_is_get_only(self):
        calls = []
        associations(
            "contacts",
            "1",
            "deals",
            "token",
            lambda request: calls.append(request) or {"results": []},
        )
        self.assertEqual(calls[0].method, "GET")
        self.assertIn("/associations/deals", calls[0].full_url)

    def test_raw_hubspot_writes_are_private_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            private = workspace / "private-runs"
            allowed = private / "run"
            self.assertTrue(write_private_run({}, allowed, private).exists())
            with self.assertRaises(HubSpotError):
                write_private_run({}, workspace / "outside", private)

    def test_private_root_is_caller_owned_not_package_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed_style = Path(tmp) / "project"
            private = installed_style / "private-runs"
            result = write_private_run({"fixture": True}, private / "run", private)
            self.assertEqual(result.parent, (private / "run").resolve())

    def test_hubspot_analyze_fixture_contract(self):
        fixture = json.loads(
            (Path(__file__).resolve().parent.parent / "fixtures" / "hubspot_pages.json").read_text()
        )
        calls = []

        def transport(request):
            calls.append(request)
            url = request.full_url
            for deal_id, contact_ids in fixture["associations"].items():
                if f"/objects/deals/{deal_id}/associations/contacts" in url:
                    return {"results": [{"toObjectId": x} for x in contact_ids]}
            if "/contacts?" in url:
                return {"results": fixture["contacts"]}
            if "/deals?" in url:
                return {"results": fixture["deals"]}
            raise AssertionError(url)

        # The fixture exercises a portal-specific custom stage, which is supplied by
        # configuration rather than hardcoded into the shipped candidate filter.
        with mock.patch.dict(os.environ, {"FOUND_MONEY_EXTRA_DEAL_STAGES": "custom_open_stage"}):
            raw = fetch_private_run("fixture-token", transport)
        self.assertEqual(raw["candidate_contact_ids"], ["c1", "c2"])
        rows = canonical_records(raw, today=__import__("datetime").date(2026, 7, 28))
        self.assertEqual(len(rows), 3)  # two associations plus one unassociated-deal review record
        self.assertTrue(any(row["last_activity_days"] == 88 for row in rows))
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["c1"]["trial_end_days"], 57)
        self.assertEqual(classify(map_row(by_id["c1"])), "expired_trial")
        self.assertIsNone(by_id["c2"]["trial_end_days"])
        self.assertNotEqual(classify(map_row(by_id["c2"])), "expired_trial")
        self.assertTrue(any(row.get("deal_stage") == "closed_lost" for row in rows))
        report = public_report([map_row(row) for row in rows])
        self.assertTrue(report["no_send_enforced"])
        self.assertIn("unquantified: no observed source value", json.dumps(report))
        self.assertTrue(all(call.method == "GET" for call in calls))
        self.assertTrue(
            any("properties=" in call.full_url and "email" in call.full_url for call in calls)
        )


if __name__ == "__main__":
    unittest.main()
