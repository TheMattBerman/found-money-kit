from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Record:
    source_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    company: Optional[str] = None
    lifecycle: str = ""
    deal_stage: str = ""
    last_activity_days: Optional[float] = None
    trial_end_days: Optional[float] = None
    closed_lost_days: Optional[float] = None
    last_purchase_days: Optional[float] = None
    value: Optional[float] = None
    subscribed: Optional[bool] = None
    dnc: bool = False
    invalid_contact: bool = False
    consent_email: Optional[bool] = None
    consent_sms: Optional[bool] = None
    open_deal: bool = False
    active_subscription: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Decision:
    token: str
    classification: str
    status: str
    reasons: List[str]
    value_low: float
    value_high: float
    score: int
    channel: Optional[str]
    draft: Optional[str]
