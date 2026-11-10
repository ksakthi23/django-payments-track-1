from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from payments import PaymentError
from payments import get_payment_model


def test_get_payment_model_returns_registered_model() -> None:
    expected_model = object()

    with override_settings(PAYMENT_MODEL="test_app.Payment"):
        with patch(
            "payments.apps.get_model", return_value=expected_model
        ) as mocked:
            result = get_payment_model()

    assert result is expected_model
    mocked.assert_called_once_with("test_app", "Payment")


@pytest.mark.parametrize("payment_model", ["invalid", None])
def test_get_payment_model_rejects_invalid_setting(
    payment_model: str | None,
) -> None:
    with override_settings(PAYMENT_MODEL=payment_model):
        with pytest.raises(
            ImproperlyConfigured,
            match="PAYMENT_MODEL must be of the form",
        ):
            get_payment_model()


def test_get_payment_model_rejects_uninstalled_model() -> None:
    with override_settings(PAYMENT_MODEL="missing_app.Payment"):
        with patch("payments.apps.get_model", return_value=None):
            with pytest.raises(
                ImproperlyConfigured, match="has not been installed"
            ):
                get_payment_model()


def test_payment_error_stores_message_and_optional_details() -> None:
    error = PaymentError("Payment failed")

    assert str(error) == "Payment failed"
    assert error.code is None
    assert error.gateway_message is None


def test_payment_error_stores_code_and_gateway_message() -> None:
    error = PaymentError(
        "Payment declined",
        code="card_declined",
        gateway_message="The card was declined",
    )

    assert str(error) == "Payment declined"
    assert error.code == "card_declined"
    assert error.gateway_message == "The card was declined"
