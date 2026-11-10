from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from payments import PaymentStatus
from payments import RedirectNeeded

from . import SofortProvider

SECRET = "abcd1234"
CLIENT_ID = "1234"
PROJECT_ID = "abcd"


class Payment(Mock):
    id = 1
    variant = "sagepay"
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING
    transaction_id = None
    captured_amount = 0
    billing_first_name = "John"
    description = "foo bar"

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


@pytest.fixture
def provider() -> SofortProvider:
    return SofortProvider(id=CLIENT_ID, project_id=PROJECT_ID, key=SECRET)


@patch("xmltodict.parse")
@patch("requests.post")
def test_provider_raises_redirect_needed_on_success(
    mocked_post: MagicMock,
    mocked_parser: MagicMock,
    payment: Payment,
    provider: SofortProvider,
) -> None:
    response = MagicMock()
    response.status_code = 200
    mocked_post.return_value = response
    mocked_parser.return_value = {
        "new_transaction": {"payment_url": "http://payment.com"}
    }
    with pytest.raises(RedirectNeeded):
        provider.get_form(payment)


@patch("xmltodict.parse")
@patch("requests.post")
@patch("payments.sofort.redirect")
def test_provider_redirects_on_success(
    mocked_redirect: MagicMock,
    mocked_post: MagicMock,
    mocked_parser: MagicMock,
    payment: Payment,
    provider: SofortProvider,
) -> None:
    transaction_id = "1234"
    request = MagicMock()
    request.GET = {"trans": transaction_id}
    mocked_parser.return_value = {
        "transactions": {
            "transaction_details": {
                "status": "ok",
                "sender": {"holder": "John Doe", "country_code": "EN"},
            }
        }
    }
    provider.process_data(payment, request)
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total
    assert payment.transaction_id == transaction_id


@patch("xmltodict.parse")
@patch("requests.post")
@patch("payments.sofort.redirect")
def test_provider_redirects_on_failure(
    mocked_redirect: MagicMock,
    mocked_post: MagicMock,
    mocked_parser: MagicMock,
    payment: Payment,
    provider: SofortProvider,
) -> None:
    transaction_id = "1234"
    request = MagicMock()
    request.GET = {"trans": transaction_id}
    mocked_parser.return_value = {}
    provider.process_data(payment, request)
    assert payment.status == PaymentStatus.REJECTED
    assert payment.captured_amount == 0
    assert payment.transaction_id == transaction_id


@patch("xmltodict.parse")
@patch("requests.post")
def test_provider_refunds_payment(
    mocked_post: MagicMock,
    mocked_parser: MagicMock,
    payment: Payment,
    provider: SofortProvider,
) -> None:
    payment.extra_data = json.dumps(
        {
            "transactions": {
                "transaction_details": {
                    "status": "ok",
                    "sender": {
                        "holder": "John Doe",
                        "country_code": "EN",
                        "bic": "1234",
                        "iban": "abcd",
                    },
                }
            }
        }
    )
    mocked_parser.return_value = {}
    provider.refund(payment)
    assert payment.status == PaymentStatus.REFUNDED


def test_get_form_saves_payment_without_id(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    payment.id = None
    response = MagicMock(status_code=200)

    with (
        patch(
            "payments.sofort.render_to_string",
            return_value="<xml />",
        ),
        patch.object(
            provider,
            "post_request",
            return_value=(
                {"new_transaction": {"payment_url": "https://example.com/pay"}},
                response,
            ),
        ),
        pytest.raises(RedirectNeeded),
    ):
        provider.get_form(payment)

    payment.save.assert_called_once()


def test_get_form_truncates_long_description(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    payment.description = "A" * 50
    response = MagicMock(status_code=200)

    with (
        patch(
            "payments.sofort.render_to_string",
            return_value="<xml />",
        ) as render,
        patch.object(
            provider,
            "post_request",
            return_value=(
                {"new_transaction": {"payment_url": "https://example.com/pay"}},
                response,
            ),
        ),
        pytest.raises(RedirectNeeded),
    ):
        provider.get_form(payment)

    context = render.call_args.args[1]
    assert context["description"] == "A" * 37 + "..."


def test_get_form_raises_payment_error_for_api_error(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    response = MagicMock(status_code=200)
    error_doc = {
        "errors": {
            "error": {
                "field": "amount",
                "message": "Invalid amount",
            }
        }
    }

    with (
        patch("payments.sofort.render_to_string", return_value="<xml />"),
        patch.object(provider, "post_request", return_value=(error_doc, response)),
        pytest.raises(Exception, match="Error in amount: Invalid amount"),
    ):
        provider.get_form(payment)


def test_get_form_returns_none_for_non_200_response(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    response = MagicMock(status_code=500)

    with (
        patch("payments.sofort.render_to_string", return_value="<xml />"),
        patch.object(
            provider,
            "post_request",
            return_value=({}, response),
        ),
    ):
        assert provider.get_form(payment) is None


def test_process_data_rejects_missing_transaction(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    from django.http import HttpResponseForbidden

    request = MagicMock()
    request.GET = {}

    response = provider.process_data(payment, request)

    assert isinstance(response, HttpResponseForbidden)
    assert response.content == b"FAILED"


def test_refund_uses_explicit_amount(
    payment: Payment,
    provider: SofortProvider,
) -> None:
    payment.extra_data = json.dumps(
        {
            "transactions": {
                "transaction_details": {
                    "sender": {
                        "holder": "John Doe",
                        "bic": "1234",
                        "iban": "abcd",
                    }
                }
            }
        }
    )
    response_doc = {"refund": "accepted"}

    with (
        patch("payments.sofort.render_to_string", return_value="<refund />") as render,
        patch.object(provider, "post_request", return_value=(response_doc, MagicMock())),
    ):
        result = provider.refund(payment, amount=25)

    assert result == 25
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.message == json.dumps(response_doc)
    assert render.call_args.args[1]["amount"] == 25
