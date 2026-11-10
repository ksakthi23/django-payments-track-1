from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import NonCallableMock
from unittest.mock import patch

import pytest
from django.utils.timezone import localdate

from payments import core

from . import PaymentStatus
from .forms import CreditCardPaymentFormWithName
from .forms import PaymentForm
from .models import BasePayment


@patch("payments.core.PAYMENT_HOST", new_callable=NonCallableMock)
def test_text_get_base_url(host: NonCallableMock) -> None:
    host.__str__ = lambda x: "example.com/string"
    assert core.get_base_url() == "https://example.com/string"


@patch("payments.core.PAYMENT_HOST")
def test_callable_get_base_url(host: MagicMock) -> None:
    host.return_value = "example.com/callable"
    assert core.get_base_url() == "https://example.com/callable"


def test_provider_factory() -> None:
    core.provider_factory("default")


def test_provider_does_not_exist() -> None:
    with pytest.raises(
        ValueError,
        match="Payment variant does not exist: fake_provider",
    ):
        core.provider_factory("fake_provider")


class Payment(BasePayment):
    """
    Concrete model class for testing.
    Instantiating an abstract model is deprecated in newer versions of Django.
    """

@pytest.mark.django_db
def test_change_status_persists_additional_fields() -> None:
    payment = Payment(
        variant="authorizenet",
        currency="USD",
        total=Decimal("100.00"),
    )
    payment.save()

    payment.transaction_id = "AUTH-12345"
    payment.captured_amount = Decimal("100.00")

    payment.change_status(
        PaymentStatus.CONFIRMED,
        update_fields=["transaction_id", "captured_amount"],
    )

    payment.refresh_from_db()

    assert payment.status == PaymentStatus.CONFIRMED
    assert payment.transaction_id == "AUTH-12345"
    assert payment.captured_amount == Decimal("100.00")

    payment.captured_amount = Decimal("0.00")
    payment.change_status(
        PaymentStatus.ERROR,
        update_fields=["captured_amount"],
    )
    payment.refresh_from_db()

    assert payment.status == PaymentStatus.ERROR
    assert payment.captured_amount == Decimal("0.00")


def test_payment_attributes() -> None:
    payment = Payment(extra_data='{"attr1": "test1", "attr2": "test2"}')
    assert payment.attrs.attr1 == "test1"
    assert payment.attrs.attr2 == "test2"
    assert getattr(payment.attrs, "attr5", None) is None
    assert not hasattr(payment.attrs, "attr7")


def test_capture_with_wrong_status() -> None:
    payment = Payment(variant="default", status=PaymentStatus.WAITING)
    with pytest.raises(
        ValueError,
        match="Only pre-authorized payments can be captured\\.",
    ):
        payment.capture()


@patch("payments.dummy.DummyProvider.capture")
def test_capture_preauth_successfully(mocked_capture_method: MagicMock) -> None:
    amount = Decimal(20)
    with patch.object(BasePayment, "save"):
        mocked_capture_method.return_value = amount
        payment = Payment(variant="default", status=PaymentStatus.PREAUTH)
        payment.capture(amount)

        assert payment.status == PaymentStatus.CONFIRMED
        assert payment.captured_amount == amount
    assert mocked_capture_method.call_count == 1


@patch("payments.dummy.DummyProvider.capture")
def test_capture_preauth_without_amount(mocked_capture_method: MagicMock) -> None:
    with patch.object(BasePayment, "save"):
        mocked_capture_method.return_value = None
        captured_amount = Decimal(100)
        payment = Payment(
            variant="default",
            status=PaymentStatus.PREAUTH,
            captured_amount=captured_amount,
        )
        payment.capture(None)

        assert payment.status == PaymentStatus.PREAUTH
        assert payment.captured_amount == captured_amount
    assert mocked_capture_method.call_count == 1


def test_release_with_wrong_status() -> None:
    payment = Payment(variant="default", status=PaymentStatus.WAITING)
    with pytest.raises(
        ValueError,
        match="Only pre-authorized payments can be released\\.",
    ):
        payment.release()


@patch("payments.dummy.DummyProvider.release")
def test_release_preauth_successfully(mocked_release_method: MagicMock) -> None:
    with patch.object(BasePayment, "save"):
        payment = Payment(variant="default", status=PaymentStatus.PREAUTH)
        payment.release()
        assert payment.status == PaymentStatus.REFUNDED
    assert mocked_release_method.call_count == 1


