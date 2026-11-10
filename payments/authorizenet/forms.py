from __future__ import annotations

from payments import PaymentStatus
from payments.forms import CreditCardPaymentForm

RESPONSE_STATUS = {
    "1": PaymentStatus.CONFIRMED,
    "2": PaymentStatus.REJECTED,
}

INVALID_RESPONSE_MESSAGE = (
    "Invalid or incomplete response received from Authorize.Net."
)


class PaymentForm(CreditCardPaymentForm):
    def clean(self):
        cleaned_data = super().clean()

        if self.errors or self.payment.transaction_id:
            return cleaned_data

        request_data = {
            "x_card_num": cleaned_data.get("number"),
            "x_exp_date": cleaned_data.get("expiration"),
            "x_card_code": cleaned_data.get("cvv2"),
        }

        response = self.provider.get_payment_response(
            self.payment, request_data
        )

        response_data = response.text.split("|")

        if len(response_data) < 7:
            message = INVALID_RESPONSE_MESSAGE

        else:
            response_code = response_data[0]
            message = response_data[3]
            transaction_id = response_data[6].strip()

            if (
                response.ok
                and response_code == "1"
                and not transaction_id
            ):
                message = INVALID_RESPONSE_MESSAGE
            elif not response.ok or response_code not in RESPONSE_STATUS:
                message = message or INVALID_RESPONSE_MESSAGE
            else:
                status = RESPONSE_STATUS[response_code]
                if status == PaymentStatus.REJECTED:
                    message = message or "The transaction was declined."

                self.payment.transaction_id = transaction_id
                self.payment.captured_amount = (
                    self.payment.total
                    if status == PaymentStatus.CONFIRMED
                    else 0
                )

                self.payment.change_status(
                    status,
                    message=message,
                    update_fields=["transaction_id", "captured_amount"],
                )

                if status == PaymentStatus.CONFIRMED:
                    return cleaned_data

                self._errors["__all__"] = self.error_class([message])
                return cleaned_data

        self.payment.captured_amount = 0
        self.payment.change_status(
            PaymentStatus.ERROR,
            message=message,
            update_fields=["captured_amount"],
        )

        self._errors["__all__"] = self.error_class([message])

        return cleaned_data
