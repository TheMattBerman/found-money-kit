"""Private, GET-only HubSpot ingestion and canonical transformation."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from found_money.safety.allowlist import HUBSPOT_HOST, assert_network_allowed

BASE = "https://api.hubapi.com"
CONTACT_PROPERTIES = [
    "email",
    "phone",
    "lifecyclestage",
    "trial_end_date",
    "notes_last_updated",
    "notes_last_contacted",
    "lastmodifieddate",
    "hs_email_optout",
    "hs_email_bad_address",
]
DEAL_PROPERTIES = [
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "createdate",
    "hs_v2_date_entered_current_stage",
    "notes_last_activity_date",
]


class HubSpotError(RuntimeError):
    pass


def _get(url, token, transport=None):
    if not token:
        raise HubSpotError("HUBSPOT_PRIVATE_APP_TOKEN is required in the environment")
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != HUBSPOT_HOST:
            raise ValueError("host")
        assert_network_allowed("GET", parsed.hostname, parsed.path)
    except Exception as exc:
        raise HubSpotError("HubSpot request is outside the central read allowlist") from exc
    request = Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    if transport:
        return transport(request)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def paginate(path, token, transport=None, limit=100, properties=None):
    after = None
    result = []
    while True:
        query = {"limit": limit}
        if properties:
            query["properties"] = ",".join(properties)
        if after:
            query["after"] = after
        page = _get(BASE + path + "?" + urlencode(query), token, transport)
        result.extend(page.get("results", []))
        after = page.get("paging", {}).get("next", {}).get("after")
        if not after:
            return result


def associations(object_type, object_id, to_type, token=None, transport=None):
    """Read relationship pages only; there is deliberately no association write function."""
    token = token or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    return paginate(
        f"/crm/v4/objects/{object_type}/{object_id}/associations/{to_type}", token, transport
    )


def _age(value, today):
    if not value:
        return None
    try:
        return max(
            0, (today - datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()).days
        )
    except ValueError:
        return None


def _yes(value):
    return str(value).lower() == "true"


def _props(item):
    return item.get("properties", {}) or {}


def canonical_records(raw, today=None):
    """Convert HubSpot timestamps/stages/opt-outs into the kit's canonical mapping rows."""
    today = today or datetime.now(timezone.utc).date()
    contacts = {str(item.get("id")): item for item in raw["contacts"]}
    records = []
    consumed = set()

    def contact_row(source_id):
        item = contacts.get(source_id, {})
        p = _props(item)
        activity = (
            p.get("notes_last_updated")
            or p.get("notes_last_contacted")
            or p.get("lastmodifieddate")
        )
        return {
            "id": source_id,
            "email": p.get("email"),
            "phone": p.get("phone"),
            "lifecycle": p.get("lifecyclestage", ""),
            "last_activity_days": _age(activity, today),
            "trial_end_days": _age(p.get("trial_end_date"), today),
            "subscribed": False if _yes(p.get("hs_email_optout")) else None,
            "invalid_contact": _yes(p.get("hs_email_bad_address")),
            "consent_email": None,
            "consent_sms": None,
        }

    # A contact that is associated with several candidates remains one review record, not several opportunities.
    for item in sorted(raw["candidate_deals"], key=lambda d: str(d.get("id"))):
        deal_id = str(item.get("id"))
        associated = raw["associations"].get(deal_id, [])
        targets = [
            source_id
            for source_id in associated
            if source_id in contacts and source_id not in consumed
        ]
        if not targets:
            targets = ["deal-" + deal_id] if not associated else []
        for source_id in targets:
            row = (
                contact_row(source_id)
                if source_id in contacts
                else {"id": source_id, "consent_email": None, "consent_sms": None}
            )
            p = _props(item)
            stage = str(p.get("dealstage", "")).lower()
            row["value"] = p.get("amount")
            if stage == "qualifiedtobuy":
                # Stage age is not a trial end. Only an explicit trial end date can classify it as expired.
                row["open_deal"] = True
            else:
                row["deal_stage"] = "closed_lost"
                row["closed_lost_days"] = _age(
                    p.get("closedate") or p.get("hs_v2_date_entered_current_stage"), today
                )
            records.append(row)
            consumed.add(source_id)
    return records


# Deal stages treated as recovery candidates. Only the standard HubSpot stage ids
# are built in. Custom pipeline stage ids are portal-specific, so a deployment adds
# its own through FOUND_MONEY_EXTRA_DEAL_STAGES (comma-separated) rather than having
# one portal's ids hardcoded here.
DEFAULT_CANDIDATE_DEAL_STAGES = ("qualifiedtobuy", "closedlost")


def candidate_deal_stages():
    extra = os.environ.get("FOUND_MONEY_EXTRA_DEAL_STAGES", "")
    stages = {part.strip().lower() for part in extra.split(",") if part.strip()}
    stages.update(DEFAULT_CANDIDATE_DEAL_STAGES)
    return stages


def fetch_private_run(token=None, transport=None):
    token = token or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    contacts = paginate("/crm/v3/objects/contacts", token, transport, properties=CONTACT_PROPERTIES)
    deals = paginate("/crm/v3/objects/deals", token, transport, properties=DEAL_PROPERTIES)
    stages = candidate_deal_stages()
    candidates = [
        item for item in deals if str(_props(item).get("dealstage", "")).lower() in stages
    ]
    contact_ids = set()
    association_map = {}
    for deal in candidates:
        ids = [
            str(row.get("toObjectId"))
            for row in associations("deals", str(deal.get("id")), "contacts", token, transport)
            if row.get("toObjectId") is not None
        ]
        association_map[str(deal.get("id"))] = ids
        contact_ids.update(ids)
    return {
        "contract": "GET-only",
        "contacts": contacts,
        "deals": deals,
        "candidate_deals": candidates,
        "candidate_contact_ids": sorted(contact_ids),
        "associations": association_map,
    }


def write_private_run(raw, output_dir, private_root=None):
    """Raw HubSpot data stays under the caller's trusted private-runs root, never site-packages."""
    output = Path(output_dir).resolve()
    private = Path(private_root or (Path.cwd() / "private-runs")).resolve()
    if private not in output.parents and output != private:
        raise HubSpotError("HubSpot raw data may only be written under the caller's private-runs/")
    from found_money.receipts import _atomic_write_bytes, _validate_relative_under_root

    path = _validate_relative_under_root(output, "raw-hubspot.json")
    _atomic_write_bytes(path, json.dumps(raw, indent=2).encode("utf-8"))
    return path
