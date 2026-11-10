from __future__ import annotations

import json
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from payments import PaymentError
from payments import PaymentStatus
from payments import PurchasedItem
from payments import RedirectNeeded

from . import StripeProviderV3

# Secret key from https://stripe.com/docs/api/authentication
API_KEY = "test_key_placeholder"
API_KEY_BAD = "aaaaaaa123"


class payment_attrs:
    session: dict = {}


class Payment(Mock):
    id = 1
    description = "payment"
    currency = "USD"
    delivery = 0
    status = PaymentStatus.WAITING
    message = None
    tax = 0
    total = 100
    captured_amount = 0
    transaction_id: str | None = None
    billing_email = "john@doe.com"
    attrs = payment_attrs()

    def change_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message

    def get_failure_url(self) -> str:
        return "http://cancel.com"

    def get_process_url(self) -> str:
        return "http://example.com"

    def get_purchased_items(self) -> list[PurchasedItem]:
        return []

    def get_success_url(self) -> str:
        return "http://success.com"


def test_provider_create_session_success() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "id": "cs_test_...",
        "url": "https://checkout.stripe.com/c/pay/cs_test_...",
        "status": "open",
        "payment_status": "unpaid",
        "payment_intent": "pi_...",
    }
    with (
        patch("stripe.checkout.Session.create", return_value=return_value),
        pytest.raises(RedirectNeeded),
    ):
        provider.get_form(payment)

    assert "url" in payment.attrs.session
    assert "id" in payment.attrs.session
    assert payment.status == PaymentStatus.WAITING


def test_provider_create_session_failure() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.create") as f_session:
        f_session.side_effect = PaymentError("Error")
        with pytest.raises(PaymentError):
            provider.get_form(payment)

    assert payment.status == PaymentStatus.ERROR


def test_provider_create_session_failure_no_url() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "status": "open",
        "payment_status": "unpaid",
        "payment_intent": "pi_...",
    }
    with (
        patch("stripe.checkout.Session.create", return_value=return_value),
        pytest.raises(PaymentError),
    ):
        provider.get_form(payment)

    assert "url" not in payment.attrs.session
    assert "id" not in payment.attrs.session


def test_provider_create_session_failure_with_transaction_id() -> None:
    payment = Payment()
    payment.transaction_id = "transaction-id"
    provider = StripeProviderV3(api_key=API_KEY)
    with patch("stripe.checkout.Session.create"), pytest.raises(PaymentError):
        provider.create_session(payment)


def test_provider_create_session_success_with_billing_name() -> None:
    payment = Payment()
    payment.billing_first_name = "Billy"
    payment.billing_last_name = "Ngname"
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.create") as create_session:
        provider.create_session(payment)

    create_session.assert_called_once()
    _, kwargs = create_session.call_args
    assert kwargs["metadata"]["customer_name"] == "Billy Ngname"


def test_provider_status_confirmed():
    payment = Payment()
    payment.attrs = payment_attrs()
    payment.transaction_id = "cs_test_..."
    provider = StripeProviderV3(api_key=API_KEY)

    class MockSession:
        payment_status = "paid"

    with patch("stripe.checkout.Session.retrieve", return_value=MockSession()):
        provider.status(payment)

    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total


def test_provider_status_not_paid():
    payment = Payment()
    payment.transaction_id = "cs_test_..."
    provider = StripeProviderV3(api_key=API_KEY)

    class MockSession:
        payment_status = "unpaid"

    with patch("stripe.checkout.Session.retrieve", return_value=MockSession()):
        provider.status(payment)

    assert payment.status == PaymentStatus.WAITING
    assert payment.captured_amount == 0


def test_provider_refund_failure_bad_status() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY)
    with pytest.raises(PaymentError):
        provider.refund(payment)


def test_provider_refund_failure_no_payment_intent() -> None:
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    assert isinstance(payment.attrs.session, dict)
    del payment.attrs.session["payment_intent"]
    provider = StripeProviderV3(api_key=API_KEY)
    with pytest.raises(PaymentError):
        provider.refund(payment)


def test_provider_refund_failure_stripe_error() -> None:
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    provider = StripeProviderV3(api_key=API_KEY)
    with patch("stripe.Refund.create") as f_refund:
        f_refund.side_effect = PaymentError("Stripe error")
        with pytest.raises(PaymentError):
            provider.refund(payment)


## Provider.refund() should not change the payment status.
## Status management is handled by BasePayment.refund().
## Refunds should be performed via payment.refund() (see docs/refund.rst).
def test_provider_refund_success() -> None:
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    payment.attrs.session["payment_intent"] = "pi_..."
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "id": "re_...",
        "payment_status": "succeeded",
        "amount": 100,
    }

    with patch("stripe.Refund.create", return_value=return_value):
        provider.refund(payment)

    assert payment.status == PaymentStatus.CONFIRMED