def test_refund_with_wrong_status() -> None:
    payment = Payment(variant="default", status=PaymentStatus.WAITING)
    with pytest.raises(ValueError, match="Only charged payments can be refunded\\."):
        payment.refund()


def test_refund_too_high_amount() -> None:
    payment = Payment(
        variant="default",
        status=PaymentStatus.CONFIRMED,
        captured_amount=Decimal(100),
    )
    with pytest.raises(
        ValueError,
        match="Refund amount can not be greater then captured amount",
    ):
        payment.refund(Decimal(200))


@patch("payments.dummy.DummyProvider.refund")
def test_refund_without_amount(mocked_refund_method: MagicMock) -> None:
    captured_amount = Decimal(200)
    with patch.object(BasePayment, "save"):
        mocked_refund_method.return_value = captured_amount
        payment = Payment(
            variant="default",
            status=PaymentStatus.CONFIRMED,
            captured_amount=captured_amount,
        )
        payment.refund()

        assert payment.status == PaymentStatus.REFUNDED
        assert payment.captured_amount == Decimal(0)
    assert mocked_refund_method.call_count == 1


@patch("payments.dummy.DummyProvider.refund")
def test_refund_partial_success(mocked_refund_method: MagicMock) -> None:
    refund_amount = Decimal(100)
    captured_amount = Decimal(200)
    with patch.object(BasePayment, "save"):
        mocked_refund_method.return_value = refund_amount
        payment = Payment(
            variant="default",
            status=PaymentStatus.CONFIRMED,
            captured_amount=captured_amount,
        )
        payment.refund(refund_amount)

        assert payment.status == PaymentStatus.CONFIRMED
        assert payment.captured_amount == Decimal(100)
    assert mocked_refund_method.call_count == 1


@patch("payments.dummy.DummyProvider.refund")
def test_refund_fully_success(mocked_refund_method: MagicMock) -> None:
    refund_amount = Decimal(200)
    captured_amount = Decimal(200)
    with patch.object(BasePayment, "save"):
        mocked_refund_method.return_value = refund_amount
        payment = Payment(
            variant="default",
            status=PaymentStatus.CONFIRMED,
            captured_amount=captured_amount,
        )
        payment.refund(refund_amount)

        assert payment.status == PaymentStatus.REFUNDED
        assert payment.captured_amount == Decimal(0)
    assert mocked_refund_method.call_count == 1


@pytest.fixture
def credit_card_data() -> dict[str, str | int]:
    return {
        "name": "John Doe",
        "number": "4716124728800975",
        "expiration_0": "5",
        "expiration_1": localdate().year + 1,
        "cvv2": "123",
    }


def test_form_verifies_card_number(credit_card_data: dict[str, str | int]) -> None:
    form = CreditCardPaymentFormWithName(data=credit_card_data)
    assert form.is_valid()


def test_form_raises_error_for_invalid_card_number(
    credit_card_data: dict[str, str | int],
) -> None:
    data = dict(credit_card_data, number="1112223334445556")
    form = CreditCardPaymentFormWithName(data=data)
    assert not form.is_valid()
    assert "number" in form.errors


def test_form_raises_error_for_invalid_cvv2(
    credit_card_data: dict[str, str | int],
) -> None:
    data = dict(credit_card_data, cvv2="12345")
    form = CreditCardPaymentFormWithName(data=data)
    assert not form.is_valid()
    assert "cvv2" in form.errors


def test_form_contains_hidden_fields() -> None:
    data = {
        "field1": "value1",
        "field2": "value2",
        "field3": "value3",
        "field4": "value4",
    }
    form = PaymentForm(data=data, hidden_inputs=True)
    assert len(form.fields) == len(data)
    assert form.fields["field1"].initial == "value1"


def test_mastercard() -> None:
    assert core.get_credit_card_issuer("2720999018275485") == (
        "mastercard",
        "MasterCard",
    )
    assert core.get_credit_card_issuer("5101395940513451") == (
        "mastercard",
        "MasterCard",
    )
    assert core.get_credit_card_issuer("5469166706524768") == (
        "mastercard",
        "MasterCard",
    )


