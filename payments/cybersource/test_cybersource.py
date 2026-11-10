from __future__ import annotations

import types
from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from suds.sax.element import Element
from suds.sudsobject import Object
from django.core import signing
from django.utils.timezone import localdate

from payments import PaymentStatus
from payments import FraudStatus
from payments import PaymentError
from payments import PurchasedItem
from payments import RedirectNeeded

from . import ACCEPTED
from . import AUTHENTICATE_REQUIRED
from . import TRANSACTION_SETTLED
from . import TRANSACTION_REVERSED
from . import CyberSourceProvider

MERCHANT_ID = "abcd1234"
PASSWORD = "1234abdd1234abcd"
ORG_ID = "abc"

PROCESS_DATA = {
    "name": "John Doe",
    "number": "371449635398431",
    "expiration_0": "5",
    "expiration_1": localdate().year + 1,
    "cvv2": "1234",
    "fingerprint": "abcd1234",
}


class Payment(Mock):
    id = 1
    variant = "cybersource"
    currency = "USD"
    total = 100
    status = PaymentStatus.WAITING
    transaction_id = None
    captured_amount = 0
    message = ""

    attrs = types.SimpleNamespace(
        fingerprint_session_id="fake",
        merchant_defined_data={},
    )

    def get_process_url(self) -> str:
        return "http://example.com"

    def get_failure_url(self) -> str:
        return "http://cancel.com"

    def get_success_url(self) -> str:
        return "http://success.com"

    def change_status(self, status: str, message: str = "") -> None:
        self.status = status
        self.message = message

    def get_purchased_items(self) -> list[PurchasedItem]:
        return [
            PurchasedItem(
                name="foo",
                quantity=Decimal(10),
                price=Decimal(20),
                currency="USD",
                sku="bar",
            )
        ]