def test_provider_refund_returns_currency_units():
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    payment.total = 30
    payment.currency = "USD"
    payment.attrs.session["payment_intent"] = "pi_..."
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "id": "re_...",
        "payment_status": "succeeded",
        "amount": 3000,
    }

    with patch("stripe.Refund.create", return_value=return_value) as mock_create:
        result = provider.refund(payment)
        mock_create.assert_called_once_with(
            payment_intent="pi_...",
            amount=3000,
            reason="requested_by_customer",
        )

    assert result == 30


def test_provider_refund_partial_returns_currency_units():
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    payment.total = 30
    payment.currency = "USD"
    payment.attrs.session["payment_intent"] = "pi_..."
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "id": "re_...",
        "payment_status": "succeeded",
        "amount": 1500,
    }

    with patch("stripe.Refund.create", return_value=return_value) as mock_create:
        result = provider.refund(payment, amount=15)
        mock_create.assert_called_once_with(
            payment_intent="pi_...",
            amount=1500,
            reason="requested_by_customer",
        )

    assert result == 15


def test_provider_refund_zero_decimal_currency_returns_currency_units():
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    payment.total = 3000
    payment.currency = "JPY"
    payment.attrs.session["payment_intent"] = "pi_..."
    provider = StripeProviderV3(api_key=API_KEY)
    return_value = {
        "id": "re_...",
        "payment_status": "succeeded",
        "amount": 3000,
    }

    with patch("stripe.Refund.create", return_value=return_value) as mock_create:
        result = provider.refund(payment, amount=3000)
        mock_create.assert_called_once_with(
            payment_intent="pi_...",
            amount=3000,
            reason="requested_by_customer",
        )

    assert result == 3000


def test_cancel_with_transaction_id():
    payment = Payment()
    payment.transaction_id = "cs_test_..."
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.expire") as mock_expire:
        provider.cancel(payment)
        mock_expire.assert_called_once_with("cs_test_...")


def test_cancel_without_transaction_id():
    payment = Payment()
    payment.transaction_id = None
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.expire") as mock_expire:
        provider.cancel(payment)
        mock_expire.assert_not_called()


def test_cancel_stripe_error():
    payment = Payment()
    payment.transaction_id = "cs_test_..."
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.expire") as mock_expire:
        mock_expire.side_effect = PaymentError("Stripe error")
        with pytest.raises(PaymentError):
            provider.cancel(payment)


def _make_webhook_request(event_type, session_status, payment_status):
    body = json.dumps(
        {
            "type": event_type,
            "data": {
                "object": {
                    "status": session_status,
                    "payment_status": payment_status,
                    "payment_intent": "pi_...",
                }
            },
        }
    )
    request = Mock()
    request.body = body
    return request


def test_process_data_sets_captured_amount_on_payment():
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        event_type="checkout.session.completed",
        session_status="complete",
        payment_status="paid",
    )

    provider.process_data(payment, request)

    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total


def test_process_data_does_not_set_captured_amount_on_expiry():
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        event_type="checkout.session.expired",
        session_status="expired",
        payment_status="unpaid",
    )

    provider.process_data(payment, request)

    assert payment.status == PaymentStatus.REJECTED
    assert payment.captured_amount == 0


def test_process_data_does_not_overwrite_cancelled_on_expiry():
    payment = Payment()
    payment.status = PaymentStatus.CANCELLED
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        event_type="checkout.session.expired",
        session_status="expired",
        payment_status="unpaid",
    )

    provider.process_data(payment, request)

    assert payment.status == PaymentStatus.CANCELLED


def test_get_form_existing_transaction_redirects() -> None:
    payment = Payment()
    payment.transaction_id = "existing-session"
    payment.attrs.session = {"url": "https://checkout.stripe.com/test"}
    provider = StripeProviderV3(api_key=API_KEY)

    with pytest.raises(RedirectNeeded):
        provider.get_form(payment)


def test_create_session_without_billing_email_or_name() -> None:
    payment = Payment()
    payment.billing_email = None
    payment.billing_first_name = None
    payment.billing_last_name = None
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.create") as create_session:
        provider.create_session(payment)

    _, kwargs = create_session.call_args
    assert "customer_email" not in kwargs
    assert "metadata" not in kwargs


def test_create_session_uses_payment_pk_when_token_disabled() -> None:
    payment = Payment()
    payment.pk = 123
    provider = StripeProviderV3(api_key=API_KEY, use_token=False)

    with patch("stripe.checkout.Session.create") as create_session:
        provider.create_session(payment)

    _, kwargs = create_session.call_args
    assert kwargs["client_reference_id"] == 123