def test_visa() -> None:
    assert core.get_credit_card_issuer("4929299255922609") == ("visa", "VISA")
    assert core.get_credit_card_issuer("4539883983691685") == ("visa", "VISA")
    assert core.get_credit_card_issuer("4916396455393611281") == ("visa", "VISA")


def test_discover() -> None:
    assert core.get_credit_card_issuer("6011281400356614") == ("discover", "Discover")
    assert core.get_credit_card_issuer("6011223438090674") == ("discover", "Discover")
    assert core.get_credit_card_issuer("6011509478386387430") == (
        "discover",
        "Discover",
    )


def test_amex() -> None:
    assert core.get_credit_card_issuer("341841172626538") == (
        "amex",
        "American Express",
    )
    assert core.get_credit_card_issuer("348710065929999") == (
        "amex",
        "American Express",
    )
    assert core.get_credit_card_issuer("341473920579841") == (
        "amex",
        "American Express",
    )


def test_cancel_with_wrong_status_confirmed():
    payment = Payment(variant="default", status=PaymentStatus.CONFIRMED)
    with pytest.raises(
        ValueError,
        match="Only waiting or input payments can be cancelled\\.",
    ):
        payment.cancel()


def test_cancel_with_wrong_status_preauth():
    payment = Payment(variant="default", status=PaymentStatus.PREAUTH)
    with pytest.raises(
        ValueError,
        match="Only waiting or input payments can be cancelled\\.",
    ):
        payment.cancel()


def test_cancel_with_wrong_status_refunded():
    payment = Payment(variant="default", status=PaymentStatus.REFUNDED)
    with pytest.raises(
        ValueError,
        match="Only waiting or input payments can be cancelled\\.",
    ):
        payment.cancel()


@patch("payments.dummy.DummyProvider.cancel")
def test_cancel_waiting_payment_successfully(mocked_cancel_method):
    with patch.object(BasePayment, "save"):
        payment = Payment(variant="default", status=PaymentStatus.WAITING)
        payment.cancel()
        assert payment.status == PaymentStatus.CANCELLED
    assert mocked_cancel_method.call_count == 1


@patch("payments.dummy.DummyProvider.cancel")
def test_cancel_input_payment_successfully(mocked_cancel_method):
    with patch.object(BasePayment, "save"):
        payment = Payment(variant="default", status=PaymentStatus.INPUT)
        payment.cancel()
        assert payment.status == PaymentStatus.CANCELLED
    assert mocked_cancel_method.call_count == 1


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("4111111111111111", ("visa", "VISA")),
        ("6011111111111117", ("discover", "Discover")),
        ("371449635398431", ("amex", "American Express")),
        ("213100000000000", ("jcb", "JCB")),
        ("30569309025904", ("diners", "Diners Club")),
        ("6759649826438453", ("maestro", "Maestro")),
        ("not-a-card", (None, None)),
    ],
)
def test_credit_card_issuer(number, expected):
    assert core.get_credit_card_issuer(number) == expected




def test_provider_factory_caches_instance() -> None:
    core.PROVIDER_CACHE.pop("cache_test", None)

    with (
        patch(
            "payments.core.settings.PAYMENT_VARIANTS",
            {
                "cache_test": (
                    "payments.dummy.DummyProvider",
                    {},
                )
            },
            create=True,
        ),
        patch(
            "payments.core.import_string",
            return_value=MagicMock,
        ) as mocked_import,
    ):
        first = core._default_provider_factory("cache_test")
        second = core._default_provider_factory("cache_test")

    assert first is second
    mocked_import.assert_called_once_with(
        "payments.dummy.DummyProvider"
    )

    core.PROVIDER_CACHE.pop("cache_test", None)


def test_provider_factory_uses_configured_arguments() -> None:
    core.PROVIDER_CACHE.pop("config_test", None)
    mock_provider_class = MagicMock()

    with (
        patch(
            "payments.core.settings.PAYMENT_VARIANTS",
            {
                "config_test": (
                    "payments.dummy.DummyProvider",
                    {"capture": False},
                )
            },
            create=True,
        ),
        patch(
            "payments.core.import_string",
            return_value=mock_provider_class,
        ),
    ):
        core._default_provider_factory("config_test")

    mock_provider_class.assert_called_once_with(capture=False)

    core.PROVIDER_CACHE.pop("config_test", None)


