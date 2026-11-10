from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from django.utils.timezone import localdate

from payments import PaymentStatus
from payments import RedirectNeeded

from . import BraintreeProvider

MERCHANT_ID = "test11"
PUBLIC_KEY = "abcd1234"
PRIVATE_KEY = "1234abcd"

PROCESS_DATA = {
    "name": "John Doe",
    "number": "371449635398431",
    "expiration_0": "5",
    "expiration_1": localdate().year + 1,
    "cvv2": "1234",
}


class Payment(Mock):
    id = 1
    variant = "braintree"
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING
    transaction_id = None
    captured_amount = 0

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


def test_provider_redirects_to_success_on_payment_success(payment: Payment) -> None:
    provider = BraintreeProvider(MERCHANT_ID, PUBLIC_KEY, PRIVATE_KEY)
    transaction_id = "12345"
    with (
        patch("braintree.Transaction"),
        patch("braintree.Transaction.sale") as mocked_sale,
    ):
        sale = MagicMock()
        sale.is_success = True
        sale.transaction.id = transaction_id
        mocked_sale.return_value = sale
        with pytest.raises(RedirectNeeded) as exc:
            provider.get_form(payment, data=PROCESS_DATA)
        url = exc.value.args[0]
        assert url == payment.get_success_url()
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total
    assert payment.transaction_id == transaction_id


def test_provider_shows_validation_error_message(payment: Payment) -> None:
    provider = BraintreeProvider(MERCHANT_ID, PUBLIC_KEY, PRIVATE_KEY)
    error_msg = "error message"
    with (
        patch("braintree.Transaction"),
        patch("braintree.Transaction.sale") as mocked_sale,
    ):
        sale = MagicMock()
        sale.is_success = False
        sale.message = error_msg
        mocked_sale.return_value = sale
        form = provider.get_form(payment, data=PROCESS_DATA)
        assert form.errors["__all__"][0] == error_msg
    assert payment.status == PaymentStatus.ERROR
    assert payment.captured_amount == 0




def test_provider_uses_production_environment() -> None:
    with patch("braintree.Configuration.configure") as mocked_configure:
        BraintreeProvider(
            MERCHANT_ID,
            PUBLIC_KEY,
            PRIVATE_KEY,
            sandbox=False,
        )

    assert mocked_configure.call_args.args[0] == __import__(
        "braintree"
    ).Environment.Production


def test_provider_rejects_pre_authorization() -> None:
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(
        ImproperlyConfigured,
        match="Braintree does not support pre-authorization",
    ):
        BraintreeProvider(
            MERCHANT_ID,
            PUBLIC_KEY,
            PRIVATE_KEY,
            capture=False,
        )


def test_provider_does_not_change_non_waiting_payment(
    payment: Payment,
) -> None:
    provider = BraintreeProvider(MERCHANT_ID, PUBLIC_KEY, PRIVATE_KEY)
    payment.status = PaymentStatus.CONFIRMED

    with patch("payments.braintree.BraintreePaymentForm") as mocked_form:
        mocked_form.return_value.is_valid.return_value = False
        provider.get_form(payment)

    assert payment.status == PaymentStatus.CONFIRMED
    mocked_form.assert_called_once()


def test_get_credit_card_clean_data_returns_none_without_cleaned_data(
    payment: Payment,
) -> None:
    from payments.braintree.forms import BraintreePaymentForm

    form = BraintreePaymentForm(data=PROCESS_DATA, payment=payment, provider=None)
    form.cleaned_data = {}

    assert form.get_credit_card_clean_data() is None


def test_form_does_not_create_sale_when_validation_fails(
    payment: Payment,
) -> None:
    from payments.braintree.forms import BraintreePaymentForm

    provider = BraintreeProvider(MERCHANT_ID, PUBLIC_KEY, PRIVATE_KEY)

    with patch("braintree.Transaction.sale") as mocked_sale:
        form = BraintreePaymentForm(
            data={"name": "John Doe"},
            payment=payment,
            provider=provider,
        )
        assert not form.is_valid()

    mocked_sale.assert_not_called()