@pytest.fixture
@patch("payments.cybersource.suds.client.Client", new=MagicMock())
def provider() -> tuple[Payment, CyberSourceProvider]:
    payment = Payment()
    return payment, CyberSourceProvider(
        merchant_id=MERCHANT_ID, password=PASSWORD, org_id=ORG_ID
    )


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_raises_redirect_needed_on_success(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    transaction_id = 1234
    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = 100
    mocked_request.return_value = response
    with pytest.raises(RedirectNeeded):
        prov.get_form(payment=payment, data=PROCESS_DATA)
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total
    assert payment.transaction_id == transaction_id


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_returns_form_on_3d_secure(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    response = MagicMock()
    response.reasonCode = AUTHENTICATE_REQUIRED
    mocked_request.return_value = response
    form = prov.get_form(payment=payment, data=PROCESS_DATA)
    assert payment.status == PaymentStatus.WAITING
    assert "PaReq" in form.fields


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_shows_validation_error_message_response(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    error_message = "The card you are trying to use was reported as lost or stolen."
    error_code = 205
    response = MagicMock()
    response.reasonCode = error_code
    mocked_request.return_value = response
    form = prov.get_form(payment=payment, data=PROCESS_DATA)
    assert form.errors["__all__"][0] == error_message


def test_provider_shows_validation_error_message_duplicate(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.transaction_id = 1  # type: ignore[assignment]
    error_message = "This payment has already been processed."
    form = prov.get_form(payment=payment, data=PROCESS_DATA)
    assert form.errors["__all__"][0] == error_message


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_captures_payment(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    transaction_id = 1234
    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = TRANSACTION_SETTLED
    mocked_request.return_value = response
    prov.capture(payment)
    assert payment.status == PaymentStatus.CONFIRMED


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_refunds_payment(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.captured_amount = payment.total
    response = MagicMock()
    response.reasonCode = ACCEPTED
    mocked_request.return_value = response
    amount = prov.refund(payment)
    assert payment.total == amount


@patch.object(CyberSourceProvider, "_make_request")
def test_provider_releases_payment(
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    transaction_id = 123
    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = ACCEPTED
    mocked_request.return_value = response
    prov.release(payment)
    assert payment.transaction_id == transaction_id


@patch("payments.cybersource.redirect")
@patch.object(CyberSourceProvider, "_make_request")
def test_provider_redirects_on_success_captured_payment(
    mocked_request: MagicMock,
    mocked_redirect: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    transaction_id = 1234
    xid = "abc"
    payment.attrs.xid = xid

    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = ACCEPTED
    mocked_request.return_value = response

    request = MagicMock()
    request.POST = {"MD": xid}
    request.GET = {
        "token": signing.dumps(
            {
                "expiration": {"year": 2023, "month": 9},
                "name": "John Doe",
                "number": "371449635398431",
                "cvv2": "123",
            }
        )
    }
    prov.process_data(payment, request)
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total
    assert payment.transaction_id == transaction_id


@patch("payments.cybersource.redirect")
@patch.object(CyberSourceProvider, "_make_request")
@patch("payments.cybersource.suds.client.Client", new=MagicMock())
def test_provider_redirects_on_success_preauth_payment(
    mocked_request: MagicMock,
    mocked_redirect: MagicMock,
) -> None:
    payment = Payment()
    provider = CyberSourceProvider(
        merchant_id=MERCHANT_ID, password=PASSWORD, org_id=ORG_ID, capture=False
    )
    transaction_id = 1234
    xid = "abc"
    payment.attrs.xid = xid

    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = ACCEPTED
    mocked_request.return_value = response

    request = MagicMock()
    request.POST = {"MD": xid}
    request.GET = {
        "token": signing.dumps(
            {
                "expiration": {"year": 2023, "month": 9},
                "name": "John Doe",
                "number": "371449635398431",
                "cvv2": "123",
            }
        )
    }
    provider.process_data(payment, request)
    assert payment.status == PaymentStatus.PREAUTH
    assert payment.captured_amount == 0
    assert payment.transaction_id == transaction_id


@patch("payments.cybersource.redirect")
@patch.object(CyberSourceProvider, "_make_request")
@patch("payments.cybersource.suds.client.Client", new=MagicMock())
def test_provider_redirects_on_failure(
    mocked_request: MagicMock,
    mocked_redirect: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    transaction_id = 1234
    xid = "abc"
    payment.attrs.xid = xid

    response = MagicMock()
    response.requestID = transaction_id
    response.reasonCode = "test code"
    mocked_request.return_value = response

    request = MagicMock()
    request.POST = {"MD": xid}
    request.GET = {
        "token": signing.dumps(
            {
                "expiration": {"year": 2023, "month": 9},
                "name": "John Doe",
                "number": "371449635398431",
                "cvv2": "123",
            }
        )
    }
    prov.process_data(payment, request)
    assert payment.status == PaymentStatus.ERROR
    assert payment.captured_amount == 0
    assert payment.transaction_id == transaction_id


@pytest.mark.parametrize(
    ("reason_code", "fraud_status"),
    [
        (480, FraudStatus.REVIEW),
        (481, FraudStatus.REJECT),
        (400, FraudStatus.REJECT),
        (520, FraudStatus.REJECT),
        (230, FraudStatus.REJECT),
        (200, FraudStatus.REJECT),
    ],
)
def test_provider_handles_fraud_reason_codes(
    provider: tuple[Payment, CyberSourceProvider],
    reason_code: int,
    fraud_status: FraudStatus,
) -> None:
    payment, prov = provider

    prov._set_proper_payment_status_from_reason_code(payment, reason_code)

    payment.change_fraud_status.assert_called_once()
    assert payment.change_fraud_status.call_args.args[0] == fraud_status
    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.captured_amount == payment.total


def test_provider_raises_error_for_unknown_reason_code(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider

    with pytest.raises(PaymentError):
        prov._set_proper_payment_status_from_reason_code(payment, 999)

    assert payment.status == PaymentStatus.ERROR


def test_provider_raises_error_when_capture_fails(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    prov._prepare_capture = Mock(return_value={})
    prov._make_request = Mock(
        return_value=types.SimpleNamespace(reasonCode=999)
    )

    with pytest.raises(PaymentError):
        prov.capture(payment)

    payment.save.assert_called_once()


def test_provider_raises_error_when_release_fails(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    prov._prepare_release = Mock(return_value={})
    prov._make_request = Mock(
        return_value=types.SimpleNamespace(reasonCode=999)
    )

    with pytest.raises(PaymentError):
        prov.release(payment)

    payment.save.assert_called_once()


def test_provider_raises_error_when_refund_fails(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    prov._prepare_refund = Mock(return_value={})
    prov._make_request = Mock(
        return_value=types.SimpleNamespace(reasonCode=999)
    )

    with pytest.raises(PaymentError):
        prov.refund(payment)

    payment.save.assert_called_once()


@pytest.mark.parametrize(
    "reason_code",
    [
        221, 222, 700, 701, 702, 703,
        201, 203, 209,
        202,
        204, 210, 251,
        205, 208, 211, 231,
        232, 240,
        450, 451, 452, 453, 454, 455,
        456, 457, 458, 459, 460, 461,
        999,
    ],
)
def test_provider_returns_error_message_for_reason_codes(
    provider: tuple[Payment, CyberSourceProvider],
    reason_code: int,
) -> None:
    _, prov = provider

    message = prov._get_error_message(reason_code)

    assert isinstance(message, str)
    assert message


@pytest.mark.parametrize(
    ("card_type", "expected"),
    [
        ("visa", "001"),
        ("mastercard", "002"),
        ("amex", "003"),
        ("discover", "004"),
        ("diners", "005"),
        ("jcb", "007"),
        ("maestro", "042"),
        ("unknown", None),
    ],
)
def test_provider_maps_card_types(
    provider: tuple[Payment, CyberSourceProvider],
    card_type: str,
    expected: str | None,
) -> None:
    _, prov = provider

    with patch(
        "payments.cybersource.get_credit_card_issuer",
        return_value=(card_type, "Card"),
    ):
        assert prov._prepare_card_type("4111111111111111") == expected


def test_provider_prepares_totals_with_default_amount(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider

    totals = prov._prepare_totals(payment)

    assert totals.currency == payment.currency
    assert totals.grandTotalAmount == str(payment.total)
    assert totals.freightAmount == str(payment.delivery)


def test_provider_prepares_totals_with_custom_amount(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider

    totals = prov._prepare_totals(payment, amount=Decimal("25.50"))

    assert totals.currency == payment.currency
    assert totals.grandTotalAmount == "25.50"


def test_provider_prepares_merchant_defined_data(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.attrs.merchant_defined_data = {"1": "customer-type"}

    data = prov._prepare_merchant_defined_data(payment)

    assert data is not None
    prov.client.factory.create.assert_any_call("data:MerchantDefinedData")
    prov.client.factory.create.assert_any_call("data:MDDField")


def test_provider_returns_none_without_merchant_defined_data(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    del payment.attrs.merchant_defined_data

    assert prov._prepare_merchant_defined_data(payment) is None


def test_process_data_redirects_when_md_is_invalid(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.attrs.xid = "expected-xid"

    request = Mock()
    request.POST = {"MD": "wrong-xid"}

    response = prov.process_data(payment, request)

    assert response.status_code == 302
    assert response.url == payment.get_failure_url()


@pytest.mark.parametrize(
    "status",
    [PaymentStatus.CONFIRMED, PaymentStatus.PREAUTH],
)
def test_process_data_redirects_when_payment_is_already_complete(
    provider: tuple[Payment, CyberSourceProvider],
    status: str,
) -> None:
    payment, prov = provider
    payment.attrs.xid = "valid-xid"
    payment.status = status

    request = Mock()
    request.POST = {"MD": "valid-xid"}

    response = prov.process_data(payment, request)

    assert response.status_code == 302
    assert response.url == payment.get_success_url()


def test_process_data_redirects_when_token_is_missing(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.attrs.xid = "valid-xid"

    request = Mock()
    request.POST = {"MD": "valid-xid"}
    request.GET = {}

    response = prov.process_data(payment, request)

    assert response.status_code == 302
    assert response.url == payment.get_failure_url()


def test_process_data_redirects_when_token_signature_is_invalid(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.attrs.xid = "valid-xid"

    request = Mock()
    request.POST = {"MD": "valid-xid"}
    request.GET = {"token": "invalid-signature"}

    response = prov.process_data(payment, request)

    assert response.status_code == 302
    assert response.url == payment.get_failure_url()


def test_serialize_response_with_suds_object(provider):
    _, cybersource = provider

    response = Object()
    response.name = "Sakthi"
    response.amount = 100

    assert cybersource._serialize_response(response) == {
        "name": "Sakthi",
        "amount": 100,
    }


def test_serialize_response_with_nested_suds_objects(provider):
    _, cybersource = provider

    nested = Object()
    nested.amount = 100

    response = Object()
    response.transaction = nested
    response.status = "ACCEPTED"

    assert cybersource._serialize_response(response) == {
        "transaction": {"amount": 100},
        "status": "ACCEPTED",
    }


def test_serialize_response_with_element(provider):
    _, cybersource = provider

    response = Element("root")

    assert cybersource._serialize_response(response) == {}


def test_serialize_response_with_regular_value(provider):
    _, cybersource = provider

    assert cybersource._serialize_response("ACCEPTED") == "ACCEPTED"
    assert cybersource._serialize_response(100) == 100
    assert cybersource._serialize_response(None) is None


def test_prepare_preauth(provider):
    payment, cybersource = provider
    card_data = {
        "name": "John Doe",
        "number": "371449635398431",
        "expiration": localdate().replace(year=localdate().year + 1),
        "cvv2": "1234",
    }

    params = cybersource._prepare_preauth(payment, card_data)

    assert params["ccAuthService"]._run == "true"
    assert params["payerAuthEnrollService"]._run == "true"
    assert "billTo" in params
    assert "card" in params
    assert "item" in params
    assert "purchaseTotals" in params


def test_make_request(provider):
    payment, cybersource = provider
    response = Object()
    response.requestID = "request-123"

    cybersource.client.service.runTransaction.return_value = response

    result = cybersource._make_request(payment, {"merchantID": MERCHANT_ID})

    assert result is response
    cybersource.client.service.runTransaction.assert_called_once_with(
        merchantID=MERCHANT_ID
    )
    assert payment.attrs.last_response == {"requestID": "request-123"}


@pytest.mark.parametrize("capture", [True, False])
def test_prepare_payer_auth_validation_check(provider, capture):
    payment, cybersource = provider
    payment.attrs = types.SimpleNamespace(
        fingerprint_session_id="fake",
        merchant_defined_data={},
        capture=capture,
    )

    card_data = {
        "name": "John Doe",
        "number": "371449635398431",
        "expiration": localdate().replace(year=localdate().year + 1),
        "cvv2": "1234",
    }

    params = cybersource._prepare_payer_auth_validation_check(
        payment, card_data, "signed-response"
    )

    assert params["payerAuthValidateService"]._run == "true"
    assert params["payerAuthValidateService"].signedPARes == "signed-response"

    if capture:
        assert params["ccCreditService"]._run == "true"
        assert "ccAuthService" not in params
    else:
        assert params["ccAuthService"]._run == "true"
        assert "ccCreditService" not in params

    assert "billTo" in params
    assert "card" in params
    assert "item" in params
    assert "purchaseTotals" in params


def test_charge_uses_preauthorization_when_capture_disabled(provider):
    payment, cybersource = provider
    cybersource._capture = False

    response = Object()
    response.requestID = "preauth-123"
    response.reasonCode = ACCEPTED

    cybersource._prepare_preauth = Mock(return_value={"test": "params"})
    cybersource._make_request = Mock(return_value=response)
    cybersource._set_proper_payment_status_from_reason_code = Mock()

    card_data = {"number": "4111111111111111"}

    cybersource.charge(payment, card_data)

    cybersource._prepare_preauth.assert_called_once_with(payment, card_data)
    cybersource._make_request.assert_called_once_with(
        payment, {"test": "params"}
    )
    assert payment.transaction_id == "preauth-123"
    assert payment.attrs.capture is False
    cybersource._set_proper_payment_status_from_reason_code.assert_called_once_with(
        payment, ACCEPTED
    )


def test_capture_accepted(provider):
    payment, cybersource = provider
    response = Object()
    response.reasonCode = ACCEPTED
    response.requestID = "capture-123"

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_capture = Mock(return_value={"test": "params"})

    result = cybersource.capture(payment)

    assert result == payment.total
    assert payment.transaction_id == "capture-123"


def test_capture_transaction_settled(provider):
    payment, cybersource = provider
    response = Object()
    response.reasonCode = TRANSACTION_SETTLED
    response.requestID = "capture-456"

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_capture = Mock(return_value={})

    result = cybersource.capture(payment)

    assert result == payment.total
    assert payment.status == PaymentStatus.CONFIRMED


def test_release_transaction_reversed(provider):
    payment, cybersource = provider
    response = Object()
    response.reasonCode = TRANSACTION_REVERSED

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_release = Mock(return_value={})

    cybersource.release(payment)


def test_release_accepted(provider):
    payment, cybersource = provider
    response = Object()
    response.reasonCode = ACCEPTED
    response.requestID = "release-123"

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_release = Mock(return_value={})

    cybersource.release(payment)

    assert payment.transaction_id == "release-123"


def test_refund_accepted(provider):
    payment, cybersource = provider
    payment.captured_amount = 75
    response = Object()
    response.reasonCode = ACCEPTED

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_refund = Mock(return_value={})

    result = cybersource.refund(payment)

    assert result == 75
    payment.save.assert_called_once()


def test_refund_failed(provider):
    payment, cybersource = provider
    response = Object()
    response.reasonCode = 999

    cybersource._make_request = Mock(return_value=response)
    cybersource._prepare_refund = Mock(return_value={})

    with pytest.raises(PaymentError):
        cybersource.refund(payment)

    payment.save.assert_called_once()


def test_new_payment_without_fingerprint(provider):
    payment, cybersource = provider
    payment.attrs = types.SimpleNamespace(merchant_defined_data={})

    params = cybersource._get_params_for_new_payment(payment)

    assert params["merchantID"] == MERCHANT_ID
    assert params["merchantReferenceCode"] == payment.id
    assert "deviceFingerprintID" not in params


def test_fingerprint_widget_renders_session_id() -> None:
    from payments.cybersource.forms import FingerprintWidget

    widget = FingerprintWidget(attrs={"class": "fingerprint"})

    with patch(
        "payments.cybersource.forms.render_to_string",
        return_value="<input>",
    ) as mocked_render:
        result = widget.render(
            "fingerprint",
            "session-123",
            attrs={"data-test": "yes"},
        )

    assert result == "<input>"
    mocked_render.assert_called_once()
    template, attrs = mocked_render.call_args.args
    assert template == "payments/cybersource_fingerprint.html"
    assert attrs["session_id"] == "session-123"
    assert attrs["name"] == "fingerprint"
    assert attrs["type"] == "hidden"
    assert attrs["class"] == "fingerprint"
    assert attrs["data-test"] == "yes"


def test_payment_form_generates_fingerprint_session_id(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider

    class MissingFingerprintAttrs:
        def __getattr__(self, name):
            raise KeyError(name)

    payment.attrs = MissingFingerprintAttrs()

    with patch(
        "payments.cybersource.forms.uuid4",
        return_value="generated-session",
    ):
        form = PaymentForm(
            data=PROCESS_DATA,
            provider=cybersource,
            payment=payment,
        )

    assert form.fields["fingerprint"].initial == "generated-session"


def test_payment_form_clean_charges_and_saves_fingerprint(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider
    cybersource.charge = Mock()

    form = PaymentForm(
        data=PROCESS_DATA,
        provider=cybersource,
        payment=payment,
    )

    assert form.is_valid()
    assert payment.attrs.fingerprint_session_id == PROCESS_DATA["fingerprint"]
    cybersource.charge.assert_called_once_with(payment, form.cleaned_data)


def test_payment_form_clean_handles_payment_error(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider
    cybersource.charge = Mock(side_effect=PaymentError("Charge failed"))

    form = PaymentForm(
        data=PROCESS_DATA,
        provider=cybersource,
        payment=payment,
    )

    assert not form.is_valid()
    assert "Charge failed" in str(form.errors)


def test_payment_form_rejects_already_processed_payment(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider
    payment.transaction_id = "existing-transaction"
    cybersource.charge = Mock()

    form = PaymentForm(
        data=PROCESS_DATA,
        provider=cybersource,
        payment=payment,
    )

    assert not form.is_valid()
    assert "already been processed" in str(form.errors)
    cybersource.charge.assert_not_called()


def test_payment_form_without_org_id_has_no_fingerprint_field(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider
    cybersource.org_id = None
    cybersource.charge = Mock()

    form = PaymentForm(
        data=PROCESS_DATA,
        provider=cybersource,
        payment=payment,
    )

    assert "fingerprint" not in form.fields
    assert form.is_valid()
    cybersource.charge.assert_called_once()


def test_payment_form_does_not_charge_when_validation_fails(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    from payments.cybersource.forms import PaymentForm

    payment, cybersource = provider
    cybersource.charge = Mock()

    invalid_data = {
        **PROCESS_DATA,
        "number": "invalid-card-number",
    }

    form = PaymentForm(
        data=invalid_data,
        provider=cybersource,
        payment=payment,
    )

    assert not form.is_valid()
    cybersource.charge.assert_not_called()


def test_provider_uses_production_endpoint_and_windows_path() -> None:
    client_mock = MagicMock()

    with (
        patch("payments.cybersource.os.path.sep", "\\"),
        patch(
            "payments.cybersource.os.path.dirname",
            return_value="C:\\project\\payments\\cybersource",
        ),
        patch("payments.cybersource.suds.client.Client", new=client_mock),
    ):
        prov = CyberSourceProvider(
            merchant_id=MERCHANT_ID,
            password=PASSWORD,
            org_id=ORG_ID,
            sandbox=False,
        )

    wsdl_path = client_mock.call_args.args[0]
    assert wsdl_path.startswith("file:///C:/project/payments/cybersource/")
    assert "\\" not in wsdl_path
    assert prov.endpoint == (
        "https://ics2ws.ic3.com/commerce/1.x/transactionProcessor"
    )


def test_provider_get_form_does_not_change_non_waiting_status(
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    payment.status = PaymentStatus.CONFIRMED

    prov.get_form(payment=payment, data={})

    assert payment.status == PaymentStatus.CONFIRMED


@patch.object(CyberSourceProvider, "_make_request")
@patch.object(CyberSourceProvider, "_prepare_capture", return_value={})
def test_provider_captures_explicit_amount(
    mocked_prepare: MagicMock,
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    response = MagicMock()
    response.reasonCode = ACCEPTED
    response.requestID = 1234
    mocked_request.return_value = response
    amount = Decimal("25.50")

    result = prov.capture(payment, amount=amount)

    assert result == amount
    mocked_prepare.assert_called_once_with(payment, amount=amount)
    assert payment.transaction_id == 1234


@patch.object(CyberSourceProvider, "_make_request")
@patch.object(CyberSourceProvider, "_prepare_refund", return_value={})
def test_provider_refunds_explicit_amount(
    mocked_prepare: MagicMock,
    mocked_request: MagicMock,
    provider: tuple[Payment, CyberSourceProvider],
) -> None:
    payment, prov = provider
    response = MagicMock()
    response.reasonCode = ACCEPTED
    mocked_request.return_value = response
    amount = Decimal("15.25")

    result = prov.refund(payment, amount=amount)

    assert result == amount
    mocked_prepare.assert_called_once_with(payment, amount=amount)
    payment.save.assert_called_once()