def test_basic_provider_get_action() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()

    with patch.object(
        provider,
        "get_return_url",
        return_value="https://example.com/payment/return",
    ) as mocked_get_return_url:
        result = provider.get_action(payment)

    assert result == "https://example.com/payment/return"
    mocked_get_return_url.assert_called_once_with(payment)


def test_payment_attributes_set_value_with_empty_extra_data() -> None:
    payment = Payment(extra_data="")
    payment.attrs.new_attribute = "test-value"

    assert payment.attrs.new_attribute == "test-value"


def test_payment_attributes_missing_attribute_raises_attribute_error() -> None:
    payment = Payment(extra_data='{"existing": "value"}')

    with pytest.raises(AttributeError, match="missing"):
        _ = payment.attrs.missing


def test_change_fraud_status_successfully() -> None:
    payment = Payment(fraud_status="unknown")

    with patch.object(Payment, "save") as mocked_save:
        payment.change_fraud_status("accept", "Fraud check passed")

    assert payment.fraud_status == "accept"
    assert payment.fraud_message == "Fraud check passed"
    mocked_save.assert_called_once_with()


def test_change_fraud_status_without_commit() -> None:
    payment = Payment(fraud_status="unknown")

    with patch.object(Payment, "save") as mocked_save:
        payment.change_fraud_status("review", "Manual review required", commit=False)

    assert payment.fraud_status == "review"
    assert payment.fraud_message == "Manual review required"
    mocked_save.assert_not_called()


def test_change_fraud_status_rejects_invalid_status() -> None:
    payment = Payment(fraud_status="unknown")

    with pytest.raises(ValueError, match="Wrong status"):
        payment.change_fraud_status("invalid")


def test_get_purchased_items_returns_empty_list() -> None:
    payment = Payment()
    assert payment.get_purchased_items() == []


def test_get_failure_url_requires_subclass_implementation() -> None:
    payment = Payment()

    with pytest.raises(NotImplementedError):
        payment.get_failure_url()


def test_get_success_url_requires_subclass_implementation() -> None:
    payment = Payment()

    with pytest.raises(NotImplementedError):
        payment.get_success_url()


def test_get_process_url() -> None:
    payment = Payment(token="12345678-1234-5678-1234-567812345678")

    assert payment.get_process_url() == (
        "/payments/process/12345678-1234-5678-1234-567812345678/"
    )


def test_get_base_url_uses_request_host_when_site_not_found(monkeypatch) -> None:

    site_module = MagicMock()
    site_module.DoesNotExist = type("DoesNotExist", (Exception,), {})
    site_module.objects.get_current.side_effect = site_module.DoesNotExist

    monkeypatch.setattr(core, "PAYMENT_HOST", None)
    monkeypatch.setattr(core, "Site", site_module, raising=False)

    request = MagicMock()
    request.get_host.return_value = "fallback.example.com"

    protocol = "https" if core.PAYMENT_USES_SSL else "http"

    assert core.get_base_url(request) == (
        f"{protocol}://fallback.example.com"
    )
    site_module.objects.get_current.assert_called_once_with(request)


def test_get_base_url_raises_when_site_and_request_are_missing(monkeypatch) -> None:
    site_module = MagicMock()
    site_module.DoesNotExist = type("DoesNotExist", (Exception,), {})
    site_module.objects.get_current.side_effect = site_module.DoesNotExist

    monkeypatch.setattr(core, "PAYMENT_HOST", None)
    monkeypatch.setattr(core, "Site", site_module, raising=False)

    with pytest.raises(site_module.DoesNotExist):
        core.get_base_url()


def test_basic_provider_get_hidden_fields_requires_subclass_implementation() -> None:
    provider = core.BasicProvider()

    with pytest.raises(NotImplementedError):
        provider.get_hidden_fields(MagicMock())


def test_basic_provider_process_data_requires_subclass_implementation() -> None:
    provider = core.BasicProvider()

    with pytest.raises(NotImplementedError):
        provider.process_data(MagicMock(), MagicMock())


def test_basic_provider_get_token_requires_subclass() -> None:
    provider = core.BasicProvider()

    with pytest.raises(NotImplementedError):
        provider.get_token_from_request(MagicMock(), MagicMock())


def test_basic_provider_capture_requires_subclass_implementation() -> None:
    with pytest.raises(NotImplementedError):
        core.BasicProvider().capture(MagicMock())


