#!/usr/bin/env python3
"""Generic step-by-step wizard engine for statement editing.

Drives a field-by-field flow for both Alfa and VTB bank statements.
Each field is shown one at a time with an 'Оставить' (keep) button.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any

SURNAME_BY_LETTER: dict[str, str] = {
    "А": "Александров", "Б": "Борисов", "В": "Васильев", "Г": "Григорьев",
    "Д": "Дмитриев", "Е": "Егоров", "Ж": "Жуков", "З": "Захаров",
    "И": "Иванов", "К": "Козлов", "Л": "Лебедев", "М": "Михайлов",
    "Н": "Николаев", "О": "Орлов", "П": "Петров", "Р": "Романов",
    "С": "Сергеев", "Т": "Тихонов", "У": "Устинов", "Ф": "Федоров",
    "Х": "Харитонов", "Ц": "Цветков", "Ч": "Чернов", "Ш": "Шаров",
    "Щ": "Щербаков", "Э": "Эльдаров", "Ю": "Юдин", "Я": "Яковлев",
}

NAME_BY_LETTER: dict[str, str] = {
    "А": "Алексей", "Б": "Борис", "В": "Виктор", "Г": "Григорий",
    "Д": "Дмитрий", "Е": "Евгений", "Ж": "Жанна", "З": "Захар",
    "И": "Иван", "К": "Кирилл", "Л": "Леонид", "М": "Михаил",
    "Н": "Николай", "О": "Олег", "П": "Павел", "Р": "Роман",
    "С": "Сергей", "Т": "Тимур", "У": "Устин", "Ф": "Фёдор",
    "Х": "Харитон", "Ц": "Цезарь", "Ч": "Чеслав", "Ш": "Шамиль",
    "Щ": "Щукин", "Э": "Эдуард", "Ю": "Юрий", "Я": "Яков",
}

PATRONYMIC_BY_LETTER: dict[str, str] = {
    "А": "Алексеевич", "Б": "Борисович", "В": "Викторович", "Г": "Григорьевич",
    "Д": "Дмитриевич", "Е": "Евгеньевич", "Ж": "Жоресович", "З": "Захарович",
    "И": "Иванович", "К": "Кириллович", "Л": "Леонидович", "М": "Михайлович",
    "Н": "Николаевич", "О": "Олегович", "П": "Павлович", "Р": "Романович",
    "С": "Сергеевич", "Т": "Тимурович", "У": "Устинович", "Ф": "Фёдорович",
    "Х": "Харитонович", "Ц": "Цезаревич", "Ч": "Чеславович", "Ш": "Шамильевич",
    "Щ": "Щукинович", "Э": "Эдуардович", "Ю": "Юрьевич", "Я": "Яковлевич",
}

CITY_BY_LETTER: dict[str, str] = {
    "А": "Архангельск", "Б": "Барнаул", "В": "Владимир", "Г": "Гатчина",
    "Д": "Дмитров", "Е": "Екатеринбург", "З": "Зеленоград", "И": "Иркутск",
    "К": "Калининград", "Л": "Липецк", "М": "Москва", "Н": "Нижний Новгород",
    "О": "Омск", "П": "Пенза", "Р": "Ростов-на-Дону", "С": "Советск",
    "Т": "Тула", "У": "Ульяновск", "Ф": "Фрязино", "Х": "Хабаровск",
    "Ч": "Челябинск", "Ш": "Шахты", "Э": "Электросталь", "Я": "Ярославль",
}

# ── Field definitions ────────────────────────────────────────────────────

ALFA_BLOCK1_FIELDS = [
    {"key": "номер_счета", "label": "📋 Номер счёта", "block": 1, "validate": "account"},
    {"key": "дата_открытия", "label": "📅 Дата открытия счёта", "block": 1},
    {"key": "валюта", "label": "💱 Валюта счёта", "block": 1},
    {"key": "тип_счета", "label": "📝 Тип счёта", "block": 1},
    {"key": "дата_формирования", "label": "📅 Дата формирования выписки", "block": 1, "unique_replace": True},
    {"key": "клиент", "label": "👤 ФИО клиента", "block": 1, "suggest_fio": True},
    {"key": "адрес", "label": "🏠 Адрес регистрации", "block": 1, "suggest_address": True},
]

ALFA_OP_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции", "block": 2},
    {"key": "номер_операции", "label": "🔢 Номер операции", "block": 2},
    {"key": "телефон", "label": "📱 Телефон/карта", "block": 2},
    {"key": "сумма", "label": "💰 Сумма операции", "block": 2},
]

ALFA_BLOCK3_FIELDS = [
    {"key": "период_с", "label": "📅 Период с", "block": 3},
    {"key": "период_по", "label": "📅 Период по", "block": 3},
    {"key": "текущий_баланс", "label": "💰 Текущий баланс", "block": 3},
]

VTB_BLOCK1_FIELDS = [
    {"key": "фио", "label": "👤 ФИО клиента", "block": 1, "suggest_fio": True},
    {"key": "номер_счета", "label": "📋 Номер счёта", "block": 1, "validate": "account"},
    {"key": "период_start", "label": "📅 Начало периода", "block": 1},
    {"key": "период_end", "label": "📅 Конец периода", "block": 1},
]

VTB_OP_FIELDS = [
    {"key": "дата", "label": "📅 Дата операции", "block": 2},
    {"key": "время", "label": "🕐 Время операции", "block": 2},
    {"key": "сумма", "label": "💰 Сумма (списание)", "block": 2},
    {"key": "сумма_зачисление", "label": "💰 Сумма (зачисление)", "block": 2},
    {"key": "комиссия", "label": "💰 Комиссия", "block": 2},
    {"key": "описание", "label": "📝 Описание", "block": 2},
]

VTB_BLOCK3_FIELDS = [
    {"key": "баланс_начало", "label": "💰 Баланс на начало периода", "block": 3},
]


# ── Wizard state helpers ─────────────────────────────────────────────────

def build_field_sequence(bank: str, operations: list[dict] | None = None) -> list[dict]:
    """Build flat field list: Block1 + per-operation Block2 + Block3."""
    if bank == "alfa":
        fields = list(ALFA_BLOCK1_FIELDS)
        for i, op in enumerate(operations or []):
            for f in ALFA_OP_FIELDS:
                fields.append({
                    **f,
                    "key": f"op_{i}_{f['key']}",
                    "label": f"{f['label']} (оп. {i + 1})",
                    "op_index": i,
                    "op_field": f["key"],
                })
        fields.extend(ALFA_BLOCK3_FIELDS)
    else:
        fields = list(VTB_BLOCK1_FIELDS)
        for i, op in enumerate(operations or []):
            for f in VTB_OP_FIELDS:
                fields.append({
                    **f,
                    "key": f"op_{i}_{f['key']}",
                    "label": f"{f['label']} (оп. {i + 1})",
                    "op_index": i,
                    "op_field": f["key"],
                })
        fields.extend(VTB_BLOCK3_FIELDS)
    return fields


def init_wizard_state(
    bank: str,
    mode: str,
    scanned_block1: dict,
    scanned_ops: list[dict],
    file_path: str | None = None,
    file_name: str | None = None,
) -> dict:
    """Create initial USER_STATE for a statement wizard."""
    fields = build_field_sequence(bank, scanned_ops)
    current_values = {}
    for k, v in scanned_block1.items():
        current_values[k] = v
    for i, op in enumerate(scanned_ops):
        for k, v in op.items():
            current_values[f"op_{i}_{k}"] = v
    return {
        "mode": mode,
        "bank": bank,
        "fields": fields,
        "field_idx": 0,
        "current_values": current_values,
        "changes": {},
        "operations": scanned_ops,
        "file_path": file_path,
        "file_name": file_name,
    }


def get_current_field(state: dict) -> dict | None:
    """Return the field dict for the current step, or None if done."""
    idx = state.get("field_idx", 0)
    fields = state.get("fields", [])
    if idx >= len(fields):
        return None
    return fields[idx]


def advance_field(state: dict) -> dict | None:
    """Move to next field. Return new field or None if wizard complete."""
    state["field_idx"] = state.get("field_idx", 0) + 1
    return get_current_field(state)


def format_step_message(field: dict, current_value: str | None) -> str:
    """Format the prompt message for a wizard step."""
    block = field.get("block", 0)
    block_names = {1: "Информация о счёте", 2: "Операции", 3: "Баланс счёта"}
    header = f"📋 Блок {block} — {block_names.get(block, '')}\n\n"
    label = field["label"]
    if current_value:
        msg = f"{header}{label}\n\nТекущее значение:\n`{current_value}`\n\nВведите новое значение или нажмите «Оставить»:"
    else:
        msg = f"{header}{label}\n\nЗначение не найдено.\n\nВведите значение или нажмите «Пропустить»:"
    return msg


def get_step_keyboard(field: dict, current_value: str | None, callback_prefix: str = "sw") -> list[list[dict]]:
    """Build inline keyboard for a wizard step."""
    kb: list[list[dict]] = []
    if current_value:
        display = current_value[:40] + ("..." if len(current_value) > 40 else "")
        kb.append([{"text": f"📌 Оставить: {display}", "callback_data": f"{callback_prefix}_keep"}])
    else:
        kb.append([{"text": "⏭ Пропустить", "callback_data": f"{callback_prefix}_skip"}])

    if field.get("suggest_fio") and current_value:
        kb.append([{"text": "💡 Предложить замену ФИО", "callback_data": f"{callback_prefix}_suggest_fio"}])
    if field.get("suggest_address") and current_value:
        kb.append([{"text": "💡 Предложить замену адреса", "callback_data": f"{callback_prefix}_suggest_addr"}])
    return kb


def validate_account_number(old_val: str, new_val: str) -> tuple[bool, str]:
    """Validate account number structure. If only last 4 digits changed, check basic structure."""
    new_clean = re.sub(r"\s", "", new_val)
    if not re.match(r"^\d{20}$", new_clean):
        return False, "Номер счёта должен содержать 20 цифр."
    if old_val and new_clean[:16] == old_val[:16]:
        if not new_clean.startswith("408"):
            return False, "Номер счёта физ. лица должен начинаться с 408."
    return True, ""


def validate_input(field: dict, value: str, available_chars: set[str] | None = None) -> tuple[bool, str]:
    """Validate user input for a field."""
    if field.get("validate") == "account":
        current = field.get("_current", "")
        ok, msg = validate_account_number(current, value)
        if not ok:
            return False, msg

    if available_chars is not None:
        missing = []
        seen = set()
        for ch in value:
            if ch not in available_chars and not ch.isspace() and ch not in seen:
                missing.append(ch)
                seen.add(ch)
        if missing:
            return False, f"⚠️ Недоступные символы: {''.join(missing)}. Попробуйте другое значение или введите в формате старое=новое."
    return True, ""


def suggest_fio(current_fio: str, available_chars: set[str] | None = None) -> list[str]:
    """Suggest replacement FIO based on first letters and available chars."""
    parts = current_fio.split()
    if not parts:
        return ["Иванов Иван Иванович"]

    first_letter_surname = parts[0][0].upper() if parts[0] else "И"
    first_letter_name = parts[1][0].upper() if len(parts) > 1 and parts[1] else "И"
    first_letter_patron = parts[2][0].upper() if len(parts) > 2 and parts[2] else "И"

    surname = SURNAME_BY_LETTER.get(first_letter_surname, "Иванов")
    name = NAME_BY_LETTER.get(first_letter_name, "Иван")
    patronymic = PATRONYMIC_BY_LETTER.get(first_letter_patron, "Иванович")

    suggestions = [f"{surname} {name} {patronymic}"]

    if available_chars:
        def fits(text: str) -> bool:
            return all(ch in available_chars or ch.isspace() for ch in text)
        suggestions = [s for s in suggestions if fits(s)]
        if not suggestions:
            for sl in "ИПВМКСАТНГ":
                for nl in "ИПВМКСАТНГ":
                    s = SURNAME_BY_LETTER.get(sl, "")
                    n = NAME_BY_LETTER.get(nl, "")
                    p = PATRONYMIC_BY_LETTER.get(nl, "")
                    candidate = f"{s} {n} {p}"
                    if fits(candidate):
                        suggestions.append(candidate)
                        if len(suggestions) >= 3:
                            return suggestions
    return suggestions[:3]


def suggest_address(current_address: str, available_chars: set[str] | None = None) -> list[str]:
    """Suggest replacement address parts based on available chars."""
    m = re.match(r"(\d+),\s*РОССИЯ", current_address)
    old_index = m.group(1) if m else "100000"

    city_m = re.search(r"(?:город|г\.)\s*(\S+)", current_address, re.IGNORECASE)
    if not city_m:
        city_m = re.search(r",\s*([А-ЯЁ][а-яё]+)\s*,", current_address)
    old_city = city_m.group(1) if city_m else ""

    first_letter = old_city[0].upper() if old_city else "М"
    new_city = CITY_BY_LETTER.get(first_letter, "Москва")

    suggestion = f"{old_index}, РОССИЯ, {new_city}"
    suggestions = [suggestion]

    if available_chars:
        def fits(t: str) -> bool:
            return all(ch in available_chars or ch.isspace() or ch in "0123456789,." for ch in t)
        suggestions = [s for s in suggestions if fits(s)]
        if not suggestions:
            for letter in "МСПКТВИ":
                c = CITY_BY_LETTER.get(letter, "")
                candidate = f"{old_index}, РОССИЯ, {c}"
                if fits(candidate):
                    suggestions.append(candidate)
                    if len(suggestions) >= 2:
                        return suggestions
    return suggestions[:2]


# ── Block 3 auto-calculations ────────────────────────────────────────────

def calc_alfa_block3(
    текущий_баланс: float,
    расходы: float,
    поступления: float,
) -> dict[str, float]:
    """Calculate Alfa Block 3 derived values.

    входящий_остаток = текущий_баланс + расходы - поступления
    исходящий_остаток = платежный_лимит = текущий_баланс
    """
    return {
        "входящий_остаток": текущий_баланс + расходы - поступления,
        "поступления": поступления,
        "расходы": расходы,
        "исходящий_остаток": текущий_баланс,
        "платежный_лимит": текущий_баланс,
        "текущий_баланс": текущий_баланс,
    }


def calc_vtb_block3(
    баланс_начало: float,
    расходы: float,
    поступления: float,
) -> dict[str, float]:
    """Calculate VTB Block 3 derived values.

    баланс_конец = баланс_начало - расходы
    """
    return {
        "баланс_начало": баланс_начало,
        "поступления": поступления,
        "расходные_операции": расходы,
        "баланс_конец": баланс_начало - расходы,
    }


def sum_operations_expense(state: dict) -> float:
    """Sum up operation amounts from wizard changes (for Block 3 расходы)."""
    changes = state.get("changes", {})
    ops = state.get("operations", [])
    total = 0.0
    for i, op in enumerate(ops):
        key = f"op_{i}_сумма"
        val_str = changes.get(key, op.get("сумма", "0"))
        val_str = str(val_str).replace(" ", "").replace("\xa0", "").replace(",", ".")
        val_str = val_str.lstrip("-")
        try:
            total += abs(float(val_str))
        except ValueError:
            pass
    return total


def sum_operations_income(state: dict) -> float:
    """Sum up income operations (VTB зачисление)."""
    changes = state.get("changes", {})
    ops = state.get("operations", [])
    total = 0.0
    for i, op in enumerate(ops):
        key = f"op_{i}_сумма_зачисление"
        val_str = changes.get(key, op.get("сумма_зачисление", "0"))
        val_str = str(val_str).replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            total += abs(float(val_str))
        except ValueError:
            pass
    return total


def format_amount_rur(val: float) -> str:
    """Format as Alfa-style: '19 121,65'."""
    integer = int(abs(val))
    frac = abs(val) - integer
    cents = round(frac * 100)
    s = f"{integer:,}".replace(",", " ")
    result = f"{s},{cents:02d}"
    if val < 0:
        result = "-" + result
    return result


def format_amount_rub(val: float) -> str:
    """Format as VTB-style: '39,862.00'."""
    integer = int(abs(val))
    frac = abs(val) - integer
    cents = round(frac * 100)
    s = f"{integer:,}"
    result = f"{s}.{cents:02d}"
    if val < 0:
        result = "-" + result
    return result


# ── Preview formatting ───────────────────────────────────────────────────

def format_preview(state: dict) -> str:
    """Format a preview of all 3 blocks with current/changed values."""
    bank = state.get("bank", "alfa")
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    ops = state.get("operations", [])

    def val(key: str) -> str:
        return changes.get(key, current.get(key, "—"))

    lines = ["🏦 Предпросмотр выписки\n"]

    if bank == "alfa":
        lines.append("📋 Блок 1 — Информация о счёте")
        lines.append(f"  Номер счёта: {val('номер_счета')}")
        lines.append(f"  Дата открытия: {val('дата_открытия')}")
        lines.append(f"  Валюта: {val('валюта')}")
        lines.append(f"  Тип счёта: {val('тип_счета')}")
        lines.append(f"  Дата формирования: {val('дата_формирования')}")
        lines.append(f"  Клиент: {val('клиент')}")
        lines.append(f"  Адрес: {val('адрес')}")
        lines.append("")
        lines.append("📋 Блок 2 — Операции")
        for i, op in enumerate(ops):
            d = val(f"op_{i}_дата")
            n = val(f"op_{i}_номер_операции")
            p = val(f"op_{i}_телефон")
            s = val(f"op_{i}_сумма")
            lines.append(f"  Оп.{i+1}: {d} | {n} | {p} | {s} RUR")
        lines.append("")
        expenses = sum_operations_expense(state)
        bal_str = changes.get("текущий_баланс", current.get("текущий_баланс", "0"))
        try:
            bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
        except ValueError:
            bal = 0.0
        income_str = current.get("поступления", "0")
        try:
            income = float(str(income_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
        except ValueError:
            income = 0.0
        calc = calc_alfa_block3(bal, expenses, income)
        lines.append("📋 Блок 3 — Баланс счёта")
        lines.append(f"  Период: {val('период_с')} — {val('период_по')}")
        lines.append(f"  Входящий остаток: {format_amount_rur(calc['входящий_остаток'])} RUR")
        lines.append(f"  Поступления: {format_amount_rur(calc['поступления'])} RUR")
        lines.append(f"  Расходы: {format_amount_rur(calc['расходы'])} RUR")
        lines.append(f"  Исходящий остаток: {format_amount_rur(calc['исходящий_остаток'])} RUR")
        lines.append(f"  Платежный лимит: {format_amount_rur(calc['платежный_лимит'])} RUR")
        lines.append(f"  Текущий баланс: {format_amount_rur(calc['текущий_баланс'])} RUR")
    else:
        lines.append("📋 Блок 1 — Информация о счёте")
        lines.append(f"  ФИО: {val('фио')}")
        lines.append(f"  Номер счёта: {val('номер_счета')}")
        lines.append(f"  Период: {val('период_start')} — {val('период_end')}")
        lines.append("")
        lines.append("📋 Блок 2 — Операции")
        for i, op in enumerate(ops):
            d = val(f"op_{i}_дата")
            t = val(f"op_{i}_время")
            s = val(f"op_{i}_сумма")
            desc = val(f"op_{i}_описание")
            lines.append(f"  Оп.{i+1}: {d} {t} | {s} RUB | {desc[:30]}")
        lines.append("")
        expenses = sum_operations_expense(state)
        income = sum_operations_income(state)
        bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
        try:
            bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
        except ValueError:
            bal = 0.0
        calc = calc_vtb_block3(bal, expenses, income)
        lines.append("📋 Блок 3 — Баланс счёта")
        lines.append(f"  Баланс на начало: {format_amount_rub(calc['баланс_начало'])} RUB")
        lines.append(f"  Поступления: {format_amount_rub(calc['поступления'])} RUB")
        lines.append(f"  Расходные операции: {format_amount_rub(calc['расходные_операции'])} RUB")
        lines.append(f"  Баланс на конец: {format_amount_rub(calc['баланс_конец'])} RUB")

    return "\n".join(lines)


def get_preview_keyboard(callback_prefix: str = "sw") -> list[list[dict]]:
    """Keyboard for the preview screen."""
    return [
        [{"text": "✅ Сформировать PDF", "callback_data": f"{callback_prefix}_generate"}],
        [{"text": "✏️ Редактировать Блок 1", "callback_data": f"{callback_prefix}_edit_b1"},
         {"text": "✏️ Блок 2", "callback_data": f"{callback_prefix}_edit_b2"},
         {"text": "✏️ Блок 3", "callback_data": f"{callback_prefix}_edit_b3"}],
        [{"text": "⬅️ Главное меню", "callback_data": "main_back"}],
    ]


def jump_to_block(state: dict, block_num: int) -> dict | None:
    """Jump wizard to the first field of a given block number."""
    fields = state.get("fields", [])
    for i, f in enumerate(fields):
        if f.get("block") == block_num:
            state["field_idx"] = i
            return f
    return None


# ── Replacement pair builder ─────────────────────────────────────────────

def build_replacement_pairs(state: dict) -> list[tuple[str, str]]:
    """Build (old_value, new_value) pairs from wizard changes for CID patching."""
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    pairs = []
    for key, new_val in changes.items():
        old_val = current.get(key)
        if old_val and old_val != new_val:
            pairs.append((str(old_val), str(new_val)))
    return pairs
