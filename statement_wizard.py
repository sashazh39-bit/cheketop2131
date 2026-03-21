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

SURNAMES_BY_LETTER: dict[str, list[str]] = {
    "А": ["Александров", "Андреев", "Алексеев"], "Б": ["Борисов", "Белов", "Богданов"],
    "В": ["Васильев", "Волков", "Виноградов"], "Г": ["Григорьев", "Горбунов", "Голубев"],
    "Д": ["Дмитриев", "Данилов", "Давыдов"], "Е": ["Егоров", "Ершов", "Елисеев"],
    "Ж": ["Жуков", "Журавлев", "Жданов"], "З": ["Захаров", "Зайцев", "Зимин"],
    "И": ["Иванов", "Ильин", "Исаев"], "К": ["Козлов", "Кузнецов", "Киселев"],
    "Л": ["Лебедев", "Лазарев", "Логинов"], "М": ["Михайлов", "Морозов", "Макаров"],
    "Н": ["Николаев", "Никитин", "Новиков"], "О": ["Орлов", "Осипов", "Овчинников"],
    "П": ["Петров", "Павлов", "Попов"], "Р": ["Романов", "Рыбаков", "Родионов"],
    "С": ["Сергеев", "Смирнов", "Соколов"], "Т": ["Тихонов", "Тарасов", "Титов"],
    "У": ["Устинов", "Уваров", "Ушаков"], "Ф": ["Федоров", "Филиппов", "Фролов"],
    "Х": ["Харитонов", "Хохлов", "Хорошев"], "Ц": ["Цветков", "Царев", "Цыганов"],
    "Ч": ["Чернов", "Чистяков", "Чуйков"], "Ш": ["Шаров", "Шилов", "Шестаков"],
    "Щ": ["Щербаков", "Щукин", "Щеглов"], "Э": ["Эльдаров", "Ефимов", "Егоров"],
    "Ю": ["Юдин", "Юрьев", "Юсупов"], "Я": ["Яковлев", "Яшин", "Ярцев"],
}

NAMES_BY_LETTER: dict[str, list[str]] = {
    "А": ["Алексей", "Андрей", "Артем"], "Б": ["Борис", "Богдан", "Булат"],
    "В": ["Виктор", "Владимир", "Валерий"], "Г": ["Григорий", "Георгий", "Глеб"],
    "Д": ["Дмитрий", "Данил", "Денис"], "Е": ["Евгений", "Егор", "Елисей"],
    "Ж": ["Жанна", "Жаргал", "Ждан"], "З": ["Захар", "Зинаида", "Зоя"],
    "И": ["Иван", "Игорь", "Илья"], "К": ["Кирилл", "Константин", "Клим"],
    "Л": ["Леонид", "Лев", "Лука"], "М": ["Михаил", "Максим", "Марк"],
    "Н": ["Николай", "Никита", "Назар"], "О": ["Олег", "Остап", "Оскар"],
    "П": ["Павел", "Петр", "Платон"], "Р": ["Роман", "Руслан", "Ростислав"],
    "С": ["Сергей", "Степан", "Семен"], "Т": ["Тимур", "Тимофей", "Тихон"],
    "У": ["Устин", "Ульян", "Умар"], "Ф": ["Федор", "Филипп", "Фарид"],
    "Х": ["Харитон", "Хасан", "Хасбулат"], "Ц": ["Цезарь", "Царь", "Цыган"],
    "Ч": ["Чеслав", "Черняк", "Чагатай"], "Ш": ["Шамиль", "Шарип", "Шатун"],
    "Щ": ["Щукин", "Щербак", "Щерба"], "Э": ["Эдуард", "Эмиль", "Эльдар"],
    "Ю": ["Юрий", "Юсуф", "Юлиан"], "Я": ["Яков", "Ярослав", "Ян"],
}