def test_basic_provider_release_requires_subclass_implementation() -> None:
    with pytest.raises(NotImplementedError):
        core.BasicProvider().release(MagicMock())


def test_basic_provider_refund_requires_subclass_implementation() -> None:
    with pytest.raises(NotImplementedError):
        core.BasicProvider().refund(MagicMock())


def test_basic_provider_cancel_requires_subclass_implementation() -> None:
    with pytest.raises(NotImplementedError):
        core.BasicProvider().cancel(MagicMock())


def test_basic_provider_get_return_url_without_extra_data(monkeypatch) -> None:
    provider = core.BasicProvider()
    payment = MagicMock()
    payment.get_process_url.return_value = "/payments/process/test/"

    monkeypatch.setattr(core, "get_base_url", lambda request=None: "https://example.com")

    assert provider.get_return_url(payment) == (
        "https://example.com/payments/process/test/"
    )


def test_basic_provider_get_return_url_with_extra_data(monkeypatch) -> None:
    provider = core.BasicProvider()
    payment = MagicMock()
    payment.get_process_url.return_value = "/payments/process/test/"

    monkeypatch.setattr(core, "get_base_url", lambda request=None: "https://example.com")

    assert provider.get_return_url(
        payment, extra_data={"status": "success", "id": "123"}
    ) == "https://example.com/payments/process/test/?status=success&id=123"


def test_basic_provider_get_form() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()

    provider.get_hidden_fields = MagicMock(
        return_value={"merchant_id": "test123"}
    )
    provider.get_action = MagicMock(
        return_value="https://example.com/return/"
    )

    form = provider.get_form(payment)

    assert isinstance(form, PaymentForm)
    assert "merchant_id" in form.fields
    assert form.fields["merchant_id"].initial == "test123"
    assert form.fields["merchant_id"].widget.is_hidden
    assert form.action == "https://example.com/return/"
    assert form.method == "post"


def test_basic_provider_get_form_with_custom_data() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()

    provider.get_hidden_fields = MagicMock(
        return_value={"reference": "ABC123"}
    )
    provider.get_action = MagicMock(return_value="/return/")

    form = provider.get_form(payment)

    assert isinstance(form, PaymentForm)
    assert "reference" in form.fields
    assert form.fields["reference"].initial == "ABC123"
    assert form.action == "/return/"
    assert form.method == "post"


def test_basic_provider_get_action_passes_payment() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()

    provider.get_return_url = MagicMock(
        return_value="https://example.com/return/"
    )

    result = provider.get_action(payment)

    assert result == "https://example.com/return/"
    provider.get_return_url.assert_called_once_with(payment)


def test_default_provider_factory_raises_for_unknown_variant(monkeypatch) -> None:
    monkeypatch.setattr(
        core.settings,
        "PAYMENT_VARIANTS",
        {},
        raising=False,
    )

    with pytest.raises(ValueError, match="Payment variant does not exist"):
        core._default_provider_factory("unknown")


def test_default_provider_factory_creates_and_caches_provider(monkeypatch) -> None:
    class FakeProvider:
        def __init__(self, mode=None):
            self.mode = mode

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_VARIANTS",
        {"test": ("fake.Provider", {"mode": "test"})},
        raising=False,
    )
    monkeypatch.setattr(core, "PROVIDER_CACHE", {})
    monkeypatch.setattr(core, "import_string", lambda path: FakeProvider)

    first = core._default_provider_factory("test")
    second = core._default_provider_factory("test")

    assert isinstance(first, FakeProvider)
    assert first.mode == "test"
    assert first is second


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("4111111111111111", ("visa", "VISA")),
        ("6011111111111117", ("discover", "Discover")),
        ("378282246310005", ("amex", "American Express")),
        ("3530111333300000", ("jcb", "JCB")),
        ("30569309025904", ("diners", "Diners Club")),
        ("6799990100000000019", ("maestro", "Maestro")),
        ("1234567890123456", (None, None)),
    ],
)
def test_get_credit_card_issuer(number, expected) -> None:
    assert core.get_credit_card_issuer(number) == expected


def test_get_base_url_with_fixed_payment_host(monkeypatch) -> None:
    monkeypatch.setattr(core, "PAYMENT_HOST", "payments.example.com")

    protocol = "https" if core.PAYMENT_USES_SSL else "http"

    assert core.get_base_url() == f"{protocol}://payments.example.com"


