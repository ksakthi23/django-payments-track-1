from __future__ import annotations

import hashlib
from unittest.mock import MagicMock
from unittest.mock import Mock

import pytest
from django.http import HttpResponse
from django.http import HttpResponseForbidden

from payments import PaymentStatus

from . import DotpayProvider
from .forms import COMPLETED
from .forms import REJECTED

VARIANT = "dotpay"
PIN = "123"
PROCESS_POST = {
    "id": "123",
    "operation_number": "MI-5",
    "operation_type": "payment",
    "operation_status": COMPLETED,
    "operation_amount": "212.35",
    "operation_currency": "PLN",
    "operation_original_amount": "58.07",
    "operation_original_currency": "USD",
    "operation_datetime": "2018-02-28 12:00:00",
    "control": "1",
    "description": "Order #1",
    "email": "user@example.org",
    "p_info": "John Doe (seller@example.com)",
    "p_email": "seller@example.com",
    "channel": "1",
}


def get_post_with_sha256(post: dict[str, str]) -> dict[str, str]:
    post = post.copy()
    post["pin"] = PIN
    keys = [
        "pin",
        "id",
        "operation_number",
        "operation_type",
        "operation_status",
        "operation_amount",
        "operation_currency",
        "operation_original_amount",
        "operation_original_currency",
        "operation_datetime",
        "control",
        "description",
        "email",
        "p_info",
        "p_email",
        "channel",
    ]
    key = "".join([post[key] for key in keys])
    sha256 = hashlib.sha256()
    sha256.update(key.encode("utf-8"))
    post["signature"] = sha256.hexdigest()
    return post


class Payment(Mock):
    id = 1
    variant = VARIANT
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING

    def get_process_url(self) -> str:
        return "http://example.com"

    def get_failure_url(self) -> str:
        return "http://cancel.com"

    def get_success_url(self) -> str:
        return "http://success.com"

    def change_status(self, status: str) -> None:
        self.status = status


@pytest.fixture
def payment() -> Payment:
    return Payment()


def test_get_hidden_fields(payment: Payment) -> None:
    provider = DotpayProvider(seller_id="123", pin=PIN)
    assert isinstance(provider.get_hidden_fields(payment), dict)


def test_process_data_payment_accepted(payment: Payment) -> None:
    request = MagicMock()
    request.POST = get_post_with_sha256(PROCESS_POST)
    provider = DotpayProvider(
        seller_id=123,
        pin=PIN,
        endpoint="test.endpoint.com",
        channel=1,
        lang="en",
        lock=True,
    )
    response = provider.process_data(payment, request)
    assert isinstance(response, HttpResponse)
    assert payment.status == PaymentStatus.CONFIRMED


def test_process_data_payment_rejected(payment: Payment) -> None:
    data = dict(PROCESS_POST)
    data.update({"operation_status": REJECTED})
    request = MagicMock()
    request.POST = get_post_with_sha256(data)
    provider = DotpayProvider(
        seller_id=123,
        pin=PIN,
        endpoint="test.endpoint.com",
        channel=1,
        lang="en",
        lock=True,
    )
    response = provider.process_data(payment, request)
    assert isinstance(response, HttpResponse)
    assert payment.status == PaymentStatus.REJECTED


def test_incorrect_process_data(payment: Payment) -> None:
    request = MagicMock()
    request.POST = PROCESS_POST  # no signature
    provider = DotpayProvider(seller_id="123", pin=PIN)
    response = provider.process_data(payment, request)
    assert isinstance(response, HttpResponseForbidden)


def test_uses_channel_groups_when_set(payment: Payment) -> None:
    channel_groups = "K,T"
    provider = DotpayProvider(
        seller_id=123,
        pin=PIN,
        endpoint="test.endpoint.com",
        channel=1,
        channel_groups=channel_groups,
        lang="en",
        lock=True,
    )
    hidden_fields = provider.get_hidden_fields(payment)
    assert hidden_fields["channel_groups"] == channel_groups
    assert hidden_fields.get("channel") is None


def test_provider_rejects_pre_authorization() -> None:
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(
        ImproperlyConfigured,
        match="Dotpay does not support pre-authorization",
    ):
        DotpayProvider(seller_id="123", pin=PIN, capture=False)


def test_get_action_returns_configured_endpoint(payment: Payment) -> None:
    endpoint = "https://example.com/payment/"
    provider = DotpayProvider(
        seller_id="123",
        pin=PIN,
        endpoint=endpoint,
    )

    assert provider.get_action(payment) == endpoint


def test_get_hidden_fields_requires_description(payment: Payment) -> None:
    payment.description = ""
    provider = DotpayProvider(seller_id="123", pin=PIN)

    with pytest.raises(ValueError, match="Payment description is required"):
        provider.get_hidden_fields(payment)


def test_get_hidden_fields_enables_channel_options(payment: Payment) -> None:
    provider = DotpayProvider(
        seller_id="123",
        pin=PIN,
        ignore_last_payment_channel=True,
        lock=True,
    )

    fields = provider.get_hidden_fields(payment)

    assert fields["ignore_last_payment_channel"] == "1"
    assert fields["ch_lock"] == "1"


def test_process_data_with_empty_post_returns_forbidden(
    payment: Payment,
) -> None:
    request = MagicMock()
    request.POST = {}

    provider = DotpayProvider(seller_id="123", pin=PIN)

    response = provider.process_data(payment, request)

    assert isinstance(response, HttpResponseForbidden)
    assert response.content == b"FAILED"


def test_process_payment_form_rejects_bad_signature(payment: Payment) -> None:
    from .forms import ProcessPaymentForm

    data = get_post_with_sha256(PROCESS_POST)
    data["signature"] = "invalid-signature"

    form = ProcessPaymentForm(payment=payment, pin=PIN, data=data)

    assert not form.is_valid()
    assert "Bad hash" in form.errors["signature"][0]


def test_process_payment_form_rejects_wrong_payment_id(
    payment: Payment,
) -> None:
    from .forms import ProcessPaymentForm

    data = dict(PROCESS_POST)
    data["control"] = "999"
    data = get_post_with_sha256(data)

    form = ProcessPaymentForm(payment=payment, pin=PIN, data=data)

    assert not form.is_valid()
    assert "Bad payment id" in form.errors["control"][0]


def test_process_payment_form_save_with_other_status(
    payment: Payment,
) -> None:
    from .forms import ProcessPaymentForm

    data = dict(PROCESS_POST)
    data["operation_status"] = "processing"
    data = get_post_with_sha256(data)

    form = ProcessPaymentForm(payment=payment, pin=PIN, data=data)

    assert form.is_valid()
    form.save()

    assert payment.transaction_id == "MI-5"
    assert payment.status == PaymentStatus.WAITING
    payment.save.assert_called_once()
