"""Сезон 1: PDF без перевода — Альфа / Яндекс столбиком."""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "s1_lib"
_ALFA_DESK = Path.home() / "Desktop" / "Альфа-чеки"
_YA_DESK = Path.home() / "Desktop" / "ретранслятор-альфа" / "pdf-service"

for _p in (_VENDOR / "yandex_pkg", _VENDOR, _YA_DESK, _ALFA_DESK):
    if _p.is_dir():
        s = str(_p)
        if s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)

from generator import AlfaReceiptError, account_control_key, build as build_alfa  # noqa: E402
from statement import StatementError, build as build_statement  # noqa: E402
from yandex import YandexReceiptError, generate_yandex_receipt  # noqa: E402

HINT_ALFA = (
    "Чек Альфа · СБП\n"
    "Пиши столбиком, без подписей — одно значение на строку:\n\n"
    "34090\n"
    "11.09.2026 00:42\n"
    "Андрей Кириллович Н\n"
    "+79962323353\n"
    "Сбербанк\n"
    "9039\n"
    "Перевод денежных средств\n\n"
    "1 сумма\n"
    "2 дата и время\n"
    "3 получатель\n"
    "4 телефон получателя\n"
    "5 банк\n"
    "6 последние 4 цифры счёта\n"
    "7 комментарий"
)

HINT_YANDEX = (
    "Чек Яндекс · СБП\n"
    "Пиши столбиком, без подписей:\n\n"
    "9900\n"
    "10.09.2026 01:45\n"
    "Александр Евгеньевич Ж.\n"
    "+79003517080\n"
    "Дарья Романовна С.\n"
    "+79627158324\n"
    "Сбербанк\n\n"
    "1 сумма\n"
    "2 дата и время\n"
    "3 отправитель\n"
    "4 телефон отправителя\n"
    "5 получатель\n"
    "6 телефон получателя\n"
    "7 банк получателя"
)

HINT_STMT = (
    "Выписка Альфа · с нуля\n"
    "Одним сообщением: шапка, пустая строка, операции.\n\n"
    "Кирилон Максим Игорович\n"
    "40817810980480009039\n"
    "122319, РОССИЯ, Москва, ул. Тверская, д. 3, кв. 10\n"
    "16.05.2025\n"
    "11.09.2026\n"
    "4698.08\n"
    "\n"
    "11.09.2026|3750|Перевод через Систему быстрых платежей от +79003451149. Без НДС.\n"
    "11.09.2026|-300|Перевод через Систему быстрых платежей на +7 (900) 345-11-49. Без НДС.\n\n"
    "Шапка:\n"
    "1 ФИО\n"
    "2 счёт (20 цифр)\n"
    "3 адрес\n"
    "4 дата открытия\n"
    "5 дата формирования\n"
    "6 входящий остаток\n\n"
    "Операции: дата|сумма|описание  (+ приход, − расход)\n"
    "До ~12 операций на одну страницу."
)

HOME_TEXT = "PDF без перевода.\nЧек — Альфа или Яндекс.\nВыписка — Альфа."
HOME_KB = [
    [{"text": "Чек", "callback_data": "s1_check"}, {"text": "Выписка", "callback_data": "s1_stmt"}],
    [{"text": "БОТ 2 СЕЗОНА", "callback_data": "s2_home"}],
]


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]


def _amount(s: str) -> float:
    s = str(s).replace("\xa0", " ").replace("RUR", "").replace("₽", "")
    s = s.replace(" ", "").replace(",", ".").strip()
    return float(s)


def _dt(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    s = re.sub(r"\s*мск\s*$", "", s, flags=re.I)
    for fmt, out in (
        (r"^(\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2}:\d{2})$", r"\1 \2"),
        (r"^(\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2})$", r"\1 \2:00"),
        (r"^(\d{2}\.\d{2}\.\d{4})$", None),
    ):
        m = re.match(fmt, s)
        if m:
            if out is None:
                raise ValueError("нужны дата и время, например 11.09.2026 00:42")
            return m.expand(out)
    raise ValueError(f"непонятная дата: {s}")


def _phone_digits(s: str) -> str:
    d = re.sub(r"\D", "", s)
    if len(d) == 10:
        d = "7" + d
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) != 11 or d[0] != "7":
        raise ValueError(f"непонятный телефон: {s}")
    return d