def test_get_base_url_with_callable_payment_host(monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "PAYMENT_HOST",
        lambda: "callable.example.com",
    )

    protocol = "https" if core.PAYMENT_USES_SSL else "http"

    assert core.get_base_url() == f"{protocol}://callable.example.com"


def test_default_provider_factory_accepts_payment_argument(monkeypatch) -> None:
    class FakeProvider:
        def __init__(self, mode=None):
            self.mode = mode

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_VARIANTS",
        {"payment_test": ("fake.Provider", {"mode": "payment"})},
        raising=False,
    )
    monkeypatch.setattr(core, "PROVIDER_CACHE", {})
    monkeypatch.setattr(core, "import_string", lambda path: FakeProvider)

    payment = MagicMock()
    provider = core._default_provider_factory("payment_test", payment=payment)

    assert isinstance(provider, FakeProvider)
    assert provider.mode == "payment"


def test_basic_provider_get_return_url_passes_request(monkeypatch) -> None:
    provider = core.BasicProvider()
    payment = MagicMock()
    payment.get_process_url.return_value = "/payments/process/test/"
    request = MagicMock()

    get_base_url = MagicMock(return_value="https://request.example.com")
    monkeypatch.setattr(core, "get_base_url", get_base_url)

    result = provider.get_return_url(payment, request=request)

    assert result == (
        "https://request.example.com/payments/process/test/"
    )
    get_base_url.assert_called_once_with(request)


def test_basic_provider_capture_disabled() -> None:
    provider = core.BasicProvider(capture=False)

    assert provider._capture is False


def test_basic_provider_get_action_returns_return_url() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()
    payment.get_process_url.return_value = "/process/123/"

    with patch.object(
        provider,
        "get_return_url",
        return_value="https://example.com/process/123/",
    ) as mock_return_url:
        result = provider.get_action(payment)

    assert result == "https://example.com/process/123/"
    mock_return_url.assert_called_once_with(payment)


def test_get_base_url_uses_http_when_ssl_disabled(monkeypatch) -> None:
    monkeypatch.setattr(core, "PAYMENT_USES_SSL", False)
    monkeypatch.setattr(core, "PAYMENT_HOST", "payments.example.com")

    assert core.get_base_url() == "http://payments.example.com"


def test_basic_provider_get_return_url_encodes_extra_data() -> None:
    provider = core.BasicProvider()
    payment = MagicMock()
    payment.get_process_url.return_value = "/process/123/"

    with patch.object(
        core,
        "get_base_url",
        return_value="https://example.com",
    ):
        result = provider.get_return_url(
            payment,
            extra_data={"reference": "ABC 123", "status": "paid&done"},
        )

    assert result == (
        "https://example.com/process/123/"
        "?reference=ABC+123&status=paid%26done"
    )


def test_default_provider_factory_uses_default_variants(monkeypatch) -> None:
    class FakeProvider:
        pass

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_VARIANTS",
        core.PAYMENT_VARIANTS,
        raising=False,
    )
    monkeypatch.setattr(core, "PROVIDER_CACHE", {})
    monkeypatch.setattr(core, "import_string", lambda path: FakeProvider)

    provider = core._default_provider_factory("default")

    assert isinstance(provider, FakeProvider)


def test_payment_string_representation() -> None:
    payment = Payment(variant="default")
    assert str(payment) == "default"


def test_save_retries_when_token_already_exists() -> None:
    payment = Payment(variant="default")

    with (
        patch(
            "payments.models.uuid4",
            side_effect=["duplicate-token", "unique-token"],
        ),
        patch.object(Payment._default_manager, "filter") as mocked_filter,
        patch("django.db.models.Model.save") as mocked_save,
    ):
        mocked_filter.return_value.exists.side_effect = [True, False]

        payment.save()

    assert payment.token == "unique-token"
    assert mocked_filter.call_count == 2
    mocked_save.assert_called_once()


def test_save_detects_possible_infinite_token_loop() -> None:
    payment = Payment(variant="default")
    tokens = [f"token-{i}" for i in range(101)] + ["token-0"]

    with (
        patch("payments.models.uuid4", side_effect=tokens),
        patch.object(Payment._default_manager, "filter") as mocked_filter,
        pytest.raises(SystemExit, match="possible infinite loop"),
    ):
        mocked_filter.return_value.exists.return_value = True
        payment.save()

    assert mocked_filter.call_count == 101


