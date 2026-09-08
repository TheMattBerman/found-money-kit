from .models import Decision
from .redaction import token


def classify(r):
    if r.trial_end_days is not None and r.trial_end_days >= 1:
        return "expired_trial"
    if r.deal_stage.lower() in ("closed lost", "closed_lost") or (
        r.closed_lost_days is not None and r.closed_lost_days >= 30
    ):
        return "closed_lost"
    if r.last_purchase_days is not None and r.last_purchase_days >= 90:
        return "lapsed_customer_reorder_gap"
    if r.last_activity_days is not None and r.last_activity_days >= 45:
        return "stale_lead"
    return "not_eligible"


def suppression(r):
    if r.subscribed is False:
        return "unsubscribed"
    if r.dnc:
        return "do_not_contact"
    if r.invalid_contact:
        return "invalid_contact"
    if r.consent_email is False or r.consent_sms is False:
        return "contactability_conflict"
    if r.consent_email is None and r.consent_sms is None:
        return "contactability_unknown"
    if r.open_deal:
        return "open_deal"
    if r.active_subscription:
        return "active_subscription"
    if r.last_activity_days is not None and r.last_activity_days < 14:
        return "recent_activity"
    return None


def _channel(r):
    if r.consent_email is True and r.email:
        return "email"
    if r.consent_sms is True and r.phone:
        return "sms"
    return None


def decide(r):
    kind, blocked = classify(r), suppression(r)
    if kind == "not_eligible":
        return Decision(
            token(r.source_id), kind, "excluded", ["no_recovery_lifecycle"], 0, 0, 0, None, None
        )
    if blocked:
        return Decision(token(r.source_id), kind, "suppressed", [blocked], 0, 0, 0, None, None)
    channel = _channel(r)
    if not channel:
        return Decision(
            token(r.source_id), kind, "review", ["no_usable_consented_channel"], 0, 0, 0, None, None
        )
    base = r.value
    low, high = (round(base * 0.25, 2), round(base * 0.75, 2)) if base is not None else (0, 0)
    score = int(
        high
        + {
            "expired_trial": 40,
            "closed_lost": 30,
            "lapsed_customer_reorder_gap": 25,
            "stale_lead": 15,
        }[kind]
    )
    text = {
        "expired_trial": "Your trial has ended. Reply if a quick restart review would help.",
        "closed_lost": "Checking whether priorities changed. Reply if a short fit review is useful.",
        "lapsed_customer_reorder_gap": "It may be time to revisit your prior plan. Reply for a concise options review.",
        "stale_lead": "Checking whether this is still relevant. Reply if a short update is useful.",
    }[kind]
    return Decision(
        token(r.source_id),
        kind,
        "review",
        ["human_approval_required"],
        low,
        high,
        score,
        channel,
        text[:160] if channel == "sms" else text,
    )


def dedupe(records):
    seen, result = set(), []
    for r in sorted(records, key=lambda x: x.source_id):
        # Stable contact channels win; source ID is only the fallback when both are absent.
        key = (
            ("email", r.email.lower())
            if r.email
            else (("phone", r.phone) if r.phone else ("source", r.source_id))
        )
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


def evaluate(records):
    return sorted((decide(r) for r in dedupe(records)), key=lambda d: (-d.score, d.token))


def segment_cards(ds):
    out: dict[str, dict[str, int]] = {}
    for d in ds:
        x = out.setdefault(
            d.classification,
            {"count": 0, "review": 0, "suppressed": 0, "value_low": 0, "value_high": 0},
        )
        x["count"] += 1
        x[d.status] = x.get(d.status, 0) + 1
        x["value_low"] += d.value_low
        x["value_high"] += d.value_high
    return out
