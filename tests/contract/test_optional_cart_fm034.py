"""Fail-closed optional high-value cart contract for FM-034."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from found_money.contracts.cart import (
    CART_SCHEMA_VERSION,
    EcommerceOptionalCartV1,
    cart_qualification_reason,
    omitted_cart_binding,
    parse_optional_cart,
)

CLOCK = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)

SUPPLIED = (
    '{"carts":[{"cart_id":"cart_ecom_hv_001","currency":"usd",'
    '"customer_external_id":"ord_cust_reorder_001",'
    '"observed_at":"2026-07-20T12:00:00.000Z","state":"abandoned",'
    '"total_minor":"22000"}],"schema_version":"ecommerce-optional-cart.v1",'
    '"state":"supplied"}\n'
).encode()


def test_parser_accepts_canonical_supplied_and_omitted_bindings():
    supplied = parse_optional_cart(SUPPLIED)
    assert supplied.state == "supplied"
    assert supplied.carts[0].total_minor == Decimal("22000")
    assert supplied.carts[0].currency == "usd"
    assert supplied.to_canonical_json() == SUPPLIED
    omitted = omitted_cart_binding()
    assert omitted.state == "omitted"
    assert omitted.carts == []
    assert parse_optional_cart(omitted.to_canonical_json()).sha256() == omitted.sha256()


def test_parser_rejects_wrong_schema_empty_object_and_truthy_junk():
    with pytest.raises(ValueError):
        parse_optional_cart(
            b'{"carts":[{}],"schema_version":"ecommerce-optional-cart.v1","state":"supplied"}\n'
        )
    with pytest.raises((ValueError, ValidationError)):
        EcommerceOptionalCartV1.model_validate(
            {"carts": [{}], "schema_version": CART_SCHEMA_VERSION, "state": "supplied"}
        )
    with pytest.raises(ValueError):
        parse_optional_cart(b"[]")
    with pytest.raises(ValueError):
        parse_optional_cart(b"true")
    extra = json.loads(SUPPLIED.decode())
    extra["unexpected"] = True
    with pytest.raises(ValidationError):
        EcommerceOptionalCartV1.model_validate(extra)


def test_parser_rejects_float_money_missing_join_and_noncanonical_bytes():
    payload = json.loads(SUPPLIED.decode())
    payload["carts"][0]["total_minor"] = 22000.5
    with pytest.raises((ValueError, ValidationError)):
        EcommerceOptionalCartV1.model_validate(payload)
    payload = json.loads(SUPPLIED.decode())
    del payload["carts"][0]["customer_external_id"]
    with pytest.raises(ValidationError):
        EcommerceOptionalCartV1.model_validate(payload)
    pretty = json.dumps(json.loads(SUPPLIED.decode()), indent=2).encode()
    with pytest.raises(ValueError, match="canonical"):
        parse_optional_cart(pretty)


def test_qualification_withholds_below_threshold_completed_recent_wrong_customer():
    supplied = parse_optional_cart(SUPPLIED)
    cart = supplied.carts[0]
    assert cart_qualification_reason(cart, clock=CLOCK, joined=True) is None
    assert (
        cart_qualification_reason(cart, clock=CLOCK, joined=False)
        == "wrong_customer_high_value_cart"
    )
    completed = cart.model_copy(update={"state": "completed"})
    assert (
        cart_qualification_reason(completed, clock=CLOCK, joined=True)
        == "completed_high_value_cart"
    )
    recent = cart.model_copy(update={"observed_at": CLOCK})
    assert cart_qualification_reason(recent, clock=CLOCK, joined=True) == "recent_high_value_cart"
    cheap = cart.model_copy(update={"total_minor": Decimal("9999")})
    assert (
        cart_qualification_reason(cheap, clock=CLOCK, joined=True)
        == "below_threshold_high_value_cart"
    )
