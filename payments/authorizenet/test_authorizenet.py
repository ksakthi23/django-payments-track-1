from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from django.utils.timezone import localdate

from payments import PaymentStatus
from payments import RedirectNeeded

from . import AuthorizeNetProvider

LOGIN_ID = "abcd1234"
TRANSACTION_KEY = "1234abdd"

PROCESS_DATA = {
    "number": "4007000000027",
    "expiration_0": "5",
    "expiration_1": localdate().year + 1,
    "cvv2": "123",
}

STATUS_CONFIRMED = "1"
STATUS_DECLINED = "2"
ERROR_PROCESSING = "3"


class Payment(Mock):
    id = 1
    variant = "authorizenet"
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING
    transaction_id = None
    captured_amount = 0
    message = ""

    def get_process_url(self) -> str:
        return "http://example.com"

    def get_failure_url(self) -> str:
        return "http://cancel.com"

    def get_success_url(self) -> str:
        return "http://success.com"

    def change_status(
        self,
        status: str,
        message: str = "",
        update_fields: list[str] | None = None,
    ) -> None:
        self.status = status
        self.message = message


@pytest.fixture
def payment() -> Payment:
    return Payment()


def test_provider_redirects_to_success_on_payment_success(payment: Payment) -> None:
    provider = AuthorizeNetProvider(login_id=LOGIN_ID, transaction_key=TRANSACTION_KEY)

    response_data = [
        STATUS_CONFIRMED,
        "",
        "",
        "This transaction has been approved.",
        "",
        "",
        "1234",
    ]

    with patch("requests.post") as mocked_post:
        post = MagicMock()
        post.text = "|".join(response_data)
        mocked_post.return_value = post
        with pytest.raises(RedirectNeeded) as exc:
            provider.get_form(payment, data=PROCESS_DATA)
        assert str(exc.value) == payment.get_success_url()

    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total


def test_provider_shows_validation_error_message(payment: Payment) -> None:
    provider = AuthorizeNetProvider(login_id=LOGIN_ID, transaction_key=TRANSACTION_KEY)

    error_msg = "The merchant does not accept this type of credit card."
    response_data = [ERROR_PROCESSING, "", "", error_msg, "", "", "1234"]

    with patch("requests.post") as mocked_post:
        post = MagicMock()
        post.text = "|".join(response_data)
        mocked_post.return_value = post
        form = provider.get_form(payment, data=PROCESS_DATA)
        assert form.errors["__all__"][0] == error_msg
        assert not form.is_valid()

    assert payment.status == "error"
    assert payment.captured_amount == 0
    assert payment.message == error_msg


def test_provider_shows_rejection_error_message(payment: Payment) -> None:
    provider = AuthorizeNetProvider(login_id=LOGIN_ID, transaction_key=TRANSACTION_KEY)

    error_msg = " This transaction has been declined."
    response_data = [STATUS_DECLINED, "", "", error_msg, "", "", "1234"]

    with patch("requests.post") as mocked_post:
        post = MagicMock()
        post.text = "|".join(response_data)
        mocked_post.return_value = post
        form = provider.get_form(payment, data=PROCESS_DATA)
        assert form.errors["__all__"][0] == error_msg
        assert not form.is_valid()

    assert payment.status == "rejected"
    assert payment.captured_amount == 0
    assert payment.message == error_msg


def test_provider_handles_incomplete_response(payment: Payment) -> None:
    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )

    with patch("requests.post") as mocked_post:
        post = MagicMock()
        post.ok = True
        post.text = "1|||Approved"
        mocked_post.return_value = post

        form = provider.get_form(payment, data=PROCESS_DATA)

    assert not form.is_valid()
    assert form.errors["__all__"][0] == (
        "Invalid or incomplete response received from Authorize.Net."
    )
    assert payment.status == PaymentStatus.ERROR
    assert payment.captured_amount == 0


def test_provider_handles_missing_transaction_id(
    payment: Payment,
) -> None:
    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )

    response_data = ["1", "", "", "Approved", "", "", ""]

    with patch("requests.post") as mocked_post:
        post = MagicMock()
        post.ok = True
        post.text = "|".join(response_data)
        mocked_post.return_value = post

        form = provider.get_form(payment, data=PROCESS_DATA)

    assert not form.is_valid()
    assert form.errors["__all__"][0] == (
        "Invalid or incomplete response received from Authorize.Net."
    )
    assert payment.status == PaymentStatus.ERROR
    assert payment.captured_amount == 0
    assert payment.transaction_id is None


def test_provider_rejects_pre_authorization() -> None:
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(
        ImproperlyConfigured,
        match="Authorize.Net does not support pre-authorization",
    ):
        AuthorizeNetProvider(
            login_id=LOGIN_ID,
            transaction_key=TRANSACTION_KEY,
            capture=False,
        )


def test_get_product_data_without_extra_data(payment: Payment) -> None:
    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )

    data = provider.get_product_data(payment)

    assert data["x_login"] == LOGIN_ID
    assert data["x_tran_key"] == TRANSACTION_KEY
    assert data["x_type"] == "AUTH_CAPTURE"
    assert data["x_method"] == "CC"


def test_get_form_does_not_change_non_waiting_payment(payment: Payment) -> None:
    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )
    payment.status = PaymentStatus.CONFIRMED

    form = provider.get_form(payment)

    assert form is not None
    assert payment.status == PaymentStatus.CONFIRMED


def test_process_data_returns_forbidden_response(
    payment: Payment,
) -> None:
    from django.http import HttpResponseForbidden

    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )

    response = provider.process_data(payment, Mock())

    assert isinstance(response, HttpResponseForbidden)
    assert response.status_code == 403
    assert response.content == b"FAILED"


def test_provider_does_not_charge_payment_with_existing_transaction(
    payment: Payment,
) -> None:
    provider = AuthorizeNetProvider(
        login_id=LOGIN_ID,
        transaction_key=TRANSACTION_KEY,
    )
    payment.transaction_id = "EXISTING-123"

    with patch("requests.post") as mocked_post:
        with pytest.raises(RedirectNeeded):
            provider.get_form(payment, data=PROCESS_DATA)

    mocked_post.assert_not_called()
    assert payment.transaction_id == "EXISTING-123"
