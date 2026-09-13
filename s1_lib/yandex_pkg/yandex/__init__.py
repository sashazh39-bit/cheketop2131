"""Yandex Bank SBP receipt generator.

Self-contained: the donor template, the glyph bank and the extracted font all
live in `assets/`, so the package only needs `fonttools` at runtime.

    from yandex import generate_yandex_receipt, generate_yandex_variants

    pdf = generate_yandex_receipt({
        "doc_date":   "10.09.2026",
        "transfer":   "10.09.2026 01:45:12",
        "fio_from":   "Александр Евгеньевич Ж.",
        "phone_from": "+7 900 351-70-80",
        "fio_to":     "Дарья Романовна С.",
        "phone_to":   "+7 962 715-83-24",
        "bank_to":    "Сбербанк",
        "amount":     "9 900,00",
    })
"""

import datetime as _dt

from .make_yandex_receipt import (
    TEMPLATE,
    YandexReceiptError,
    build,
    build_variants,
    format_amount,
    make_auth_code,
    make_ref_number,
    make_sbp_id,
    msk_now,
)

__all__ = [
    "generate_yandex_receipt",
    "generate_yandex_variants",
    "receipt_for_payment",
    "payment_to_receipt_data",
    "YandexReceiptError",
    "format_amount",
    "make_auth_code",
    "make_ref_number",
    "make_sbp_id",
    "TEMPLATE",
]


def generate_yandex_receipt(data: dict, seed=None) -> bytes:
    """One receipt as PDF bytes. Mirrors `generate_alfa_receipt(data)`.

    Pass `seed` (e.g. the payment id) to make the file deterministic — the same
    payment then always produces the exact same receipt.
    """
    return build(data, seed=seed)


def payment_to_receipt_data(payment: dict) -> dict:
    """Map a FakePayment-shaped dict to the fields `build()` expects.

    Recognised keys: `date` (ISO or ДД.ММ.ГГГГ ЧЧ:ММ:СС), `amount`,
    `recipientName`, `recipientPhone`, `recipientBank`, plus a sender block
    `senderName` / `senderPhone` (usually taken from the profile settings).
    The document date is "now" in MSK — the moment the receipt is opened —
    exactly as a real download would stamp it.
    """
    raw = payment.get("date")
    if isinstance(raw, str) and "." in raw and ":" in raw:
        transfer = _dt.datetime.strptime(raw, "%d.%m.%Y %H:%M:%S")
    elif raw:
        # ISO 8601 (with optional Z); interpret as MSK wall time
        iso = str(raw).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_dt.timezone(_dt.timedelta(hours=3)))
            dt = dt.replace(tzinfo=None)
        transfer = dt.replace(microsecond=0)
    else:
        transfer = (msk_now() - _dt.timedelta(hours=2)).replace(microsecond=0)

    # The receipt is issued the same day as the payment (a genuine gap can be as
    # little as a minute), so the document date follows the payment — not the
    # wall clock. That keeps the file deterministic and avoids a document date
    # that lands on a different day than the /CreationDate for old payments.
    doc_date = payment.get("doc_date") or f"{transfer:%d.%m.%Y}"
    return {
        "doc_date":   doc_date,
        "transfer":   f"{transfer:%d.%m.%Y %H:%M:%S}",
        "fio_from":   payment.get("senderName") or "Клиент",
        "phone_from": payment.get("senderPhone") or "+7 900 000-00-00",
        "fio_to":     payment.get("recipientName") or "Получатель",
        "phone_to":   payment.get("recipientPhone") or "",
        "bank_to":    payment.get("recipientBank") or "Банк",
        "amount":     payment.get("amount"),
    }


def receipt_for_payment(payment: dict, seed=None) -> bytes:
    """FakePayment-shaped dict → receipt PDF, deterministic per `seed`.

    Defaults `seed` to the payment's own id so the same payment always opens the
    same receipt. Raises `YandexReceiptError` if a name/bank uses a glyph the
    font bank does not carry.
    """
    if seed is None:
        seed = payment.get("id") or payment.get("operationId")
    return build(payment_to_receipt_data(payment), seed=seed)


def generate_yandex_variants(data: dict, count: int = 3) -> list:
    """`count` interchangeable receipts for the same payment.

    Each item is a dict with `pdf` (bytes) plus the identifiers that ended up
    in that file: `ref_no`, `auth_code`, `sbp_id`, `creation_date`, `glyphs`,
    `size` and a `filename` in the UUID form Yandex Bank itself uses.
    """
    return build_variants(data, count)
