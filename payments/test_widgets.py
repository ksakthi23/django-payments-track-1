from __future__ import annotations

from datetime import date

import pytest

from payments.widgets import CreditCardExpiryWidget
from payments.widgets import CreditCardNumberWidget


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("1234567890123456", "1234 5678 9012 3456"),
        ("123456789012345", "1234 567890 12345"),
        ("12345678901234", "1234 567890 1234"),
        ("1234-5678-9012-3456", "1234 5678 9012 3456"),
        ("1234 5678 9012 3456", "1234 5678 9012 3456"),
        ("123456", "123456"),
        ("", ""),
        (None, ""),
    ],
)
def test_credit_card_number_widget_render(number, expected):
    html = CreditCardNumberWidget().render("card_number", number)

    assert expected in html


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2030, 8, 1), [8, 2030]),
        (None, [None, None]),
    ],
)
def test_credit_card_expiry_widget_decompress(value, expected):
    widget = CreditCardExpiryWidget(widgets=[])

    assert widget.decompress(value) == expected