PATRONYMICS_BY_LETTER: dict[str, list[str]] = {
    "А": ["Алексеевич", "Андреевич", "Артемович"], "Б": ["Борисович", "Богданович", "Булатович"],
    "В": ["Викторович", "Владимирович", "Валерьевич"], "Г": ["Григорьевич", "Георгиевич", "Глебович"],
    "Д": ["Дмитриевич", "Данилович", "Денисович"], "Е": ["Евгеньевич", "Егорович", "Елисеевич"],
    "Ж": ["Жоресович", "Ждановна", "Жановна"], "З": ["Захарович", "Зиновьевич", "Зиядович"],
    "И": ["Иванович", "Игоревич", "Ильич"], "К": ["Кириллович", "Константинович", "Климович"],
    "Л": ["Леонидович", "Львович", "Лукич"], "М": ["Михайлович", "Максимович", "Маркович"],
    "Н": ["Николаевич", "Никитич", "Назарович"], "О": ["Олегович", "Остапович", "Оскарович"],
    "П": ["Павлович", "Петрович", "Платонович"], "Р": ["Романович", "Русланович", "Ростиславович"],
    "С": ["Сергеевич", "Степанович", "Семенович"], "Т": ["Тимурович", "Тимофеевич", "Тихонович"],
    "У": ["Устинович", "Ульянович", "Умарович"], "Ф": ["Федорович", "Филиппович", "Фаридович"],
    "Х": ["Харитонович", "Хасанович", "Хамидович"], "Ц": ["Цезаревич", "Царевич", "Цыганович"],
    "Ч": ["Чеславович", "Черняевич", "Чаадаевич"], "Ш": ["Шамильевич", "Шариповна", "Шатунович"],
    "Щ": ["Щукинович", "Щербакович", "Щегловна"], "Э": ["Эдуардович", "Эмильевич", "Эльдарович"],
    "Ю": ["Юрьевич", "Юсуфович", "Юлианович"], "Я": ["Яковлевич", "Ярославович", "Янович"],
}

