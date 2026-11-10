from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from django.utils import timezone
from django.utils.timezone import localdate
from requests import HTTPError

from payments import PaymentError
from payments import PaymentStatus
from payments import PurchasedItem
from payments import RedirectNeeded

from . import PaypalCardProvider
from . import PaypalProvider

CLIENT_ID = "abc123"
PAYMENT_TOKEN = "5a4dae68-2715-4b1e-8bb2-2c2dbe9255f6"
SECRET = "123abc"
VARIANT = "wallet"

PROCESS_DATA = {
    "name": "John Doe",
    "number": "371449635398431",
    "expiration_0": "5",
    "expiration_1": localdate().year + 1,
    "cvv2": "1234",
}


class PaymentQuerySet(Mock):
    __payments: dict = {}

    def create(self, **kwargs: object) -> Payment:
        if kwargs:
            raise NotImplementedError(f"arguments not supported yet: {kwargs}")
        id_ = max(self.__payments) + 1 if self.__payments else 1
        self.__payments[id_] = {}
        payment = Payment()
        payment.id = id_
        payment.save()
        return payment

    def get(self, *args: object, **kwargs: object) -> Payment:
        if args or kwargs:
            return self.filter(*args, **kwargs).get()
        payment = Payment()
        (payment_fields,) = self.__payments.values()
        for payment_field_name, payment_field_value in payment_fields.items():
            setattr(payment, payment_field_name, deepcopy(payment_field_value))
        return payment

    def filter(
        self,
        *args: object,
        pk: object = None,
        **kwargs: object,
    ) -> PaymentQuerySet:
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        if pk is not None:
            return PaymentQuerySet(
                {pk_: payment for pk_, payment in self.__payments.items() if pk_ == pk}
            )
        return self

    def update(self, **kwargs: object) -> None:
        for payment in self.__payments.values():
            for field_name, field_value in kwargs.items():
                if not any(
                    field.name == field_name
                    for field in Payment._meta.get_fields(
                        include_parents=True, include_hidden=True
                    )
                ):
                    raise NotImplementedError(
                        f"updating unknown field not supported yet: {field_name}"
                    )
                payment[field_name] = deepcopy(field_value)

    def delete(self) -> None:
        self.__payments.clear()


class Payment(Mock):
    objects = PaymentQuerySet()

    id = 1
    description = "payment"
    currency = "USD"
    delivery = Decimal(10)
    status = PaymentStatus.WAITING
    tax = Decimal(10)
    token = PAYMENT_TOKEN
    total = Decimal(220)
    captured_amount = Decimal(0)
    variant = VARIANT
    transaction_id = None
    message = ""
    extra_data = json.dumps(
        {
            "links": {
                "approval_url": None,
                "capture": {"href": "http://capture.com"},
                "refund": {"href": "http://refund.com"},
                "execute": {"href": "http://execute.com"},
            }
        }
    )

    @property
    def pk(self) -> int:
        return self.id

    def change_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message
        self.save(update_fields=["status", "message"])

    def get_failure_url(self) -> str:
        return "http://cancel.com"

    def get_process_url(self) -> str:
        return "http://example.com"

    def get_purchased_items(self) -> list[PurchasedItem]:
        return [
            PurchasedItem(
                name="foo", quantity=10, price=Decimal(20), currency="USD", sku="bar"
            )
        ]

    def get_success_url(self) -> str:
        return "http://success.com"

    def save(
        self,
        *args: object,
        update_fields: set[str] | list[str] | None = None,
        **kwargs: object,
    ) -> None:
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        if update_fields is None:
            update_fields = {
                field.name
                for field in self._meta.get_fields(
                    include_parents=True, include_hidden=True
                )
            }
        Payment.objects.filter(pk=self.pk).update(
            **{field: getattr(self, field) for field in update_fields}
        )

    def refresh_from_db(self, *args: object, **kwargs: object) -> None:
        if args or kwargs:
            raise NotImplementedError(f"arguments not supported yet: {args}, {kwargs}")
        payment_from_db = Payment.objects.get(pk=self.pk)
        for field in self._meta.get_fields(include_parents=True, include_hidden=True):
            field_value_from_db = getattr(payment_from_db, field.name)
            setattr(self, field.name, field_value_from_db)

    class Meta(Mock):
        def get_fields(
            self,
            include_parents: bool = True,
            include_hidden: bool = False,
        ) -> tuple[Mock, ...]:
            fields = []
            for field_name in (
                "id",
                "description",
                "currency",
                "delivery",
                "status",
                "tax",
                "token",
                "total",
                "captured_amount",
                "variant",
                "transaction_id",
                "message",
                "extra_data",
            ):
                field = Mock()
                field.name = field_name
                fields.append(field)
            return tuple(fields)

    _meta = Meta()


