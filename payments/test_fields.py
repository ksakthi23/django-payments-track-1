from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from payments.fields import CreditCardNumberField


def test_validate_rejects_card_type_not_in_valid_types() -> None:
    # 4111111111111111 is a valid Visa test number (passes Luhn check)
    field = CreditCardNumberField(valid_types=["mastercard"])
    with pytest.raises(ValidationError, match="We accept only MasterCard"):
        field.validate("4111111111111111")


def test_card_number_to_python_handles_none() -> None:
    field = CreditCardNumberField()
    assert field.to_python(None) == ""


def test_card_number_rejects_empty_required_value() -> None:
    field = CreditCardNumberField(required=True)

    with pytest.raises(ValidationError):
        field.validate("")


def test_card_number_rejects_invalid_checksum() -> None:
    field = CreditCardNumberField()

    with pytest.raises(ValidationError):
        field.validate("4111111111111112")


def test_expiry_field_accepts_custom_error_messages() -> None:
    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField(
        error_messages={"invalid_month": "Custom month error."}
    )

    assert field.error_messages["invalid_month"] == "Custom month error."


def test_expiry_field_rejects_past_date() -> None:
    from datetime import date
    from unittest.mock import patch

    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField()

    with patch("payments.fields.localdate", return_value=date(2026, 11, 10)):
        with pytest.raises(ValidationError, match="expiration date"):
            field.clean(["10", "2026"])


def test_expiry_compress_rejects_missing_year() -> None:
    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField()

    with pytest.raises(ValidationError):
        field.compress(["11", ""])


def test_expiry_compress_rejects_missing_month() -> None:
    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField()

    with pytest.raises(ValidationError):
        field.compress(["", "2027"])


def test_expiry_compress_returns_none_for_empty_data() -> None:
    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField()

    assert field.compress([]) is None


def test_verification_field_rejects_empty_required_value() -> None:
    from payments.fields import CreditCardVerificationField

    field = CreditCardVerificationField(required=True)

    with pytest.raises(ValidationError):
        field.validate("")


def test_card_number_to_python_removes_spaces_and_hyphens() -> None:
    field = CreditCardNumberField()
    assert field.to_python("4111 1111-1111 1111") == "4111111111111111"


def test_card_number_checksum_rejects_non_digits() -> None:
    assert CreditCardNumberField.cart_number_checksum_validation(
        "4111abcd11111111"
    ) is False


def test_card_number_checksum_doubles_digits_above_four() -> None:
    assert CreditCardNumberField.cart_number_checksum_validation(
        "79927398713"
    ) is True


def test_card_number_accepts_allowed_card_type() -> None:
    field = CreditCardNumberField(valid_types=["visa"])
    field.validate("4111111111111111")


def test_expiry_field_accepts_future_date() -> None:
    from datetime import date
    from unittest.mock import patch

    from payments.fields import CreditCardExpiryField

    field = CreditCardExpiryField()

    with patch("payments.fields.localdate", return_value=date(2026, 11, 10)):
        result = field.clean(["12", "2026"])

    assert result == date(2026, 12, 31)


def test_verification_field_rejects_invalid_format() -> None:
    from payments.fields import CreditCardVerificationField

    field = CreditCardVerificationField()

    with pytest.raises(ValidationError):
        field.validate("12A")


def test_verification_field_accepts_empty_optional_value() -> None:
    from payments.fields import CreditCardVerificationField

    field = CreditCardVerificationField(required=False)
    field.validate("")