def test_payment_get_form_delegates_to_provider() -> None:
    payment = Payment(variant="default")
    data = {"card_number": "4111111111111111"}
    provider = MagicMock()

    with patch(
        "payments.models.provider_factory",
        return_value=provider,
    ) as mocked_factory:
        result = payment.get_form(data=data)

    mocked_factory.assert_called_once_with("default", payment)
    provider.get_form.assert_called_once_with(payment, data=data)
    assert result == provider.get_form.return_value


@patch("payments.dummy.DummyProvider.refund")
def test_refund_logs_when_provider_refunds_more_than_captured(
    mocked_refund_method: MagicMock,
) -> None:
    mocked_refund_method.return_value = Decimal("150.00")

    payment = Payment(
        variant="default",
        status=PaymentStatus.CONFIRMED,
        captured_amount=Decimal("100.00"),
    )

    with (
        patch.object(BasePayment, "save"),
        patch("payments.models.logger.error") as mocked_logger,
    ):
        payment.refund()

    mocked_logger.assert_called_once()
    assert payment.captured_amount == Decimal("-50.00")


def test_get_base_url_uses_request_host_when_site_does_not_exist(
    monkeypatch,
) -> None:
    class SiteDoesNotExist(Exception):
        pass

    class FakeSiteObjects:
        def get_current(self, request):
            raise SiteDoesNotExist

    class FakeSite:
        DoesNotExist = SiteDoesNotExist
        objects = FakeSiteObjects()

    monkeypatch.setattr(core, "PAYMENT_HOST", None)
    monkeypatch.setattr(core, "Site", FakeSite, raising=False)

    request = MagicMock()
    request.get_host.return_value = "request.example.com"

    protocol = "https" if core.PAYMENT_USES_SSL else "http"

    assert core.get_base_url(request) == (
        f"{protocol}://request.example.com"
    )
    request.get_host.assert_called_once()


def test_get_base_url_uses_current_site_domain(monkeypatch) -> None:
    class FakeSite:
        domain = "site.example.com"

    class FakeSiteObjects:
        def get_current(self, request):
            return FakeSite()

    class FakeSiteModel:
        DoesNotExist = type("DoesNotExist", (Exception,), {})
        objects = FakeSiteObjects()

    monkeypatch.setattr(core, "PAYMENT_HOST", None)
    monkeypatch.setattr(core, "Site", FakeSiteModel, raising=False)

    protocol = "https" if core.PAYMENT_USES_SSL else "http"

    assert core.get_base_url() == f"{protocol}://site.example.com"


def test_custom_provider_factory_is_imported_from_settings(
    monkeypatch,
) -> None:
    import importlib

    class FakeFactory:
        pass

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_VARIANT_FACTORY",
        "fake.custom_factory",
        raising=False,
    )

    try:
        with patch(
            "django.utils.module_loading.import_string",
            return_value=FakeFactory,
        ) as mocked_import:
            importlib.reload(core)

            mocked_import.assert_called_once_with(
                "fake.custom_factory"
            )
            assert core.provider_factory is FakeFactory
    finally:
        monkeypatch.undo()
        importlib.reload(core)


def test_core_import_raises_without_payment_host_or_sites(
    monkeypatch,
) -> None:
    import importlib
    import sys

    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_HOST",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        core.settings,
        "INSTALLED_APPS",
        [
            app for app in core.settings.INSTALLED_APPS
            if app != "django.contrib.sites"
        ],
    )

    original_module = sys.modules["payments.core"]

    try:
        with pytest.raises(
            ImproperlyConfigured,
            match="without the sites app",
        ):
            importlib.reload(core)
    finally:
        monkeypatch.undo()
        importlib.reload(original_module)


def test_core_import_loads_site_when_payment_host_missing(
    monkeypatch,
) -> None:
    import importlib

    monkeypatch.setattr(
        core.settings,
        "PAYMENT_HOST",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        core.settings,
        "INSTALLED_APPS",
        [
            *core.settings.INSTALLED_APPS,
            "django.contrib.sites",
        ],
    )

    try:
        reloaded = importlib.reload(core)
        assert reloaded.Site.__name__ == "Site"
    finally:
        monkeypatch.undo()
        importlib.reload(core)