# PaypalProvider tests


@pytest.fixture
def paypal_payment() -> Payment:
    Payment.objects.delete()
    return Payment.objects.create()


@pytest.fixture
def paypal_provider() -> PaypalProvider:
    return PaypalProvider(secret=SECRET, client_id=CLIENT_ID)


def test_provider_raises_redirect_needed_on_success(
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        transaction_id = "1234"
        data = MagicMock()
        data.return_value = {
            "id": transaction_id,
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "links": [{"rel": "approval_url", "href": "http://approval_url.com"}],
        }
        post = MagicMock()
        post.json = data
        post.status_code = 200
        mocked_post.return_value = post
        mocked_request.return_value = post
        with pytest.raises(RedirectNeeded):
            paypal_provider.get_form(payment=paypal_payment)

    assert paypal_payment.status == PaymentStatus.WAITING
    assert paypal_payment.captured_amount == Decimal(0)
    assert paypal_payment.transaction_id == transaction_id


@patch("requests.request")
@patch("requests.post")
def test_provider_captures_payment(
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    data = MagicMock()
    data.return_value = {
        "state": "completed",
        "token_type": "test_token_type",
        "access_token": "test_access_token",
    }
    post = MagicMock()
    post.json = data
    post.status_code = 200
    mocked_post.return_value = post
    mocked_request.return_value = post
    paypal_provider.capture(paypal_payment)
    assert paypal_payment.status == PaymentStatus.CONFIRMED


@patch("requests.post")
def test_provider_handles_captured_payment(
    mocked_post: MagicMock, paypal_payment: Payment, paypal_provider: PaypalProvider
) -> None:
    data = MagicMock()
    data.return_value = {"name": "AUTHORIZATION_ALREADY_COMPLETED"}
    response = MagicMock()
    response.json = data
    mocked_post.side_effect = HTTPError(response=response)
    paypal_provider.capture(paypal_payment)
    assert paypal_payment.status == PaymentStatus.CONFIRMED


@patch("requests.request")
@patch("requests.post")
def test_provider_refunds_payment_fully(
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    token = MagicMock()
    token.json.return_value = {
        "token_type": "test_token_type",
        "access_token": "test_access_token",
    }
    token.status_code = 200
    mocked_post.return_value = token
    refund = MagicMock()
    refund.json.return_value = {"amount": {"total": "220.00", "currency": "USD"}}
    refund.status_code = 200
    mocked_request.return_value = refund
    paypal_provider.refund(paypal_payment)
    mocked_request.assert_called_with(
        "post",
        "http://refund.com",
        headers={
            "Content-Type": "application/json",
            "Authorization": "test_token_type test_access_token",
        },
        data="{}",
    )
    assert paypal_payment.status == PaymentStatus.REFUNDED


@patch("requests.request")
@patch("requests.post")
def test_provider_refunds_payment_partially(
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    token = MagicMock()
    token.json.return_value = {
        "token_type": "test_token_type",
        "access_token": "test_access_token",
    }
    token.status_code = 200
    mocked_post.return_value = token
    refund = MagicMock()
    refund.json.return_value = {"amount": {"total": "1.00", "currency": "USD"}}
    refund.status_code = 200
    mocked_request.return_value = refund
    paypal_provider.refund(paypal_payment, amount=Decimal(1))
    mocked_request.assert_called_with(
        "post",
        "http://refund.com",
        headers={
            "Content-Type": "application/json",
            "Authorization": "test_token_type test_access_token",
        },
        data='{"amount": {"currency": "USD", "total": "1.00"}}',
    )
    assert paypal_payment.status == PaymentStatus.REFUNDED


@patch("requests.request")
@patch("requests.post")
@patch("payments.paypal.redirect")
def test_provider_redirects_on_success_captured_payment(
    mocked_redirect: MagicMock,
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    data = MagicMock()
    data.return_value = {
        "token_type": "test_token_type",
        "access_token": "test_access_token",
        "payer": {"payer_info": "test123"},
        "transactions": [
            {
                "related_resources": [
                    {"sale": {"links": ""}, "authorization": {"links": ""}}
                ]
            }
        ],
    }
    post = MagicMock()
    post.json = data
    post.status_code = 200
    mocked_post.return_value = post
    mocked_request.return_value = post

    request = MagicMock()
    request.GET = {"token": "test", "PayerID": "1234"}
    paypal_provider.process_data(paypal_payment, request)

    assert paypal_payment.status == PaymentStatus.CONFIRMED
    assert paypal_payment.captured_amount == paypal_payment.total
    paypal_payment.refresh_from_db()
    assert paypal_payment.status == PaymentStatus.CONFIRMED
    assert paypal_payment.captured_amount == paypal_payment.total


@patch("requests.request")
@patch("requests.post")
@patch("payments.paypal.redirect")
def test_provider_redirects_on_success_preauth_payment(
    mocked_redirect: MagicMock,
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
) -> None:
    data = MagicMock()
    data.return_value = {
        "token_type": "test_token_type",
        "access_token": "test_access_token",
        "payer": {"payer_info": "test123"},
        "transactions": [
            {
                "related_resources": [
                    {"sale": {"links": ""}, "authorization": {"links": ""}}
                ]
            }
        ],
    }
    post = MagicMock()
    post.json = data
    post.status_code = 200
    mocked_post.return_value = post
    mocked_request.return_value = post

    request = MagicMock()
    request.GET = {"token": "test", "PayerID": "1234"}
    provider = PaypalProvider(secret=SECRET, client_id=CLIENT_ID, capture=False)
    provider.process_data(paypal_payment, request)

    assert paypal_payment.status == PaymentStatus.PREAUTH
    assert paypal_payment.captured_amount == Decimal(0)
    paypal_payment.refresh_from_db()
    assert paypal_payment.status == PaymentStatus.PREAUTH
    assert paypal_payment.captured_amount == Decimal(0)


@patch("payments.paypal.redirect")
def test_provider_request_without_payerid_redirects_on_failure(
    mocked_redirect: MagicMock, paypal_payment: Payment, paypal_provider: PaypalProvider
) -> None:
    request = MagicMock()
    request.GET = {"token": "test", "PayerID": None}
    paypal_provider.process_data(paypal_payment, request)
    assert paypal_payment.status == PaymentStatus.REJECTED
    paypal_payment.refresh_from_db()
    assert paypal_payment.status == PaymentStatus.REJECTED


@patch("requests.request")
@patch("requests.post")
def test_provider_renews_access_token(
    mocked_post: MagicMock,
    mocked_request: MagicMock,
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    new_token = "new_test_token"
    response401 = MagicMock()
    response401.status_code = 401
    data = MagicMock()
    data.return_value = {"access_token": new_token, "token_type": "type"}
    response = MagicMock()
    response.json = data
    response.status_code = 200
    # The API call 401s, @authorize renews the token via oauth and retries.
    mocked_request.side_effect = [HTTPError(response=response401), response]
    mocked_post.return_value = response

    paypal_payment.created = timezone.now()
    paypal_payment.extra_data = json.dumps(
        {
            "auth_response": {
                "access_token": "expired_token",
                "token_type": "token type",
                "expires_in": 99999,
            }
        }
    )
    paypal_provider.create_payment(paypal_payment)
    payment_response = json.loads(paypal_payment.extra_data)["auth_response"]
    assert payment_response["access_token"] == new_token


# PaypalCardProvider tests


@pytest.fixture
def paypal_card_payment() -> Payment:
    return Payment(extra_data="")


@pytest.fixture
def paypal_card_provider() -> PaypalCardProvider:
    return PaypalCardProvider(secret=SECRET, client_id=CLIENT_ID)


def test_provider_raises_redirect_needed_on_success_captured_payment_card(
    paypal_card_payment: Payment, paypal_card_provider: PaypalCardProvider
) -> None:
    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        transaction_id = "1234"
        data = MagicMock()
        data.return_value = {
            "id": transaction_id,
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "transactions": [
                {
                    "related_resources": [
                        {
                            "sale": {
                                "links": [
                                    {"rel": "refund", "href": "http://refund.com"}
                                ]
                            }
                        }
                    ]
                }
            ],
        }
        post = MagicMock()
        post.json = data
        post.status_code = 200
        mocked_post.return_value = post
        mocked_request.return_value = post
        with pytest.raises(RedirectNeeded) as exc:
            paypal_card_provider.get_form(
                payment=paypal_card_payment, data=PROCESS_DATA
            )
        assert str(exc.value) == paypal_card_payment.get_success_url()

    links = paypal_card_provider._get_links(paypal_card_payment)
    assert paypal_card_payment.status == PaymentStatus.CONFIRMED
    assert paypal_card_payment.captured_amount == paypal_card_payment.total
    assert paypal_card_payment.transaction_id == transaction_id
    assert "refund" in links


def test_provider_raises_redirect_needed_on_success_preauth_payment_card(
    paypal_card_payment: Payment,
) -> None:
    provider = PaypalCardProvider(secret=SECRET, client_id=CLIENT_ID, capture=False)
    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        transaction_id = "1234"
        data = MagicMock()
        data.return_value = {
            "id": transaction_id,
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "transactions": [
                {
                    "related_resources": [
                        {
                            "authorization": {
                                "links": [
                                    {"rel": "refund", "href": "http://refund.com"},
                                    {"rel": "capture", "href": "http://capture.com"},
                                ]
                            }
                        }
                    ]
                }
            ],
        }
        post = MagicMock()
        post.json = data
        post.status_code = 200
        mocked_post.return_value = post
        mocked_request.return_value = post
        with pytest.raises(RedirectNeeded) as exc:
            provider.get_form(payment=paypal_card_payment, data=PROCESS_DATA)
        assert str(exc.value) == paypal_card_payment.get_success_url()

    links = provider._get_links(paypal_card_payment)
    assert paypal_card_payment.status == PaymentStatus.PREAUTH
    assert paypal_card_payment.captured_amount == Decimal(0)
    assert paypal_card_payment.transaction_id == transaction_id
    assert "capture" in links
    assert "refund" in links


def test_form_shows_validation_error_message(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    with patch("requests.post") as mocked_post:
        error_message = "error message"
        data = MagicMock()
        data.return_value = {"details": [{"issue": error_message}]}
        post = MagicMock()
        post.json = data
        post.status_code = 400
        mocked_post.side_effect = HTTPError(response=post)
        form = paypal_card_provider.get_form(
            payment=paypal_card_payment, data=PROCESS_DATA
        )
    assert paypal_card_payment.status == PaymentStatus.ERROR
    assert form.errors["__all__"][0] == error_message


def test_provider_get_with_none_payment(paypal_provider: PaypalProvider) -> None:
    """Test that get() method works with payment=None."""
    test_url = "http://example.com/api/test"
    expected_get_response_data = {"status": "success"}
    expected_token = "test_access_token"
    expected_token_type = "Bearer"

    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        # Mock for token acquisition
        token_response_mock = MagicMock()
        token_response_mock.json.return_value = {
            "access_token": expected_token,
            "token_type": expected_token_type,
            "expires_in": 3600,
        }
        token_response_mock.status_code = 200

        # Mock for the actual GET request
        get_response_mock = MagicMock()
        get_response_mock.json.return_value = expected_get_response_data
        get_response_mock.status_code = 200

        mocked_post.return_value = token_response_mock
        mocked_request.return_value = get_response_mock

        response_data = paypal_provider.get(None, test_url)

        mocked_post.assert_called_once_with(
            paypal_provider.oauth2_url,
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            auth=(paypal_provider.client_id, paypal_provider.secret),
        )

        mocked_request.assert_called_once_with(
            "get",
            test_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"{expected_token_type} {expected_token}",
            },
        )
        assert response_data == expected_get_response_data


def test_form_shows_internal_error_message(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        error_message = "error message"
        data = MagicMock()
        data.return_value = {
            "token_type": "test_token_type",
            "access_token": "test_access_token",
            "message": error_message,
        }
        post = MagicMock()
        post.status_code = 400
        post.json = data
        mocked_post.return_value = post
        mocked_request.return_value = post
        with pytest.raises(PaymentError):
            paypal_card_provider.get_form(
                payment=paypal_card_payment, data=PROCESS_DATA
            )
    assert paypal_card_payment.status == PaymentStatus.ERROR
    assert paypal_card_payment.message == error_message


def test_provider_get_with_none_payment_handles_401(
    paypal_provider: PaypalProvider,
) -> None:
    """Test that get() method with payment=None handles 401 errors correctly."""
    test_url = "http://example.com/api/test"
    expected_get_response_data = {"status": "success"}
    expected_token = "test_access_token"
    expected_token_type = "Bearer"

    with (
        patch("requests.post") as mocked_post,
        patch("requests.request") as mocked_request,
    ):
        # Mock for token acquisition (called twice due to 401 retry)
        token_response_mock = MagicMock()
        token_response_mock.json.return_value = {
            "access_token": expected_token,
            "token_type": expected_token_type,
            "expires_in": 3600,
        }
        token_response_mock.status_code = 200

        # Mock for the GET request - first 401, then success
        get_response_401 = MagicMock()
        get_response_401.status_code = 401

        get_response_200 = MagicMock()
        get_response_200.json.return_value = expected_get_response_data
        get_response_200.status_code = 200

        mocked_post.return_value = token_response_mock
        mocked_request.side_effect = [
            HTTPError(response=get_response_401),
            get_response_200,
        ]

        response_data = paypal_provider.get(None, test_url)

        # Should have called post twice (initial token + retry after 401)
        assert mocked_post.call_count == 2
        # Should have called the API twice (initial 401 + retry)
        assert mocked_request.call_count == 2
        assert response_data == expected_get_response_data


def test_http_request_handles_invalid_json(
    paypal_provider: PaypalProvider,
) -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError("Invalid JSON")

    with (
        patch.object(
            paypal_provider, "get_access_token", return_value="Bearer test"
        ),
        patch("requests.request", return_value=response),
    ):
        result = paypal_provider.http_request(None, "http://example.com")

    assert result == {}


def test_http_request_raises_generic_error_for_400_without_message(
    paypal_provider: PaypalProvider,
) -> None:
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {}

    with (
        patch.object(
            paypal_provider, "get_access_token", return_value="Bearer test"
        ),
        patch("requests.request", return_value=response),
        pytest.raises(PaymentError, match="Paypal error"),
    ):
        paypal_provider.http_request(None, "http://example.com")


def test_http_request_raises_generic_error_for_500(
    paypal_provider: PaypalProvider,
) -> None:
    response = MagicMock()
    response.status_code = 500
    response.json.return_value = {"error": "Internal server error"}

    with (
        patch.object(
            paypal_provider, "get_access_token", return_value="Bearer test"
        ),
        patch("requests.request", return_value=response),
        pytest.raises(PaymentError, match="Paypal error"),
    ):
        paypal_provider.http_request(None, "http://example.com")


def test_get_last_response_returns_regular_response(
    paypal_provider: PaypalProvider,
) -> None:
    payment = Mock(extra_data=json.dumps({"response": {"id": "test-123"}}))
    assert paypal_provider.get_last_response(payment) == {"id": "test-123"}


def test_process_data_without_token_returns_forbidden(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    request = Mock(GET={})
    response = paypal_provider.process_data(paypal_payment, request)
    assert response.status_code == 403


def test_process_data_confirmed_payment_without_payer_id_redirects_to_success(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    paypal_payment.status = PaymentStatus.CONFIRMED
    request = Mock(GET={"token": "test-token"})
    response = paypal_provider.process_data(paypal_payment, request)
    assert response.url == paypal_payment.get_success_url()


def test_process_data_payment_error_redirects_to_failure(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    request = Mock(GET={"token": "test-token", "PayerID": "test-payer"})
    with patch.object(
        paypal_provider,
        "execute_payment",
        side_effect=PaymentError("PayPal execution failed"),
    ):
        response = paypal_provider.process_data(paypal_payment, request)
    assert response.url == paypal_payment.get_failure_url()


def test_process_data_key_error_returns_bad_request(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    request = Mock(GET={"token": "test-token", "PayerID": "test-payer"})
    with patch.object(
        paypal_provider,
        "execute_payment",
        side_effect=KeyError("payer"),
    ):
        response = paypal_provider.process_data(paypal_payment, request)
    assert response.status_code == 400


def test_capture_returns_amount_for_partial_capture(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    amount = Decimal("50.00")
    with patch.object(
        paypal_provider,
        "post",
        return_value={"state": "partially_captured"},
    ):
        result = paypal_provider.capture(paypal_payment, amount)

    assert result == amount


def test_capture_returns_none_for_pending_state(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    with patch.object(
        paypal_provider,
        "post",
        return_value={"state": "pending"},
    ):
        result = paypal_provider.capture(paypal_payment)

    assert result is None
    assert paypal_payment.status == PaymentStatus.WAITING


def test_capture_raises_error_for_refunded_state(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    with (
        patch.object(
            paypal_provider,
            "post",
            return_value={"state": "refunded"},
        ),
        pytest.raises(PaymentError, match="Payment already refunded"),
    ):
        paypal_provider.capture(paypal_payment)

    assert paypal_payment.status == PaymentStatus.REFUNDED


def test_capture_returns_none_for_unknown_state(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    with patch.object(
        paypal_provider,
        "post",
        return_value={"state": "unknown"},
    ):
        result = paypal_provider.capture(paypal_payment)

    assert result is None


def test_release_posts_to_void_url(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    with (
        patch.object(
            paypal_provider,
            "_get_links",
            return_value={"void": {"href": "https://example.com/void"}},
        ),
        patch.object(paypal_provider, "post") as mocked_post,
    ):
        paypal_provider.release(paypal_payment)

    mocked_post.assert_called_once_with(
        paypal_payment, "https://example.com/void"
    )


def test_authorize_reraises_non_401_http_error(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    response = Mock(status_code=500)
    error = HTTPError(response=response)

    with (
        patch.object(paypal_provider, "get_access_token", return_value="token"),
        patch.object(paypal_provider, "get", side_effect=error),
        pytest.raises(HTTPError),
    ):
        paypal_provider.get(paypal_payment, "https://example.com")


def test_capture_handles_invalid_json_authorization_error(
    paypal_provider: PaypalProvider,
    paypal_payment: Payment,
) -> None:
    response = Mock()
    response.json.side_effect = ValueError("Invalid JSON")

    with (
        patch.object(
            paypal_provider,
            "_get_links",
            return_value={"capture": {"href": "https://example.com/capture"}},
        ),
        patch.object(
            paypal_provider,
            "post",
            side_effect=HTTPError(response=response),
        ),
        pytest.raises(HTTPError),
    ):
        paypal_provider.capture(paypal_payment)


def test_card_provider_product_data_includes_cvv(
    paypal_card_provider: PaypalCardProvider,
    paypal_card_payment: Payment,
) -> None:
    from datetime import date

    extra_data = {
        "expiration": date(2028, 12, 1),
        "number": "4111111111111111",
        "cvv2": "123",
    }

    with patch.object(
        paypal_card_provider,
        "get_transactions_data",
        return_value={},
    ):
        result = paypal_card_provider.get_product_data(
            paypal_card_payment, extra_data
        )

    card = result["payer"]["funding_instruments"][0]["credit_card"]
    assert card["cvv2"] == "123"
    assert card["expire_month"] == 12
    assert card["expire_year"] == 2028


def test_provider_get_form_uses_existing_approval_url(
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    paypal_payment.id = None
    links = {"approval_url": {"href": "https://example.com/approve"}}

    with (
        patch.object(paypal_payment, "save") as save,
        patch.object(paypal_provider, "_get_links", return_value=links),
        pytest.raises(RedirectNeeded, match="https://example.com/approve"),
    ):
        paypal_provider.get_form(paypal_payment)

    assert save.call_count >= 2
    assert paypal_payment.status == PaymentStatus.WAITING


def test_provider_get_form_creates_payment_without_approval_url(
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    links = [
        {},
        {"approval_url": {"href": "https://example.com/approve"}},
    ]

    with (
        patch.object(paypal_provider, "_get_links", side_effect=links),
        patch.object(
            paypal_provider,
            "create_payment",
            return_value={"id": "test-transaction"},
        ) as create_payment,
        pytest.raises(RedirectNeeded),
    ):
        paypal_provider.get_form(paypal_payment)

    create_payment.assert_called_once_with(paypal_payment)
    assert paypal_payment.transaction_id == "test-transaction"


def test_card_provider_get_form_does_not_change_non_waiting_status(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    paypal_card_payment.status = PaymentStatus.INPUT

    with (
        patch("payments.paypal.PaymentForm") as form_class,
        patch.object(paypal_card_payment, "change_status") as change_status,
    ):
        form_class.return_value.is_valid.return_value = False
        result = paypal_card_provider.get_form(paypal_card_payment)

    assert result is form_class.return_value
    change_status.assert_not_called()


def test_card_provider_product_data_without_cvv(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    from datetime import date

    extra_data = {
        "expiration": date(2028, 12, 1),
        "number": "4111111111111111",
    }

    with (
        patch.object(
            paypal_card_provider,
            "get_transactions_data",
            return_value={},
        ),
        patch("payments.paypal.get_credit_card_issuer", return_value=("visa", None)),
    ):
        result = paypal_card_provider.get_product_data(
            paypal_card_payment, extra_data
        )

    card = result["payer"]["funding_instruments"][0]["credit_card"]
    assert "cvv2" not in card


def test_card_provider_process_data_returns_forbidden(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    response = paypal_card_provider.process_data(paypal_card_payment, Mock())

    assert response.status_code == 403
    assert response.content == b"FAILED"


def test_form_shows_internal_error_for_non_400_http_error(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    response = Mock(status_code=500)

    with patch.object(
        paypal_card_provider,
        "create_payment",
        side_effect=HTTPError(response=response),
    ):
        form = paypal_card_provider.get_form(
            payment=paypal_card_payment,
            data=PROCESS_DATA,
        )

    assert paypal_card_payment.status == PaymentStatus.ERROR
    assert "Internal PayPal error" in form.errors["__all__"]


def test_provider_renews_token_when_cached_response_has_no_access_token(
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    paypal_payment.created = timezone.now()

    response401 = Mock(status_code=401)
    success = Mock(status_code=200)
    success.json.return_value = {"status": "success"}

    with (
        patch.object(
            paypal_provider,
            "get_last_response",
            side_effect=[
                {
                    "access_token": "cached_token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                {},
                {},
            ],
        ) as mocked_last_response,
        patch("requests.post") as mocked_post,
        patch(
            "requests.request",
            side_effect=[HTTPError(response=response401), success],
        ) as mocked_request,
    ):
        mocked_post.return_value.json.return_value = {
            "access_token": "renewed_token",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        result = paypal_provider.http_request(
            paypal_payment, "https://example.com/api"
        )

    assert result == {"status": "success"}
    assert mocked_request.call_count == 2
    assert mocked_post.call_count == 1
    assert mocked_last_response.call_count == 3



def test_provider_reraises_non_401_http_error_from_authorized_request(
    paypal_payment: Payment,
    paypal_provider: PaypalProvider,
) -> None:
    paypal_payment.extra_data = json.dumps(
        {"auth_response": {"access_token": "cached_token"}}
    )
    paypal_payment.created = timezone.now()

    response500 = Mock(status_code=500)

    with (
        patch.object(
            paypal_provider,
            "get_access_token",
            return_value="Bearer cached_token",
        ),
        patch(
            "requests.request",
            side_effect=HTTPError(response=response500),
        ),
        pytest.raises(HTTPError),
    ):
        paypal_provider.http_request(
            paypal_payment, "https://example.com/api"
        )


def test_form_does_not_create_payment_when_transaction_id_exists(
    paypal_card_payment: Payment,
    paypal_card_provider: PaypalCardProvider,
) -> None:
    paypal_card_payment.transaction_id = "existing-transaction"

    with (
        patch.object(
            paypal_card_provider,
            "create_payment",
        ) as mocked_create,
        pytest.raises(RedirectNeeded),
    ):
        paypal_card_provider.get_form(
            payment=paypal_card_payment,
            data=PROCESS_DATA,
        )

    mocked_create.assert_not_called()
    assert paypal_card_payment.transaction_id == "existing-transaction"