def phone_alfa(s: str) -> str:
    d = _phone_digits(s)
    return f"+7 ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:11]}"


def phone_yandex(s: str) -> str:
    d = _phone_digits(s)
    return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}"


def account_from_last4(last4: str) -> str:
    digits = re.sub(r"\D", "", last4)
    if len(digits) == 20:
        return digits
    if len(digits) < 4:
        raise ValueError("нужны последние 4 цифры счёта")
    tail = digits[-4:]
    mid = f"{random.randint(0, 999):03d}"
    body = "40817810" + "0" + "8048" + mid + tail
    return body[:8] + str(account_control_key(body)) + body[9:]


def parse_alfa_check(text: str) -> dict:
    rows = _lines(text)
    if len(rows) != 7:
        raise ValueError(f"нужно 7 строк, пришло {len(rows)}")
    amount, date, recipient, phone, bank, last4, comment = rows
    return {
        "amount": _amount(amount),
        "transfer_dt": _dt(date),
        "recipient": recipient,
        "phone": phone_alfa(phone),
        "bank": bank,
        "account": account_from_last4(last4),
        "message": comment,
    }


def parse_yandex_check(text: str) -> dict:
    rows = _lines(text)
    if len(rows) != 7:
        raise ValueError(f"нужно 7 строк, пришло {len(rows)}")
    amount, date, fio_from, phone_from, fio_to, phone_to, bank = rows
    transfer = _dt(date)
    day = transfer.split()[0]
    return {
        "amount": _amount(amount),
        "transfer": transfer,
        "doc_date": day,
        "fio_from": fio_from,
        "phone_from": phone_yandex(phone_from),
        "fio_to": fio_to,
        "phone_to": phone_yandex(phone_to),
        "bank_to": bank,
    }


def parse_statement(text: str) -> dict:
    raw = text.replace("\r\n", "\n").strip("\n")
    if "\n\n" in raw:
        head, _, tail = raw.partition("\n\n")
        header = _lines(head)
        ops_lines = _lines(tail)
    else:
        all_lines = _lines(raw)
        header, ops_lines = all_lines[:6], all_lines[6:]
    if len(header) != 6:
        raise ValueError(f"в шапке нужно 6 строк, пришло {len(header)}")
    if not ops_lines:
        raise ValueError("нет операций — после пустой строки напиши дата|сумма|описание")
    client, account, address, opened, formed, opening = header
    acc = re.sub(r"\D", "", account)
    if len(acc) != 20:
        raise ValueError("счёт должен быть 20 цифр")
    ops = []
    for spec in ops_lines:
        parts = [p.strip() for p in spec.split("|")]
        if len(parts) < 2:
            raise ValueError(f"операция: дата|сумма|описание  (не {spec!r})")
        date, amt = parts[0], _amount(parts[1])
        desc = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
        op = {
            "type": "sbp_in" if amt > 0 else "sbp_out",
            "date": date,
            "amount": amt,
        }
        if desc:
            op["description"] = desc
        ops.append(op)
    if len(ops) > 15:
        raise ValueError(f"на одну страницу влезает ~12–15 операций, сейчас {len(ops)}")
    return {
        "client": client,
        "account": acc,
        "address": address,
        "opened": opened,
        "formed": formed,
        "period_from": formed,
        "period_to": formed,
        "opening_balance": _amount(opening),
        "currency": "RUR",
        "acc_type": "Текущий счёт",
        "ops": ops,
    }


def make_pdf(kind: str, text: str) -> tuple[bytes, str]:
    if kind == "s1_alfa_check":
        return build_alfa(parse_alfa_check(text)), "alfa_sbp.pdf"
    if kind == "s1_yandex_check":
        return generate_yandex_receipt(parse_yandex_check(text)), "yandex_sbp.pdf"
    if kind == "s1_alfa_stmt":
        return build_statement(parse_statement(text)), "alfa_statement.pdf"
    raise ValueError("неизвестный тип")


S1_ERRORS = (ValueError, AlfaReceiptError, YandexReceiptError, StatementError)
