#!/usr/bin/env python3
"""Generate the illustrative vertical-SaaS dataset.

Deterministic, offline, stdlib-only. One fixed seed produces byte-identical
``hubspot/snapshot.json``, ``stripe/snapshot.json`` and ``README.md`` under
``private-runs/demo-verticalsaas/``.

Field values are chosen to satisfy the real detector conditions in
``found_money/events/library.py`` (and the identity join in
``found_money/identity/__init__.py``), not just the shape of the spec:

* failed payment      -> stripe invoice status ``open`` + ``payment_failed``,
                         and no succeeded payment on the same customer, which
                         would trigger the ``later_payment`` suppression.
* expired trial       -> subscription ``trialing`` + ``converted: false`` +
                         past ``trial_end``, and no ``active``/``paid`` record
                         on the same customer (``reactivation`` suppression).
* trial no convert    -> same, with status ``incomplete_expired``.
* canceled customer   -> subscription ``canceled`` + ``canceled_at``; the Stripe
                         customer row carries no ``status`` so the family fires
                         once, from the subscription, and is not reactivated.
* closed lost         -> deal stage ``closed_lost`` with ``closed_at`` at least
                         ``stale_deal_days`` (30) in the past, associated to a
                         contact, and no other open deal on the same contact
                         (``active_negotiation`` suppression).
* disappeared high    -> succeeded Stripe invoice >= 10_000 minor units paid
  value customer         more than ``lapse_days`` (90) ago, with no later
                         succeeded payment.
* renewal upsell      -> active subscription with ``renewal_at`` inside the
                         30-day renewal window.

Usage::

    python3 scripts/generate_demo_dataset.py
    python3 scripts/generate_demo_dataset.py --verify-detection   # needs found_money
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SEED = 20260827
NOW = datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "private-runs" / "demo-verticalsaas"

# Portal scale reproduced from the shape of the real export.
TARGET_CONTACTS = 1059
TARGET_DEALS = 196

# Cohort targets (spec "Cohorts to produce").
N_ACTIVE_PAYING = 120
N_FAILED_PAYMENT = 12
N_EXPIRED_TRIAL = 10  # subscription status "trialing"      -> expired_trial
N_TRIAL_NO_CONVERT = 5  # subscription status "incomplete_expired" -> trial_no_convert
N_CANCELED = 20
N_CLOSED_LOST = 89
N_LAPSED_HIGH_VALUE = 10
N_RENEWAL_UPSELL = 12
N_SILENT_PROPOSAL = 15

# Remaining deal volume so the deal total lands on TARGET_DEALS.
N_CUSTOM_STAGE_DEALS = 35  # ~18% unlabeled numeric-string stage
N_CLOSED_WON_DEALS = 30
N_OPEN_PIPELINE_DEALS = 27

# Mess targets.
PCT_DEALS_MISSING_AMOUNT = 0.20
PCT_DEALS_MISSING_CURRENCY = 0.20
PCT_CONTACTS_MISSING_ACTIVITY = 0.10
N_NON_USD_DEALS = 4
N_EMAIL_OPT_OUT = 15
N_INVALID_ADDRESS = 8
N_FUTURE_CLOSE_DATES = 4
N_SHARED_KEY_PAIRS = 2  # two contacts, one synthetic_customer_key
N_AMBIGUOUS_DEALS = 3  # one deal, two contacts in different clusters

# $70-$500/month standard, $1,200-$2,500/month multi-location. Minor units.
STANDARD_PRICES_MINOR = (
    7000,
    8900,
    9900,
    11900,
    12900,
    14900,
    16900,
    17900,
    19900,
    22900,
    24900,
    27900,
    29900,
    34900,
    39900,
    44900,
    49900,
)
MULTI_LOCATION_PRICES_MINOR = (120000, 139900, 159900, 179900, 199900, 229900, 249900)
MULTI_LOCATION_SHARE = 0.07

# HubSpot-ish pipeline stages with no human-readable label in the export.
UNLABELED_STAGES = ("custom_open_stage", "custom_stage_b", "custom_stage_c")
MID_PIPELINE_STAGES = ("presentationscheduled", "contractsent", "decisionmakerboughtin")
OPEN_PIPELINE_STAGES = ("appointmentscheduled", "qualifiedtobuy")

# Free-text lead noise observed in the real export. None of these collide with a
# status the detectors or suppression rules read as active/canceled/paid.
LEAD_LIFECYCLE_STAGES = (
    "lead",
    "lead",
    "lead",
    "subscriber",
    "subscriber",
    "marketingqualifiedlead",
    "salesqualifiedlead",
    "opportunity",
    "other",
)
JUNK_LIFECYCLE_STAGES = (
    "bad lead",
    "bad lead - no budget",
    "end user not decision maker",
    "wrong persona - member",
    "do not contact",
    "no response",
    "not a fit",
    "junk",
    "duplicate?",
    "student / not a business",
)
JUNK_LIFECYCLE_SHARE = 0.16


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days: int, hour: int = 12) -> str:
    return _iso((NOW - timedelta(days=days)).replace(hour=hour))


def _days_ahead(days: int, hour: int = 12) -> str:
    return _iso((NOW + timedelta(days=days)).replace(hour=hour))


@dataclass
class Dataset:
    """Accumulates both snapshots plus the bookkeeping the README reports."""

    rng: random.Random
    contacts: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)
    associations: dict[str, list[str]] = field(default_factory=dict)
    customers: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    invoices: list[dict[str, Any]] = field(default_factory=list)
    cohorts: dict[str, list[str]] = field(default_factory=dict)
    mess: dict[str, list[str]] = field(default_factory=dict)
    _counters: Counter[str] = field(default_factory=Counter)

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]:05d}"

    def record(self, cohort: str, identifier: str) -> None:
        self.cohorts.setdefault(cohort, []).append(identifier)

    def note(self, case: str, identifier: str) -> None:
        self.mess.setdefault(case, []).append(identifier)

    # -- builders -----------------------------------------------------------

    def new_key(self) -> str:
        return self.next_id("syn_vs_")

    def add_contact(
        self,
        *,
        key: str,
        lifecycle_stage: str,
        last_activity_at: str | None,
        email_opt_out: bool = False,
        invalid_address: bool = False,
    ) -> str:
        contact_id = self.next_id("hs_ct_")
        properties: dict[str, Any] = {
            "lifecycle_stage": lifecycle_stage,
            "synthetic_customer_key": key,
        }
        if last_activity_at is not None:
            properties["last_activity_at"] = last_activity_at
        properties["email_opt_out"] = email_opt_out
        properties["invalid_address"] = invalid_address
        self.contacts.append({"id": contact_id, "properties": properties})
        return contact_id

    def add_deal(
        self,
        *,
        key: str,
        deal_stage: str,
        contact_ids: list[str],
        created_at: str,
        closed_at: str | None = None,
        amount_minor: int | None = None,
        currency: str | None = "usd",
    ) -> str:
        deal_id = self.next_id("hs_dl_")
        properties: dict[str, Any] = {"deal_stage": deal_stage}
        if amount_minor is not None:
            properties["amount_minor"] = amount_minor
        if currency is not None:
            properties["currency"] = currency
        properties["synthetic_customer_key"] = key
        properties["created_at"] = created_at
        if closed_at is not None:
            properties["closed_at"] = closed_at
        self.deals.append({"id": deal_id, "properties": properties})
        self.associations[deal_id] = list(contact_ids)
        return deal_id

    def add_customer(self, *, key: str, status: str | None) -> str:
        customer_id = self.next_id("cus_vs_")
        record: dict[str, Any] = {"id": customer_id, "synthetic_customer_key": key}
        if status is not None:
            record["status"] = status
        self.customers.append(record)
        return customer_id

    def add_subscription(self, *, customer_id: str, key: str, **fields: Any) -> str:
        subscription_id = self.next_id("sub_vs_")
        record: dict[str, Any] = {
            "id": subscription_id,
            "customer_id": customer_id,
            "synthetic_customer_key": key,
            "billing_interval": "month",
        }
        record.update(fields)
        self.subscriptions.append(record)
        return subscription_id

    def add_invoice(self, *, customer_id: str, key: str, **fields: Any) -> str:
        invoice_id = self.next_id("in_vs_")
        record: dict[str, Any] = {
            "id": invoice_id,
            "customer_id": customer_id,
            "synthetic_customer_key": key,
        }
        record.update(fields)
        self.invoices.append(record)
        return invoice_id

    # -- pricing ------------------------------------------------------------

    def monthly_price_minor(self, *, high_value: bool = False) -> int:
        if high_value:
            if self.rng.random() < 0.4:
                return self.rng.choice(MULTI_LOCATION_PRICES_MINOR)
            return self.rng.choice(STANDARD_PRICES_MINOR[-6:])
        if self.rng.random() < MULTI_LOCATION_SHARE:
            return self.rng.choice(MULTI_LOCATION_PRICES_MINOR)
        return self.rng.choice(STANDARD_PRICES_MINOR)

    def annual_contract_minor(self) -> int:
        return self.monthly_price_minor() * 12


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------


def build_active_paying(data: Dataset) -> None:
    """Paying accounts with no recovery signal at all.

    Renewal dates sit outside the 30-day renewal window and the succeeded
    invoice is newer than the 90-day lapse threshold, so neither
    ``renewal_upsell`` nor ``disappeared_high_value_customer`` fires.
    """
    for _ in range(N_ACTIVE_PAYING):
        key = data.new_key()
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(data.rng.randint(1, 110)),
        )
        customer_id = data.add_customer(key=key, status="active")
        amount = data.monthly_price_minor()
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status="active",
            renewal_at=_days_ahead(data.rng.randint(45, 330)),
            amount_minor=amount,
            currency="usd",
            created_at=_days_ago(data.rng.randint(60, 900)),
        )
        data.add_invoice(
            customer_id=customer_id,
            key=key,
            status="paid",
            collection_outcome="payment_succeeded",
            amount_due_cents=amount,
            currency="usd",
            paid_at=_days_ago(data.rng.randint(3, 80), hour=9),
        )
        data.record("active_paying", contact_id)


def build_failed_payments(data: Dataset) -> None:
    """Open invoices that failed collection inside the last 90 days."""
    for index in range(N_FAILED_PAYMENT):
        key = data.new_key()
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(data.rng.randint(1, 60)),
        )
        customer_id = data.add_customer(key=key, status="active")
        amount = data.monthly_price_minor()
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status="active",
            renewal_at=_days_ahead(data.rng.randint(40, 300)),
            amount_minor=amount,
            currency="usd",
            created_at=_days_ago(data.rng.randint(90, 800)),
        )
        data.add_invoice(
            customer_id=customer_id,
            key=key,
            status="open",
            collection_outcome="payment_failed",
            amount_due_cents=amount,
            currency="usd",
            attempted_at=_days_ago(5 + index * 7 + data.rng.randint(0, 3), hour=10),
        )
        data.record("failed_payment", contact_id)


def build_trials(data: Dataset) -> None:
    """Trials that ended without converting.

    ``trialing`` rows land in ``expired_trial``; ``incomplete_expired`` rows land
    in ``trial_no_convert``. The Stripe customer row deliberately carries no
    ``status`` so the reactivation suppression stays quiet.
    """
    plan = ["trialing"] * N_EXPIRED_TRIAL + ["incomplete_expired"] * N_TRIAL_NO_CONVERT
    for status in plan:
        key = data.new_key()
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="opportunity",
            last_activity_at=_days_ago(data.rng.randint(30, 520)),
        )
        customer_id = data.add_customer(key=key, status=None)
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status=status,
            trial_end=_days_ago(data.rng.randint(35, 500)),
            converted=False,
            amount_minor=data.monthly_price_minor(),
            currency="usd",
        )
        cohort = "expired_trial" if status == "trialing" else "trial_no_convert"
        data.record(cohort, contact_id)


def build_canceled(data: Dataset) -> None:
    """Cancellations spread over 18 months."""
    for index in range(N_CANCELED):
        key = data.new_key()
        canceled_days = 20 + index * 26 + data.rng.randint(0, 12)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(canceled_days - data.rng.randint(0, 10)),
        )
        customer_id = data.add_customer(key=key, status=None)
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status="canceled",
            canceled_at=_days_ago(canceled_days),
            amount_minor=data.monthly_price_minor(),
            currency="usd",
            created_at=_days_ago(canceled_days + data.rng.randint(180, 900)),
        )
        data.record("canceled_customer", contact_id)


def build_lapsed_high_value(data: Dataset) -> None:
    """Above-median accounts that still look active but went quiet."""
    for _ in range(N_LAPSED_HIGH_VALUE):
        key = data.new_key()
        paid_days = data.rng.randint(125, 300)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(paid_days - data.rng.randint(0, 5)),
        )
        customer_id = data.add_customer(key=key, status="active")
        amount = data.monthly_price_minor(high_value=True)
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status="active",
            renewal_at=_days_ahead(data.rng.randint(40, 200)),
            amount_minor=amount,
            currency="usd",
            created_at=_days_ago(paid_days + data.rng.randint(200, 900)),
        )
        data.add_invoice(
            customer_id=customer_id,
            key=key,
            status="paid",
            collection_outcome="payment_succeeded",
            amount_due_cents=amount,
            currency="usd",
            paid_at=_days_ago(paid_days, hour=9),
        )
        data.record("lapsed_high_value", contact_id)


def build_renewal_upsell(data: Dataset) -> None:
    """Long-tenure accounts renewing inside the 30-day window."""
    for index in range(N_RENEWAL_UPSELL):
        key = data.new_key()
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(data.rng.randint(1, 45)),
        )
        customer_id = data.add_customer(key=key, status="active")
        amount = data.monthly_price_minor()
        data.add_subscription(
            customer_id=customer_id,
            key=key,
            status="active",
            renewal_at=_days_ahead(2 + index * 2 + data.rng.randint(0, 1)),
            amount_minor=amount,
            currency="usd",
            created_at=_days_ago(data.rng.randint(700, 1400)),
        )
        data.add_invoice(
            customer_id=customer_id,
            key=key,
            status="paid",
            collection_outcome="payment_succeeded",
            amount_due_cents=amount,
            currency="usd",
            paid_at=_days_ago(data.rng.randint(5, 40), hour=9),
        )
        data.record("renewal_upsell", contact_id)


def build_closed_lost(data: Dataset) -> tuple[list[str], list[str]]:
    """Closed-lost deals, one per contact, plus the deliberate ambiguity cases.

    Returns the future-dated deal ids and the ambiguous deal ids.
    """
    future_deal_ids: list[str] = []
    ambiguous_deal_ids: list[str] = []
    # Deliberate ambiguity: a deal associated to two contacts whose synthetic
    # keys differ resolves to two customer tokens, which the event library
    # quarantines instead of guessing.
    ambiguous_positions = set(data.rng.sample(range(N_CLOSED_LOST), N_AMBIGUOUS_DEALS))
    # Future close dates land on closed-lost rows so the staleness rule sees them.
    future_positions = set(data.rng.sample(range(N_CLOSED_LOST), N_FUTURE_CLOSE_DATES // 2))
    for index in range(N_CLOSED_LOST):
        key = data.new_key()
        closed_days = 35 + int(index * 5.6) + data.rng.randint(0, 4)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="opportunity",
            last_activity_at=_days_ago(closed_days - data.rng.randint(0, 20)),
        )
        contact_ids = [contact_id]
        if index in ambiguous_positions:
            second_id = data.add_contact(
                key=data.new_key(),
                lifecycle_stage="opportunity",
                last_activity_at=_days_ago(closed_days + data.rng.randint(1, 30)),
            )
            contact_ids.append(second_id)
        closed_at = (
            _days_ahead(data.rng.randint(9, 40))
            if index in future_positions
            else _days_ago(closed_days)
        )
        deal_id = data.add_deal(
            key=key,
            deal_stage="closed_lost",
            contact_ids=contact_ids,
            created_at=_days_ago(closed_days + data.rng.randint(20, 200)),
            closed_at=closed_at,
            amount_minor=data.annual_contract_minor(),
        )
        if index in future_positions:
            future_deal_ids.append(deal_id)
        if index in ambiguous_positions:
            ambiguous_deal_ids.append(deal_id)
        data.record("closed_lost_deal", deal_id)
    return future_deal_ids, ambiguous_deal_ids


def build_silent_proposals(data: Dataset) -> None:
    """Mid-pipeline deals that never closed and are aging."""
    for index in range(N_SILENT_PROPOSAL):
        key = data.new_key()
        age_days = 21 + index * 12 + data.rng.randint(0, 6)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="opportunity",
            last_activity_at=_days_ago(age_days - data.rng.randint(0, 15)),
        )
        deal_id = data.add_deal(
            key=key,
            deal_stage=data.rng.choice(MID_PIPELINE_STAGES),
            contact_ids=[contact_id],
            created_at=_days_ago(age_days),
            amount_minor=data.annual_contract_minor(),
        )
        data.record("silent_proposal", deal_id)


def build_custom_stage_deals(data: Dataset) -> None:
    """Deals parked in an unlabeled numeric pipeline stage."""
    for _ in range(N_CUSTOM_STAGE_DEALS):
        key = data.new_key()
        age_days = data.rng.randint(15, 520)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage=data.rng.choice(LEAD_LIFECYCLE_STAGES),
            last_activity_at=_days_ago(max(1, age_days - data.rng.randint(0, 40))),
        )
        deal_id = data.add_deal(
            key=key,
            deal_stage=data.rng.choice(UNLABELED_STAGES),
            contact_ids=[contact_id],
            created_at=_days_ago(age_days),
            amount_minor=data.annual_contract_minor(),
        )
        data.record("unlabeled_stage_deal", deal_id)
        data.note("unlabeled_custom_stage", deal_id)


def build_closed_won_deals(data: Dataset) -> list[str]:
    """Won deals, including the remaining future-dated close dates."""
    future_deal_ids: list[str] = []
    future_positions = set(
        data.rng.sample(range(N_CLOSED_WON_DEALS), N_FUTURE_CLOSE_DATES - N_FUTURE_CLOSE_DATES // 2)
    )
    for index in range(N_CLOSED_WON_DEALS):
        key = data.new_key()
        closed_days = 12 + index * 17 + data.rng.randint(0, 8)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="customer",
            last_activity_at=_days_ago(max(1, closed_days - data.rng.randint(0, 30))),
        )
        closed_at = (
            _days_ahead(data.rng.randint(5, 45))
            if index in future_positions
            else _days_ago(closed_days)
        )
        deal_id = data.add_deal(
            key=key,
            deal_stage="closed_won",
            contact_ids=[contact_id],
            created_at=_days_ago(closed_days + data.rng.randint(20, 160)),
            closed_at=closed_at,
            amount_minor=data.annual_contract_minor(),
        )
        data.record("closed_won_deal", deal_id)
        if index in future_positions:
            future_deal_ids.append(deal_id)
    return future_deal_ids


def build_open_pipeline_deals(data: Dataset) -> None:
    for _ in range(N_OPEN_PIPELINE_DEALS):
        key = data.new_key()
        age_days = data.rng.randint(3, 60)
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage="opportunity",
            last_activity_at=_days_ago(max(1, age_days - data.rng.randint(0, 3))),
        )
        deal_id = data.add_deal(
            key=key,
            deal_stage=data.rng.choice(OPEN_PIPELINE_STAGES),
            contact_ids=[contact_id],
            created_at=_days_ago(age_days),
            amount_minor=data.annual_contract_minor(),
        )
        data.record("open_pipeline_deal", deal_id)


def build_shared_key_contacts(data: Dataset) -> None:
    """Two contacts, one synthetic_customer_key: the join has to collapse them."""
    for _ in range(N_SHARED_KEY_PAIRS):
        key = data.new_key()
        first = data.add_contact(
            key=key,
            lifecycle_stage="lead",
            last_activity_at=_days_ago(data.rng.randint(10, 400)),
        )
        second = data.add_contact(
            key=key,
            lifecycle_stage="subscriber",
            last_activity_at=_days_ago(data.rng.randint(10, 400)),
        )
        data.note("shared_synthetic_key", f"{first}+{second}")


def build_lead_noise(data: Dataset, remaining: int) -> None:
    """Everything else in the portal: leads, junk lifecycle values, dead ends."""
    for _ in range(remaining):
        key = data.new_key()
        junk = data.rng.random() < JUNK_LIFECYCLE_SHARE
        lifecycle = (
            data.rng.choice(JUNK_LIFECYCLE_STAGES)
            if junk
            else data.rng.choice(LEAD_LIFECYCLE_STAGES)
        )
        contact_id = data.add_contact(
            key=key,
            lifecycle_stage=lifecycle,
            last_activity_at=_days_ago(data.rng.randint(1, 545)),
        )
        data.record("lead_noise", contact_id)
        if junk:
            data.note("free_text_lead_noise", contact_id)


# ---------------------------------------------------------------------------
# Mess passes applied after the cohorts exist
# ---------------------------------------------------------------------------


def apply_suppression_flags(data: Dataset) -> None:
    """Opt-outs and invalid addresses, deliberately overlapping live cohorts.

    Overlapping matters: a suppression flag on a contact inside a recovery
    cohort is what exercises the ``explicit_disqualification`` exclusion path
    rather than merely existing in the file.
    """
    by_id = {contact["id"]: contact for contact in data.contacts}

    opt_out_cohort = (
        data.cohorts["failed_payment"][:2]
        + data.cohorts["canceled_customer"][:1]
        + data.cohorts["renewal_upsell"][:1]
    )
    opt_out_leads = data.rng.sample(
        data.cohorts["lead_noise"], N_EMAIL_OPT_OUT - len(opt_out_cohort)
    )
    for contact_id in opt_out_cohort + opt_out_leads:
        by_id[contact_id]["properties"]["email_opt_out"] = True
        data.note("email_opt_out", contact_id)

    invalid_cohort = data.cohorts["expired_trial"][:1] + data.cohorts["lapsed_high_value"][:1]
    invalid_leads = data.rng.sample(
        [
            item
            for item in data.cohorts["lead_noise"]
            if not by_id[item]["properties"]["email_opt_out"]
        ],
        N_INVALID_ADDRESS - len(invalid_cohort),
    )
    for contact_id in invalid_cohort + invalid_leads:
        by_id[contact_id]["properties"]["invalid_address"] = True
        data.note("invalid_address", contact_id)


def apply_missing_activity(data: Dataset) -> None:
    """Roughly 10% of contacts never got an activity timestamp written."""
    protected = set(data.cohorts["lapsed_high_value"])
    eligible = [contact["id"] for contact in data.contacts if contact["id"] not in protected]
    target = int(round(len(data.contacts) * PCT_CONTACTS_MISSING_ACTIVITY))
    chosen = set(data.rng.sample(eligible, target))
    for contact in data.contacts:
        if contact["id"] in chosen:
            contact["properties"].pop("last_activity_at", None)
            data.note("missing_last_activity_at", contact["id"])


def apply_deal_currency_and_amount_gaps(data: Dataset) -> None:
    """Missing amounts, missing currency, and a handful of non-USD deals."""
    deal_ids = [deal["id"] for deal in data.deals]
    by_id = {deal["id"]: deal for deal in data.deals}

    non_usd = data.rng.sample(deal_ids, N_NON_USD_DEALS)
    for index, deal_id in enumerate(sorted(non_usd)):
        currency = "eur" if index % 2 == 0 else "gbp"
        by_id[deal_id]["properties"]["currency"] = currency
        data.note(f"non_usd_deal_{currency}", deal_id)

    remaining = [deal_id for deal_id in deal_ids if deal_id not in set(non_usd)]
    missing_amount = data.rng.sample(
        remaining, int(round(len(deal_ids) * PCT_DEALS_MISSING_AMOUNT))
    )
    for deal_id in missing_amount:
        by_id[deal_id]["properties"].pop("amount_minor", None)
        data.note("missing_amount", deal_id)

    missing_currency = data.rng.sample(
        remaining, int(round(len(deal_ids) * PCT_DEALS_MISSING_CURRENCY))
    )
    for deal_id in missing_currency:
        by_id[deal_id]["properties"].pop("currency", None)
        data.note("missing_currency", deal_id)


def build_dataset() -> Dataset:
    data = Dataset(rng=random.Random(SEED))
    build_active_paying(data)
    build_failed_payments(data)
    build_trials(data)
    build_canceled(data)
    build_lapsed_high_value(data)
    build_renewal_upsell(data)
    future_lost, ambiguous_deals = build_closed_lost(data)
    build_silent_proposals(data)
    build_custom_stage_deals(data)
    future_won = build_closed_won_deals(data)
    build_open_pipeline_deals(data)
    build_shared_key_contacts(data)

    for deal_id in future_lost + future_won:
        data.note("future_close_date", deal_id)
    for deal_id in ambiguous_deals:
        data.note("deal_with_two_contacts", deal_id)

    remaining = TARGET_CONTACTS - len(data.contacts)
    if remaining < 0:
        raise ValueError("cohorts already exceed the contact target")
    build_lead_noise(data, remaining)

    apply_suppression_flags(data)
    apply_missing_activity(data)
    apply_deal_currency_and_amount_gaps(data)

    data.rng.shuffle(data.contacts)
    data.rng.shuffle(data.deals)
    data.rng.shuffle(data.customers)
    data.rng.shuffle(data.subscriptions)
    data.rng.shuffle(data.invoices)
    return data


# ---------------------------------------------------------------------------
# Snapshots, integrity, reporting
# ---------------------------------------------------------------------------


def hubspot_snapshot(data: Dataset) -> dict[str, Any]:
    return {
        "schema": "hubspot-snapshot.v1",
        "source": "hubspot",
        "contacts": data.contacts,
        "deals": data.deals,
        "associations": {deal["id"]: data.associations[deal["id"]] for deal in data.deals},
    }


def stripe_snapshot(data: Dataset) -> dict[str, Any]:
    return {
        "schema": "stripe-snapshot.v1",
        "source": "stripe",
        "customers": data.customers,
        "subscriptions": data.subscriptions,
        "invoices": data.invoices,
    }


def assert_referential_integrity(hubspot: dict[str, Any], stripe: dict[str, Any]) -> None:
    contact_ids = {contact["id"] for contact in hubspot["contacts"]}
    deal_ids = {deal["id"] for deal in hubspot["deals"]}
    if len(contact_ids) != len(hubspot["contacts"]):
        raise AssertionError("duplicate contact id")
    if len(deal_ids) != len(hubspot["deals"]):
        raise AssertionError("duplicate deal id")

    for deal_id, linked in hubspot["associations"].items():
        if deal_id not in deal_ids:
            raise AssertionError(f"association references unknown deal {deal_id}")
        if not linked:
            raise AssertionError(f"association for {deal_id} is empty")
        for contact_id in linked:
            if contact_id not in contact_ids:
                raise AssertionError(f"association references unknown contact {contact_id}")
    if set(hubspot["associations"]) != deal_ids:
        raise AssertionError("every deal must carry an association")

    customer_ids = {customer["id"] for customer in stripe["customers"]}
    if len(customer_ids) != len(stripe["customers"]):
        raise AssertionError("duplicate stripe customer id")
    customer_key = {
        customer["id"]: customer["synthetic_customer_key"] for customer in stripe["customers"]
    }
    for collection in ("subscriptions", "invoices"):
        seen: set[str] = set()
        for record in stripe[collection]:
            if record["id"] in seen:
                raise AssertionError(f"duplicate stripe {collection} id {record['id']}")
            seen.add(record["id"])
            if record["customer_id"] not in customer_ids:
                raise AssertionError(
                    f"{collection} {record['id']} references unknown customer {record['customer_id']}"
                )
            if record["synthetic_customer_key"] != customer_key[record["customer_id"]]:
                raise AssertionError(
                    f"{collection} {record['id']} disagrees with its customer's synthetic key"
                )

    hubspot_keys = {
        contact["properties"]["synthetic_customer_key"] for contact in hubspot["contacts"]
    }
    hubspot_keys.update(deal["properties"]["synthetic_customer_key"] for deal in hubspot["deals"])
    stripe_keys = {customer["synthetic_customer_key"] for customer in stripe["customers"]}
    orphans = sorted(stripe_keys - hubspot_keys)
    if orphans:
        raise AssertionError(f"stripe keys absent from hubspot: {orphans[:5]}")

    for record in stripe["invoices"]:
        if not isinstance(record.get("amount_due_cents"), int):
            raise AssertionError(f"invoice {record['id']} has a non-integer amount")
    for record in stripe["subscriptions"]:
        if not isinstance(record.get("amount_minor"), int):
            raise AssertionError(f"subscription {record['id']} has a non-integer amount")
    for deal in hubspot["deals"]:
        amount = deal["properties"].get("amount_minor")
        if amount is not None and not isinstance(amount, int):
            raise AssertionError(f"deal {deal['id']} has a non-integer amount")


def money_summary(hubspot: dict[str, Any], stripe: dict[str, Any]) -> dict[str, Any]:
    deal_totals: Counter[str] = Counter()
    unquantified = 0
    missing_currency_only = 0
    for deal in hubspot["deals"]:
        properties = deal["properties"]
        amount = properties.get("amount_minor")
        currency = properties.get("currency")
        if amount is None:
            unquantified += 1
            continue
        if currency is None:
            missing_currency_only += 1
            unquantified += 1
            continue
        deal_totals[currency] += amount

    stripe_totals: Counter[str] = Counter()
    for invoice in stripe["invoices"]:
        if invoice.get("collection_outcome") == "payment_failed":
            stripe_totals[invoice["currency"]] += invoice["amount_due_cents"]
    for subscription in stripe["subscriptions"]:
        if subscription.get("status") in {"trialing", "incomplete_expired", "canceled"}:
            stripe_totals[subscription["currency"]] += subscription["amount_minor"]
        elif subscription.get("status") == "active" and subscription.get("renewal_at"):
            renewal = datetime.strptime(subscription["renewal_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if renewal <= NOW + timedelta(days=30):
                stripe_totals[subscription["currency"]] += subscription["amount_minor"]
    lapsed_total: Counter[str] = Counter()
    for invoice in stripe["invoices"]:
        if invoice.get("collection_outcome") != "payment_succeeded":
            continue
        paid = datetime.strptime(invoice["paid_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        if NOW - paid >= timedelta(days=90):
            lapsed_total[invoice["currency"]] += invoice["amount_due_cents"]
    for currency, amount in lapsed_total.items():
        stripe_totals[currency] += amount

    combined: Counter[str] = Counter()
    combined.update(deal_totals)
    combined.update(stripe_totals)
    return {
        "deal_totals_minor": dict(sorted(deal_totals.items())),
        "stripe_signal_totals_minor": dict(sorted(stripe_totals.items())),
        "combined_totals_minor": dict(sorted(combined.items())),
        "unquantified_deals": unquantified,
        "deals_missing_currency_only": missing_currency_only,
    }


def _money(minor: int, currency: str) -> str:
    """Format minor units without implying a symbol or an exchange rate."""
    return f"{minor / 100:,.2f} {currency.upper()}"


def expected_detection(data: Dataset) -> dict[str, int]:
    """Candidates the event library should emit, after suppression and quarantine.

    Derived from the generator's own bookkeeping, so it stays honest when the
    cohort sizes or mess ratios are tuned. ``--verify-detection`` reproduces
    these numbers by running the real detectors.
    """
    suppressed = set(data.mess.get("email_opt_out", [])) | set(data.mess.get("invalid_address", []))
    families = {
        "failed_payment": "failed_payment",
        "expired_trial": "expired_trial",
        "trial_no_convert": "trial_no_convert",
        "canceled_customer": "canceled_customer",
        "renewal_upsell": "renewal_upsell",
        "disappeared_high_value_customer": "lapsed_high_value",
    }
    expected = {
        family: sum(1 for item in data.cohorts[cohort] if item not in suppressed)
        for family, cohort in families.items()
    }
    quarantined = set(data.mess.get("deal_with_two_contacts", []))
    future = set(data.mess.get("future_close_date", []))
    expected["closed_lost_stale_deal"] = sum(
        1
        for deal_id in data.cohorts["closed_lost_deal"]
        if deal_id not in quarantined
        and deal_id not in future
        and not any(contact_id in suppressed for contact_id in data.associations[deal_id])
    )
    return dict(sorted(expected.items()))


def render_readme(data: Dataset, hubspot: dict[str, Any], stripe: dict[str, Any]) -> str:
    summary = money_summary(hubspot, stripe)
    counts = {name: len(items) for name, items in sorted(data.cohorts.items())}
    mess = {name: len(items) for name, items in sorted(data.mess.items())}
    stage_counts = Counter(deal["properties"]["deal_stage"] for deal in hubspot["deals"])
    lines: list[str] = []
    add = lines.append

    add("# Demo dataset - vertical B2B software (synthetic)")
    add("")
    add(
        "Generated by `scripts/generate_demo_dataset.py`. Illustrative records: no real people, "
        "companies, emails, phone numbers, or identifiers. Regenerate with:"
    )
    add("")
    add("```")
    add("python3 scripts/generate_demo_dataset.py")
    add("```")
    add("")
    add(f"Seed `{SEED}`, clock `{_iso(NOW)}`. Re-running produces byte-identical files.")
    add("All money is in minor units (cents): `14900` is $149.00.")
    add("")
    add("## Scale")
    add("")
    add("| Object | Count |")
    add("|---|---|")
    add(f"| HubSpot contacts | {len(hubspot['contacts'])} |")
    add(f"| HubSpot deals | {len(hubspot['deals'])} |")
    add(f"| HubSpot deal->contact associations | {len(hubspot['associations'])} |")
    add(f"| Stripe customers | {len(stripe['customers'])} |")
    add(f"| Stripe subscriptions | {len(stripe['subscriptions'])} |")
    add(f"| Stripe invoices | {len(stripe['invoices'])} |")
    add("")
    add("## Cohorts produced")
    add("")
    add("| Cohort | Count | How it is expressed |")
    add("|---|---|---|")
    cohort_notes = {
        "active_paying": "active subscription, renewal outside the 30-day window, recent paid invoice",
        "failed_payment": "invoice `open` + `collection_outcome: payment_failed`, attempted in the last 90 days",
        "expired_trial": "subscription `trialing`, `converted: false`, `trial_end` in the past",
        "trial_no_convert": "subscription `incomplete_expired`, `converted: false`, `trial_end` in the past",
        "canceled_customer": "subscription `canceled` with `canceled_at` spread over 18 months",
        "lapsed_high_value": "active subscription, above-median amount, last succeeded payment 125-300 days ago",
        "renewal_upsell": "active subscription, long tenure, `renewal_at` inside 30 days",
        "closed_lost_deal": "deal `closed_lost` with `closed_at` (2 dated in the future)",
        "silent_proposal": "deal in a mid-pipeline stage, no close date, aging",
        "unlabeled_stage_deal": "deal with a numeric-string stage and no human-readable name",
        "closed_won_deal": "deal `closed_won` (2 dated in the future)",
        "open_pipeline_deal": "deal in an early pipeline stage",
        "lead_noise": "contacts with no deal and no Stripe presence",
    }
    for name, count in counts.items():
        add(f"| {name} | {count} | {cohort_notes.get(name, '')} |")
    add("")
    add("### Deal stages present")
    add("")
    add("| `deal_stage` | Count |")
    add("|---|---|")
    for stage, count in sorted(stage_counts.items()):
        add(f"| `{stage}` | {count} |")
    add("")
    add("## Quantified opportunity by currency")
    add("")
    add(
        "Currencies are never converted or blended. Deal amounts are annual "
        "contract value (12 x the monthly price); Stripe amounts are the "
        "monthly subscription or the invoice face value."
    )
    add("")
    add("| Currency | HubSpot deals | Stripe recovery signals | Combined |")
    add("|---|---|---|---|")
    for currency in sorted(summary["combined_totals_minor"]):
        deals_minor = summary["deal_totals_minor"].get(currency, 0)
        stripe_minor = summary["stripe_signal_totals_minor"].get(currency, 0)
        combined_minor = summary["combined_totals_minor"][currency]
        add(
            f"| {currency} | {deals_minor} ({_money(deals_minor, currency)}) "
            f"| {stripe_minor} ({_money(stripe_minor, currency)}) "
            f"| {combined_minor} ({_money(combined_minor, currency)}) |"
        )
    add("")
    add(
        f"**Unquantified deals: {summary['unquantified_deals']}** "
        f"({mess.get('missing_amount', 0)} with no `amount_minor`, "
        f"{summary['deals_missing_currency_only']} with an amount but no `currency`)."
    )
    add("")
    add("## Where each mess case landed")
    add("")
    add("| Spec case | Count | Where |")
    add("|---|---|---|")
    add(
        f"| 1. Missing amounts | {mess.get('missing_amount', 0)} | "
        f"deals with `amount_minor` removed "
        f"({mess.get('missing_amount', 0) / len(hubspot['deals']):.0%} of deals) |"
    )
    add(
        f"| 2. Missing currency | {mess.get('missing_currency', 0)} | "
        "deals with `currency` removed |"
    )
    add(
        f"| 3. Unlabeled custom stage | {mess.get('unlabeled_custom_stage', 0)} | "
        f"deals in `{'`, `'.join(UNLABELED_STAGES)}` "
        f"({mess.get('unlabeled_custom_stage', 0) / len(hubspot['deals']):.0%} of deals) |"
    )
    add(
        f"| 4. Free-text lead noise | {mess.get('free_text_lead_noise', 0)} | "
        "contacts with junk `lifecycle_stage` (bad lead, end user not decision maker, "
        "do not contact, no response, junk, duplicate?) |"
    )
    add(
        f"| 5. Suppression - opt-out | {mess.get('email_opt_out', 0)} | "
        "`email_opt_out: true`; 4 sit inside live cohorts so the exclusion path runs |"
    )
    add(
        f"| 5. Suppression - invalid address | {mess.get('invalid_address', 0)} | "
        "`invalid_address: true`; 2 sit inside live cohorts |"
    )
    add(
        f"| 6. Identity ambiguity - shared key | {mess.get('shared_synthetic_key', 0)} | "
        "contact pairs sharing one `synthetic_customer_key` (the join collapses them) |"
    )
    add(
        f"| 6. Identity ambiguity - split deal | {mess.get('deal_with_two_contacts', 0)} | "
        "closed-lost deals associated to two contacts in different clusters "
        "(quarantined as `ambiguous_identity` rather than guessed) |"
    )
    add(
        f"| 7. Missing activity dates | {mess.get('missing_last_activity_at', 0)} | "
        f"contacts with no `last_activity_at` "
        f"({mess.get('missing_last_activity_at', 0) / len(hubspot['contacts']):.0%} of contacts) |"
    )
    add(
        f"| 8. Future close dates | {mess.get('future_close_date', 0)} | "
        "deals whose `closed_at` is after the clock |"
    )
    add(
        f"| 9. Non-USD deals | "
        f"{mess.get('non_usd_deal_eur', 0) + mess.get('non_usd_deal_gbp', 0)} | "
        f"{mess.get('non_usd_deal_eur', 0)} `eur`, {mess.get('non_usd_deal_gbp', 0)} `gbp`; "
        "they form their own currency groups |"
    )
    add("")
    add("## Detector reachability")
    add("")
    add(
        "Cohort field values were written against the detector conditions in "
        "`found_money/events/library.py`, not against the prose in the spec."
    )
    add("")
    add(
        "Reachable through the full event library (`detect_event_families`). "
        "The candidate column is the cohort minus the rows the kit deliberately "
        "drops: opt-out and invalid-address suppressions, deals whose "
        "`closed_at` is in the future, and quarantined ambiguous joins. "
        "`python3 scripts/generate_demo_dataset.py --verify-detection` "
        "reproduces these counts by running the detectors."
    )
    add("")
    add("| Event family | Cohort | Expected candidates | Dropped, and why |")
    add("|---|---|---|---|")
    detection = expected_detection(data)
    cohort_for_family = {
        "canceled_customer": "canceled_customer",
        "closed_lost_stale_deal": "closed_lost_deal",
        "disappeared_high_value_customer": "lapsed_high_value",
        "expired_trial": "expired_trial",
        "failed_payment": "failed_payment",
        "renewal_upsell": "renewal_upsell",
        "trial_no_convert": "trial_no_convert",
    }
    drop_notes = {
        "canceled_customer": "1 `email_opt_out`",
        "closed_lost_stale_deal": (
            f"{mess.get('deal_with_two_contacts', 0)} quarantined as ambiguous, "
            "2 closed in the future"
        ),
        "disappeared_high_value_customer": "1 `invalid_address`",
        "expired_trial": "1 `invalid_address`",
        "failed_payment": "2 `email_opt_out`",
        "renewal_upsell": "1 `email_opt_out`",
        "trial_no_convert": "none",
    }
    for family, expected_count in detection.items():
        cohort_size = counts[cohort_for_family[family]]
        note = drop_notes[family] if cohort_size != expected_count else "none"
        add(f"| `{family}` | {cohort_size} | {expected_count} | {note} |")
    add("")
    add("Present as data but not detectable from these two sources:")
    add("")
    add(
        "- `silent_proposal` - the detector reads a `proposals` source. These "
        "deals sit in HubSpot mid-pipeline stages, so they are visible in the "
        "snapshot and inert to the detector."
    )
    add(
        "- `lapsed_repeat_buyer` / `overdue_reorder` - both require an `orders` "
        "source, which a SaaS subscription business does not have."
    )
    add("- `no_show_rebook` - requires an `appointments` source.")
    add(
        "- `engaged_unbooked` - requires an engagement score or flag, which is "
        "not part of `hubspot-snapshot.v1`."
    )
    add(
        "- `payment_rescue` - a Money Map pile, not a separate detector. The "
        "failed-payment family feeds it, so it needs no cohort of its own."
    )
    add("")
    suppressed_ids = set(data.mess.get("email_opt_out", [])) | set(
        data.mess.get("invalid_address", [])
    )
    suppressed_in_cohort = sum(
        1
        for cohort in (
            "failed_payment",
            "expired_trial",
            "trial_no_convert",
            "canceled_customer",
            "lapsed_high_value",
            "renewal_upsell",
        )
        for contact_id in data.cohorts[cohort]
        if contact_id in suppressed_ids
    )
    add(
        f"The detectors also emit {mess.get('deal_with_two_contacts', 0)} "
        "`ambiguous_identity` data gaps (the split-association deals) and "
        f"{suppressed_in_cohort} `explicit_disqualification` exclusions (the "
        "opt-out and invalid-address contacts that sit inside live cohorts). "
        "Both paths are exercised on purpose."
    )
    add("")
    add(
        "The Recovery Room shows only opportunities supported by the supplied "
        "records and business profile. Missing evidence stays visible as a gap; "
        "generated opportunities are not recovered revenue."
    )
    add("")
    add("## Deliberate constraints")
    add("")
    add(
        "- Customers in the failed-payment, expired-trial and canceled cohorts "
        "carry no succeeded invoice, because a succeeded payment anywhere on "
        "the customer triggers the `later_payment` suppression and would erase "
        "the cohort."
    )
    add(
        "- Stripe customer rows for trial and canceled accounts carry no "
        "`status`, because an `active` status triggers the `reactivation` "
        "suppression for those families."
    )
    add(
        "- Closed-lost contacts hold exactly one deal, because a second open "
        "deal on the same contact triggers the `active_negotiation` suppression."
    )
    add(
        "- The HubSpot portal's single product object has no representation in "
        "`hubspot-snapshot.v1` or `stripe-snapshot.v1`, so it is not emitted."
    )
    add("")
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def verify_detection(hubspot: dict[str, Any], stripe: dict[str, Any]) -> dict[str, int]:
    """Run the real detector library over the generated snapshots."""
    from found_money.events.library import detect_event_families
    from found_money.identity import build_identity_graph, normalize_source_records

    run_id = "run_demo_verticalsaas"
    nodes = normalize_source_records(
        {"hubspot": hubspot, "stripe": stripe}, default_observed_at=NOW
    )
    graph = build_identity_graph(nodes, run_id=run_id, built_at=NOW)
    result = detect_event_families(
        {"hubspot": hubspot, "stripe": stripe},
        graph,
        run_id=run_id,
        built_at=NOW,
    )
    families = Counter(item.event_family for item in result.candidates.candidates)
    reasons = Counter(item.reason_code for item in result.exclusions.exclusions)
    gaps = Counter(item.reason_code for item in result.data_gaps.gaps)
    print("\nDetector verification (detect_event_families):")
    print(f"  identity clusters: {len(graph.customers)}")
    print(f"  candidates: {len(result.candidates.candidates)}")
    for family, count in sorted(families.items()):
        print(f"    {family}: {count}")
    print(f"  exclusions: {len(result.exclusions.exclusions)}")
    for reason, count in sorted(reasons.items()):
        print(f"    {reason}: {count}")
    print(f"  data gaps: {len(result.data_gaps.gaps)}")
    for gap_reason, count in sorted(gaps.items()):
        print(f"    {gap_reason}: {count}")
    return {str(family): count for family, count in families.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="directory that receives hubspot/, stripe/ and README.md",
    )
    parser.add_argument(
        "--verify-detection",
        action="store_true",
        help="run found_money's event library over the generated snapshots",
    )
    args = parser.parse_args()

    data = build_dataset()
    hubspot = hubspot_snapshot(data)
    stripe = stripe_snapshot(data)
    assert_referential_integrity(hubspot, stripe)

    root = args.output_root
    write_json(root / "hubspot" / "snapshot.json", hubspot)
    write_json(root / "stripe" / "snapshot.json", stripe)
    readme = render_readme(data, hubspot, stripe)
    (root / "README.md").write_text(readme, encoding="utf-8")

    summary = money_summary(hubspot, stripe)
    print(f"wrote {root / 'hubspot' / 'snapshot.json'}")
    print(f"wrote {root / 'stripe' / 'snapshot.json'}")
    print(f"wrote {root / 'README.md'}")
    print(
        f"contacts={len(hubspot['contacts'])} deals={len(hubspot['deals'])} "
        f"customers={len(stripe['customers'])} subscriptions={len(stripe['subscriptions'])} "
        f"invoices={len(stripe['invoices'])}"
    )
    for name, items in sorted(data.cohorts.items()):
        print(f"  cohort {name}: {len(items)}")
    for name, items in sorted(data.mess.items()):
        print(f"  mess {name}: {len(items)}")
    print(f"  combined quantified minor units: {summary['combined_totals_minor']}")
    print(f"  unquantified deals: {summary['unquantified_deals']}")

    if args.verify_detection:
        verify_detection(hubspot, stripe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
