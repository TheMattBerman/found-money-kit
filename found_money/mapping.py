from .models import Record

ALIASES = {
    "source_id": ("id", "record_id", "contact_id", "hs_object_id"),
    "email": ("email",),
    "phone": ("phone", "mobilephone"),
    "name": ("name", "firstname"),
    "company": ("company", "company_name"),
    "lifecycle": ("lifecycle", "lifecyclestage"),
    "deal_stage": ("deal_stage", "dealstage"),
    "last_activity_days": ("last_activity_days",),
    "trial_end_days": ("trial_end_days",),
    "closed_lost_days": ("closed_lost_days",),
    "last_purchase_days": ("last_purchase_days", "reorder_gap_days"),
    "value": ("value", "amount", "deal_amount"),
    "subscribed": ("subscribed",),
    "dnc": ("dnc", "do_not_contact"),
    "invalid_contact": ("invalid_contact",),
    "consent_email": ("consent_email", "email_consent"),
    "consent_sms": ("consent_sms", "sms_consent"),
    "open_deal": ("open_deal",),
    "active_subscription": ("active_subscription",),
}


def _get(row, names):
    return next((row[n] for n in names if row.get(n) not in (None, "")), None)


def _bool(v):
    if v in (None, ""):
        return None
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "y")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError("numeric lifecycle/value field is invalid")


def map_row(row):
    v = {k: _get(row, a) for k, a in ALIASES.items()}
    source = str(v["source_id"] or "")
    if not source:
        raise ValueError("record is missing a stable id")
    record = Record(
        source_id=source,
        email=v["email"],
        phone=v["phone"],
        name=v["name"],
        company=v["company"],
        lifecycle=str(v["lifecycle"] or ""),
        deal_stage=str(v["deal_stage"] or ""),
        last_activity_days=_num(v["last_activity_days"]),
        trial_end_days=_num(v["trial_end_days"]),
        closed_lost_days=_num(v["closed_lost_days"]),
        last_purchase_days=_num(v["last_purchase_days"]),
        value=_num(v["value"]),
        subscribed=_bool(v["subscribed"]),
        dnc=bool(_bool(v["dnc"])),
        invalid_contact=bool(_bool(v["invalid_contact"])),
        consent_email=_bool(v["consent_email"]),
        consent_sms=_bool(v["consent_sms"]),
        open_deal=bool(_bool(v["open_deal"])),
        active_subscription=bool(_bool(v["active_subscription"])),
        raw=dict(row),
    )
    if record.value is not None and record.value < 0:
        raise ValueError("value cannot be negative")
    return record