ADDRESS_DATABASE: list[dict[str, str]] = [
    {"индекс": "101000", "регион": "Московская", "город": "Москва", "улица": "Мясницкая", "дом": "15", "кв": "42"},
    {"индекс": "190000", "регион": "Ленинградская", "город": "Санкт-Петербург", "улица": "Невский проспект", "дом": "28", "кв": "15"},
    {"индекс": "620000", "регион": "Свердловская", "город": "Екатеринбург", "улица": "Ленина", "дом": "24А", "кв": "89"},
    {"индекс": "630099", "регион": "Новосибирская", "город": "Новосибирск", "улица": "Красный проспект", "дом": "65", "кв": "33"},
    {"индекс": "420015", "регион": "Татарстан", "город": "Казань", "улица": "Баумана", "дом": "36", "кв": "7"},
    {"индекс": "603005", "регион": "Нижегородская", "город": "Нижний Новгород", "улица": "Большая Покровская", "дом": "18", "кв": "56"},
    {"индекс": "443010", "регион": "Самарская", "город": "Самара", "улица": "Куйбышева", "дом": "95", "кв": "21"},
    {"индекс": "644099", "регион": "Омская", "город": "Омск", "улица": "Ленина", "дом": "12", "кв": "88"},
    {"индекс": "454091", "регион": "Челябинская", "город": "Челябинск", "улица": "Кирова", "дом": "167", "кв": "14"},
    {"индекс": "344006", "регион": "Ростовская", "город": "Ростов-на-Дону", "улица": "Большая Садовая", "дом": "47", "кв": "31"},
    {"индекс": "450076", "регион": "Башкортостан", "город": "Уфа", "улица": "Ленина", "дом": "14", "кв": "62"},
    {"индекс": "400005", "регион": "Волгоградская", "город": "Волгоград", "улица": "Мира", "дом": "22", "кв": "9"},
    {"индекс": "614000", "регион": "Пермская", "город": "Пермь", "улица": "Ленина", "дом": "51", "кв": "73"},
    {"индекс": "350000", "регион": "Краснодарская", "город": "Краснодар", "улица": "Красная", "дом": "109", "кв": "45"},
    {"индекс": "410000", "регион": "Саратовская", "город": "Саратов", "улица": "Московская", "дом": "34", "кв": "18"},
    {"индекс": "394018", "регион": "Воронежская", "город": "Воронеж", "улица": "Плехановская", "дом": "53", "кв": "27"},
    {"индекс": "300041", "регион": "Тульская", "город": "Тула", "улица": "Советская", "дом": "17", "кв": "5"},
    {"индекс": "236000", "регион": "Калининградская", "город": "Калининград", "улица": "Ленинский проспект", "дом": "83", "кв": "11"},
    {"индекс": "238750", "регион": "Калининградская", "город": "Советск", "улица": "Каштановая", "дом": "8В", "кв": "78"},
    {"индекс": "238340", "регион": "Калининградская", "город": "Светлый", "улица": "Калининградская", "дом": "2А", "кв": "34"},
    {"индекс": "238401", "регион": "Калининградская", "город": "Славск", "улица": "Советская", "дом": "12В", "кв": "56"},
    {"индекс": "660049", "регион": "Красноярская", "город": "Красноярск", "улица": "Мира", "дом": "94", "кв": "37"},
    {"индекс": "680000", "регион": "Хабаровская", "город": "Хабаровск", "улица": "Муравьева-Амурского", "дом": "15", "кв": "8"},
    {"индекс": "690091", "регион": "Приморская", "город": "Владивосток", "улица": "Светланская", "дом": "29", "кв": "65"},
    {"индекс": "664003", "регион": "Иркутская", "город": "Иркутск", "улица": "Карла Маркса", "дом": "13", "кв": "19"},
    {"индекс": "170100", "регион": "Тверская", "город": "Тверь", "улица": "Советская", "дом": "23", "кв": "41"},
    {"индекс": "150000", "регион": "Ярославская", "город": "Ярославль", "улица": "Свободы", "дом": "46", "кв": "12"},
    {"индекс": "160000", "регион": "Вологодская", "город": "Вологда", "улица": "Мира", "дом": "6", "кв": "3"},
    {"индекс": "214000", "регион": "Смоленская", "город": "Смоленск", "улица": "Ленина", "дом": "10", "кв": "22"},
    {"индекс": "460000", "регион": "Оренбургская", "город": "Оренбург", "улица": "Советская", "дом": "32", "кв": "57"},
    {"индекс": "440000", "регион": "Пензенская", "город": "Пенза", "улица": "Московская", "дом": "78", "кв": "4"},
    {"индекс": "432017", "регион": "Ульяновская", "город": "Ульяновск", "улица": "Гончарова", "дом": "40", "кв": "16"},
    {"индекс": "410031", "регион": "Саратовская", "город": "Балаково", "улица": "Набережная", "дом": "21", "кв": "93"},
    {"индекс": "305000", "регион": "Курская", "город": "Курск", "улица": "Ленина", "дом": "11", "кв": "44"},
    {"индекс": "360000", "регион": "Кабардино-Балкарская", "город": "Нальчик", "улица": "Ленина", "дом": "27", "кв": "8"},
    {"индекс": "185035", "регион": "Карельская", "город": "Петрозаводск", "улица": "Ленина", "дом": "33", "кв": "71"},
    {"индекс": "248000", "регион": "Калужская", "город": "Калуга", "улица": "Кирова", "дом": "55", "кв": "29"},
    {"индекс": "241050", "регион": "Брянская", "город": "Брянск", "улица": "Ленина", "дом": "19", "кв": "63"},
    {"индекс": "153000", "регион": "Ивановская", "город": "Иваново", "улица": "Ленина", "дом": "47", "кв": "10"},
    {"индекс": "600000", "регион": "Владимирская", "город": "Владимир", "улица": "Большая Московская", "дом": "56", "кв": "38"},
    {"индекс": "390000", "регион": "Рязанская", "город": "Рязань", "улица": "Почтовая", "дом": "36", "кв": "51"},
    {"индекс": "428000", "регион": "Чувашская", "город": "Чебоксары", "улица": "Ленинградская", "дом": "17", "кв": "24"},
    {"индекс": "362040", "регион": "Северо-Осетинская", "город": "Владикавказ", "улица": "Мира", "дом": "8", "кв": "35"},
]

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

    l_s = parts[0][0].upper() if parts[0] else "И"
    l_n = parts[1][0].upper() if len(parts) > 1 and parts[1] else "И"
    l_p = parts[2][0].upper() if len(parts) > 2 and parts[2] else "И"

    surnames = SURNAMES_BY_LETTER.get(l_s, ["Иванов"])
    names = NAMES_BY_LETTER.get(l_n, ["Иван"])
    patronymics = PATRONYMICS_BY_LETTER.get(l_p, ["Иванович"])

    candidates: list[str] = []
    for s in surnames:
        for n in names:
            for p in patronymics:
                candidates.append(f"{s} {n} {p}")

    if available_chars:
        def fits(text: str) -> bool:
            return all(ch in available_chars or ch.isspace() for ch in text)
        filtered = [c for c in candidates if fits(c)]
        if filtered:
            return filtered[:3]
        for sl in "ИПВМКСАТНГ":
            for nl in "ИПВМКСАТНГ":
                for s in SURNAMES_BY_LETTER.get(sl, []):
                    for n in NAMES_BY_LETTER.get(nl, []):
                        for p in PATRONYMICS_BY_LETTER.get(nl, []):
                            cand = f"{s} {n} {p}"
                            if fits(cand):
                                filtered.append(cand)
                                if len(filtered) >= 3:
                                    return filtered
        return filtered[:3]
    return candidates[:3]


