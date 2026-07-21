from enum import StrEnum

from aiogram.filters.callback_data import CallbackData


class PaymentMethod(StrEnum):
    STARS = "stars"
    YOOKASSA = "yookassa"
    YOOKASSA_DISABLED = "disabled"


class PaymentAction(StrEnum):
    CANCEL_STARS = "cancel_stars"
    CANCEL_YOOKASSA = "cancel_yookassa"
    RETRY_PEER = "retry_peer"


class PaymentMethodCallback(CallbackData, prefix="pay"):
    method: PaymentMethod
    tariff: str
    user_id: int


class PaymentActionCallback(CallbackData, prefix="pact"):
    action: PaymentAction
    tariff: str
    user_id: int


class AdminPageCallback(CallbackData, prefix="apage"):
    view: str
    page: int


class AdminClientCallback(CallbackData, prefix="aclient"):
    action: str
    user_id: int


class AdminDiscountCallback(CallbackData, prefix="adiscount"):
    user_id: int
    value: int


class RefundConfirmationCallback(CallbackData, prefix="refund"):
    payment_id: str