def test_create_session_raises_payment_error_for_stripe_error() -> None:
    import stripe

    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY)

    with (
        patch(
            "stripe.checkout.Session.create",
            side_effect=stripe.StripeError("Stripe failure"),
        ),
        pytest.raises(PaymentError),
    ):
        provider.create_session(payment)


def test_cancel_raises_payment_error_for_stripe_error() -> None:
    import stripe

    payment = Payment()
    payment.transaction_id = "session-id"
    provider = StripeProviderV3(api_key=API_KEY)

    with (
        patch(
            "stripe.checkout.Session.expire",
            side_effect=stripe.StripeError("Stripe failure"),
        ),
        pytest.raises(PaymentError),
    ):
        provider.cancel(payment)


def test_refund_raises_payment_error_for_stripe_error() -> None:
    import stripe

    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    payment.attrs.session = {"payment_intent": "pi_test"}
    provider = StripeProviderV3(api_key=API_KEY)

    with (
        patch(
            "stripe.Refund.create",
            side_effect=stripe.StripeError("Stripe failure"),
        ),
        pytest.raises(PaymentError),
    ):
        provider.refund(payment)


def test_status_does_not_retrieve_non_waiting_payment() -> None:
    payment = Payment()
    payment.status = PaymentStatus.CONFIRMED
    provider = StripeProviderV3(api_key=API_KEY)

    with patch("stripe.checkout.Session.retrieve") as retrieve:
        result = provider.status(payment)

    retrieve.assert_not_called()
    assert result is payment


def test_return_event_payload_requires_signature_header() -> None:
    from django.test import RequestFactory

    payment = Payment()
    provider = StripeProviderV3(
        api_key=API_KEY,
        endpoint_secret="endpoint-secret",
        secure_endpoint=True,
    )
    request = RequestFactory().post(
        "/webhook/",
        data=b'{"type": "checkout.session.completed"}',
        content_type="application/json",
    )

    with pytest.raises(PaymentError) as exc:
        provider.return_event_payload(request)

    assert exc.value.code == 400


def test_return_event_payload_constructs_verified_event() -> None:
    from django.test import RequestFactory

    payment = Payment()
    provider = StripeProviderV3(
        api_key=API_KEY,
        endpoint_secret="endpoint-secret",
        secure_endpoint=True,
    )
    request = RequestFactory().post(
        "/webhook/",
        data=b'{"type": "checkout.session.completed"}',
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="test-signature",
    )
    expected_event = {"type": "checkout.session.completed"}

    with patch(
        "stripe.Webhook.construct_event",
        return_value=expected_event,
    ) as construct_event:
        result = provider.return_event_payload(request)

    assert result == expected_event
    construct_event.assert_called_once_with(
        request.body,
        "test-signature",
        "endpoint-secret",
    )


def test_get_token_from_request_returns_client_reference_id() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        "checkout.session.completed",
        "complete",
        "paid",
    )
    event = {
        "data": {
            "object": {
                "client_reference_id": "payment-token",
            }
        }
    }

    with patch.object(provider, "return_event_payload", return_value=event):
        result = provider.get_token_from_request(payment, request)

    assert result == "payment-token"


def test_get_token_from_request_raises_when_reference_missing() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        "checkout.session.completed",
        "complete",
        "paid",
    )
    event = {"data": {"object": {}}}

    with (
        patch.object(provider, "return_event_payload", return_value=event),
        pytest.raises(PaymentError) as exc,
    ):
        provider.get_token_from_request(payment, request)

    assert exc.value.code == 400


def test_process_data_ignores_unrecognized_event() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        "customer.created",
        "complete",
        "paid",
    )

    result = provider.process_data(payment, request)

    assert result.status_code == 200
    assert payment.status == PaymentStatus.WAITING


def test_process_data_raises_when_session_object_missing() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = Mock()
    request.body = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {},
        }
    )

    with pytest.raises(PaymentError) as exc:
        provider.process_data(payment, request)

    assert exc.value.code == 400


def test_process_data_does_not_confirm_unpaid_completed_session() -> None:
    payment = Payment()
    provider = StripeProviderV3(api_key=API_KEY, secure_endpoint=False)
    request = _make_webhook_request(
        event_type="checkout.session.completed",
        session_status="complete",
        payment_status="unpaid",
    )

    result = provider.process_data(payment, request)

    assert result.status_code == 200
    assert payment.status == PaymentStatus.WAITING
    assert payment.captured_amount == 0
    assert payment.attrs.session["payment_status"] == "unpaid"