def _format_alfa_address(entry: dict[str, str]) -> str:
    """Format an ADDRESS_DATABASE entry into Alfa statement address format."""
    return (
        f"{entry['индекс']}, РОССИЯ,\n"
        f"{entry['регион']} область,\n"
        f"ОБЛАСТЬ {entry['регион']},\n"
        f"{entry['город']}, УЛИЦА {entry['улица']}, д.\n"
        f"{entry['дом']}, кв. {entry['кв']}"
    )


def _format_vtb_address(entry: dict[str, str]) -> str:
    """Format an ADDRESS_DATABASE entry for VTB statement style (single line)."""
    return f"{entry['индекс']}, РОССИЯ, {entry['регион']} обл., {entry['город']}, ул. {entry['улица']}, д. {entry['дом']}, кв. {entry['кв']}"


def suggest_address(current_address: str, available_chars: set[str] | None = None, bank: str = "alfa") -> list[str]:
    """Suggest replacement addresses from the real address database."""
    city_m = re.search(r"ОБЛАСТЬ\s+\S+,\s*\n?\s*(\S+)", current_address)
    if not city_m:
        city_m = re.search(r",\s*([А-ЯЁ][а-яё]+(?:-[а-яё]+)?(?:-[А-ЯЁа-яё]+)?)\s*,", current_address)
    old_city = city_m.group(1).rstrip(",") if city_m else ""
    first_letter = old_city[0].upper() if old_city else "М"

    fmt = _format_alfa_address if bank == "alfa" else _format_vtb_address

    by_letter = [e for e in ADDRESS_DATABASE if e["город"][0].upper() == first_letter]
    rest = [e for e in ADDRESS_DATABASE if e["город"][0].upper() != first_letter]

    candidates: list[str] = []
    for entry in by_letter + rest:
        addr = fmt(entry)
        candidates.append(addr)
        if len(candidates) >= 6:
            break

    if available_chars:
        safe = set("0123456789,.\n ")
        def fits(t: str) -> bool:
            return all(ch in available_chars or ch in safe for ch in t)
        filtered = [c for c in candidates if fits(c)]
        if filtered:
            return filtered[:3]
        for entry in ADDRESS_DATABASE:
            addr = fmt(entry)
            if fits(addr):
                filtered.append(addr)
                if len(filtered) >= 3:
                    return filtered
        return filtered[:3]
    return candidates[:3]


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
    """Build (old_value, new_value) pairs from wizard changes for CID patching.

    Deduplicates by old_value to prevent conflicting replacements when
    multiple fields share the same original text (e.g., same date in
    period_start, period_end, and operation_date).
    """
    changes = state.get("changes", {})
    current = state.get("current_values", {})
    seen_old: dict[str, str] = {}
    for key, new_val in changes.items():
        old_val = current.get(key)
        if old_val and old_val != new_val:
            old_s = str(old_val)
            new_s = str(new_val)
            if old_s not in seen_old:
                seen_old[old_s] = new_s
    return list(seen_old.items())
