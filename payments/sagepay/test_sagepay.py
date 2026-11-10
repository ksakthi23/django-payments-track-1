from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from payments import PaymentStatus

from . import SagepayProvider

VENDOR = "abcd1234"
ENCRYPTION_KEY = "1234abdd1234abcd"


class Payment(Mock):
    id = 1
    variant = "sagepay"
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING
    transaction_id = None
    captured_amount = 0
    billing_first_name = "John"

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
def provider() -> SagepayProvider:
    return SagepayProvider(vendor=VENDOR, encryption_key=ENCRYPTION_KEY)


@patch("payments.sagepay.redirect")
def test_provider_raises_redirect_needed_on_success(
    mocked_redirect: MagicMock,
    payment: Payment,
    provider: SagepayProvider,
) -> None:
    raw_data = {"Status": "OK"}
    data = "&".join("{}={}".format(*kv) for kv in raw_data.items())
    with patch.object(SagepayProvider, "aes_dec", return_value=data):
        provider.process_data(payment, MagicMock())
        assert payment.status == PaymentStatus.CONFIRMED
        assert payment.captured_amount == payment.total


@patch("payments.sagepay.redirect")
def test_provider_raises_redirect_needed_on_failure(
    mocked_redirect: MagicMock,
    payment: Payment,
    provider: SagepayProvider,
) -> None:
    raw_data = {"Status": ""}
    data = "&".join("{}={}".format(*kv) for kv in raw_data.items())
    with patch.object(SagepayProvider, "aes_dec", return_value=data):
        provider.process_data(payment, MagicMock())
        assert payment.status == PaymentStatus.REJECTED
        assert payment.captured_amount == 0


def test_provider_encrypts_data(payment: Payment, provider: SagepayProvider) -> None:
    data = provider.get_hidden_fields(payment)
    decrypted_data = provider.aes_dec(data["Crypt"])
    assert payment.billing_first_name in str(decrypted_data)


def test_encrypt_method_returns_valid_data(provider: SagepayProvider) -> None:
    encrypted = provider.aes_enc("mirumee")
    assert encrypted == "@e63c293672f50b9c8e291831facb4e4f"


def test_provider_rejects_pre_authorization() -> None:
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(
        ImproperlyConfigured,
        match="Sagepay does not support pre-authorization",
    ):
        SagepayProvider(
            vendor=VENDOR,
            encryption_key=ENCRYPTION_KEY,
            capture=False,
        )


def test_decrypt_method_accepts_bytes(provider: SagepayProvider) -> None:
    encrypted = provider.aes_enc("sagepay bytes test")

    decrypted = provider.aes_dec(encrypted.encode("utf-8"))

    assert decrypted.startswith("sagepay bytes test")
    assert decrypted.endswith(chr(14) * 14)


def test_hidden_fields_include_us_billing_and_delivery_states(
    payment: Payment,
    provider: SagepayProvider,
) -> None:
    payment.billing_country_code = "US"
    payment.billing_country_area = "CA"

    fields = provider.get_hidden_fields(payment)
    decrypted_data = provider.aes_dec(fields["Crypt"])

    assert "BillingState=CA" in decrypted_data
    assert "DeliveryState=CA" in decrypted_data


def test_process_data_does_not_change_non_waiting_payment(
    payment: Payment,
    provider: SagepayProvider,
) -> None:
    payment.status = PaymentStatus.CONFIRMED
    request = MagicMock()
    request.GET = {"crypt": "encrypted-response"}

    with (
        patch.object(SagepayProvider, "aes_dec", return_value="Status=OK"),
        patch("payments.sagepay.redirect") as mocked_redirect,
        patch.object(payment, "change_status") as mocked_change_status,
    ):
        provider.process_data(payment, request)

    mocked_redirect.assert_called_once_with(payment.get_success_url())
    mocked_change_status.assert_not_called()
    assert payment.status == PaymentStatus.CONFIRMED
