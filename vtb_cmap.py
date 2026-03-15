#!/usr/bin/env python3
"""CMap ВТБ: кириллица и цифры → CID hex (4 символа).

Используется для кодирования произвольного текста в content stream.
Источник: 09-03-26_03-47.pdf, ToUnicode.
"""
import unicodedata

# Цифры и символы (date, amount, номер счета *9483)
_CID_DIGIT = {
    "0": "0013", "1": "0014", "2": "0015", "3": "0016", "4": "0017",
    "5": "0018", "6": "0019", "7": "001A", "8": "001B", "9": "001C",
    ".": "0011", ",": "000F", " ": "0003", ":": "001D",
    "(": "000B", ")": "000C", "+": "000E", "-": "0010", "*": "002A",  # U+002A asterisk
}
# Рубль (в amount)
_CID_RUBLE = "0440"  # ₽

# Кириллица (заглавные и строчные) — ВТБ font subset
_CID_CYRILLIC = {
    "А": "021C", "Б": "021D", "В": "023E", "Г": "023F", "Д": "0240",
    "Е": "0221", "Ж": "0222", "З": "0243", "И": "0244", "Й": "0245",
    # К/Л/М/Р uppercase: GID=0 в базовом шрифте — slot-replacement даёт донорский глиф
    "К": "0226", "Л": "0227", "М": "0228", "Н": "0249", "О": "024A",
    "П": "022B", "Р": "022C", "С": "022D", "Т": "022E", "У": "024F",
    # Ф=0224 (есть в шрифте), Х=0251 (исправлено: было 0231→GID0), Ц=0252 (было 0247=л!)
    "Ф": "0224", "Х": "0251", "Ц": "0252",
    "Ч": "0253", "Ш": "0254", "Щ": "0255", "Ъ": "0256", "Ы": "0257",
    "Ь": "0258", "Э": "0242", "Ю": "023A", "Я": "025B",
    "а": "023C", "б": "021D", "в": "023E", "г": "023F", "д": "0240",  # б→Б CID (нет б-глифа)
    "е": "0241", "ж": "0223", "з": "0243", "и": "0244", "й": "0245",
    "к": "0246", "л": "0247", "м": "0248", "н": "0249", "о": "024A",
    "п": "022B", "р": "024C", "с": "024D", "т": "024E", "у": "024F",
    # ф=0250 (исправлено: было 0243→GID0), х=0251 (было 0231→GID0), ц=0252 (было 0247=л)
    "ф": "0250", "х": "0251", "ц": "0252", "ч": "0253", "ш": "0254",
    "щ": "0255", "ъ": "0256", "ы": "0257", "ь": "0258",
    "э": "0242", "ю": "023A", "я": "025B",
}
# Некоторые символы могут отсутствовать — homoglyph (замена при вводе)
_FALLBACK = {"ё": "е", "Ё": "Е", "‑": "-"}  # U+2011 non‑breaking hyphen
FALLBACK_TIPS = dict(_FALLBACK)  # публично для подсказок в боте

# Единственный источник истины: символы, которые шрифт ВТБ поддерживает напрямую
_SUPPORTED_CHARS = frozenset(_CID_DIGIT) | frozenset(_CID_CYRILLIC) | {"₽"}

# Латинские буквы (частые при копировании) — шрифт не поддерживает
_LATIN_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_LATIN_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_LATIN_LETTERS = _LATIN_UPPER | _LATIN_LOWER

# Другие частые неподдерживаемые символы
_OTHER_UNSUPPORTED = {
    "\u00a0",   # no-break space
    "\u2010",   # hyphen
    "\u2011",   # non-breaking hyphen (same as ‑ in _FALLBACK)
    "\u2012",   # figure dash
    "\u2013",   # en dash –
    "\u2014",   # em dash —
    "\u2015",   # horizontal bar
    "\u2212",   # minus −
    "\u200b",   # zero-width space
    "\u200c",   # zero-width non-joiner
    "\u200d",   # zero-width joiner
    "\ufeff",   # BOM / zero-width no-break
}


def get_unsupported_chars(text: str) -> list[str]:
    """Вернуть список неподдерживаемых символов (порядок первого появления).
    Пустой список = всё ок. ё/Ё/‑ — нужна замена на е/Е/-."""
    if not isinstance(text, str):
        return []
    text = unicodedata.normalize("NFC", text)
    seen: set[str] = set()
    bad: list[str] = []
    for c in text:
        if c in seen:
            continue
        if c in _SUPPORTED_CHARS:
            continue
        if c in _FALLBACK:
            seen.add(c)
            bad.append(c)
            continue
        if c in _LATIN_LETTERS:
            seen.add(c)
            bad.append(c)
            continue
        if c in _OTHER_UNSUPPORTED:
            seen.add(c)
            bad.append(c)
            continue
        # Любой другой символ — неподдерживаемый
        seen.add(c)
        bad.append(c)
    return bad


def is_text_supported(text: str) -> bool:
    """True, если весь текст можно закодировать (с учётом FALLBACK-замены в text_to_cids)."""
    return len(get_unsupported_chars(text)) == 0


def format_unsupported_error(unsupported: list[str], field_name: str = "") -> str:
    """Сообщение об ошибке с подсказкой замены."""
    if not unsupported:
        return ""
    pre = f"В поле «{field_name}» " if field_name else ""
    chars = ", ".join(f"«{c}»" for c in unsupported)
    tips = []
    has_latin = False
    others = []
    for c in unsupported:
        if c in _FALLBACK:
            tips.append(f"«{c}» → «{_FALLBACK[c]}»")
        elif c in _LATIN_LETTERS:
            has_latin = True
        else:
            others.append(c)
    parts = []
    if tips:
        parts.append(f"Используйте замену: {'; '.join(tips)}")
    if has_latin:
        parts.append("Латинские буквы (a-z, A-Z) не поддерживаются — введите кириллицу.")
    if others:
        parts.append(f"Символы {', '.join(repr(c) for c in others)} не поддерживаются.")
    return f"❌ {pre}найдены неподдерживаемые буквы: {chars}.\n" + " ".join(parts)


def suggest_replacement(text: str) -> str | None:
    """Вернуть вариант текста с автозаменой ё→е, Ё→Е, ‑→-. None если есть другие неподдерживаемые символы."""
    result = []
    for c in text:
        if c in _FALLBACK:
            result.append(_FALLBACK[c])
        elif c in _CID_DIGIT or c in _CID_CYRILLIC or c == "₽":
            result.append(c)
        else:
            return None
    return "".join(result)


# Latin letters для ID операции (визуальные аналоги кириллицей)
# A=А, B=Б, C=С, T=Т, R=Р(строчн.), K=К(строчн.)
_CID_OPID = {
    "A": "021C",  # А (выглядит как A)
    "B": "021D",  # Б (выглядит как B)
    "C": "022D",  # С (выглядит как C)
    "T": "022E",  # Т (CID 022E → U+0422 uppercase, GID=35)
    "R": "024C",  # р строчн. (выглядит как р/P)
    "K": "0246",  # к строчн. (GID 43, выглядит как k/К)
    "P": "024C",  # р строчн. (GID 49)
    "H": "0249",  # Н (выглядит как H)
}

# Маппинг банков → Latin-буква для позиции bank_letter в Operation ID
_BANK_LETTER_MAP = {
    "Т-Банк": "T", "Тинькофф": "T", "Tinkoff": "T",
    "Сбербанк": "C", "Сбер": "C",
    "Альфа-Банк": "A", "Альфа": "A", "АльфаБанк": "A",
    "ВТБ": "B", "Банк ВТБ": "B",
    "Райффайзен": "R", "Райффайзенбанк": "R",
}


def operation_id_to_cids(text: str) -> list[str] | None:
    """ID операции (B606...) → CID. Поддерживает 0-9, A-F (hex)."""
    result = []
    for c in text.upper():
        cid = _CID_DIGIT.get(c) or _CID_OPID.get(c)
        if cid is None:
            return None
        result.append(cid)
    return result


def text_to_cids(text: str) -> list[str] | None:
    """Текст → список CID hex. None если символ не найден."""
    result = []
    for c in text:
        c = _FALLBACK.get(c, c)
        cid = _CID_DIGIT.get(c) or _CID_CYRILLIC.get(c)
        if c == "₽":
            cid = _CID_RUBLE
        if cid is None:
            return None
        result.append(cid)
    return result


def format_amount(amount: int) -> str:
    """10000 → '10 000 ₽'."""
    parts = []
    s = str(amount)
    for i, c in enumerate(reversed(s)):
        if i and i % 3 == 0:
            parts.append(" ")
        parts.append(c)
    return "".join(reversed(parts)) + " ₽"


def gen_sbp_operation_id(
    op_date: "date | None" = None,
    op_time_moscow: str = "",
    direction: str = "A",
    bank_code: str = "60",
    recipient_bank: str = "",
) -> str:
    """Сгенерировать SBP Operation ID по структуре реальных ID ВТБ.

    Формат (из анализа 30+ реальных ID, все 32 символа):
      A606711136304180K0000080011700501 — НЕВЕРНО (примеры ниже)

      direction(1) + bank(2) + julian(2) + hhmm(4)  ← prefix, 9 символов
      + routing_digits(6)                            ← случайные 6 цифр
      + bank_letter(1)                               ← код банка-получателя (цифра или буква)
      + "000NNN001"(9)                               ← счётчик (NNN = 001..017)
      + "1700501"(7)                                 ← постоянный суффикс ВТБ
      = 9 + 6 + 1 + 9 + 7 = 32 символа

    Время в ID = московское время − 2ч (UTC+1 / Калининград)
    Дата (julian) = день года по часовому поясу UTC+1

    Аргументы:
      op_date         — дата операции (Moscow, default: сегодня)
      op_time_moscow  — время по Москве "HH:MM" (default: текущее московское)
      direction       — 'A' (исходящий) или 'B' (входящий)
      bank_code       — код банка-отправителя (60 = ВТБ)
      recipient_bank  — название банка-получателя (Т-Банк, Сбербанк и т.д.)
    """
    import random
    from datetime import date as _date, datetime as _datetime, timezone as _tz, timedelta as _td

    # Московское время → UTC+1 (Калининград, −2ч от Москвы)
    if op_date is None:
        now_msk = _datetime.now(_tz.utc) + _td(hours=3)
        op_date = now_msk.date()
        if not op_time_moscow:
            op_time_moscow = now_msk.strftime("%H:%M")

    if not op_time_moscow:
        now_msk = _datetime.now(_tz.utc) + _td(hours=3)
        op_time_moscow = now_msk.strftime("%H:%M")

    # Конвертируем московское время в UTC+1 (Калининград = MSK−2)
    msk_h, msk_m = int(op_time_moscow[:2]), int(op_time_moscow[3:5])
    kal_total = msk_h * 60 + msk_m - 120  # −2 часа
    # Учитываем переход через полночь
    kal_date = op_date
    if kal_total < 0:
        kal_total += 1440
        kal_date = op_date - _td(days=1)
    elif kal_total >= 1440:
        kal_total -= 1440
        kal_date = op_date + _td(days=1)
    kal_h, kal_m = divmod(kal_total, 60)

    julian = kal_date.timetuple().tm_yday   # 1-366, 1-indexed
    hhmm = f"{kal_h:02d}{kal_m:02d}"        # "HHMM" в UTC+1

    # Буква банка-получателя (или "0" если неизвестен)
    bank_letter = "0"
    for name, letter in _BANK_LETTER_MAP.items():
        if name.lower() in recipient_bank.lower() or recipient_bank.lower() in name.lower():
            bank_letter = letter
            break

    routing = "".join(random.choices("0123456789", k=6))          # 6 случайных цифр
    nnn = random.randint(1, 17)
    counter = f"000{nnn:03d}001"                                    # "000001001".."000017001"

    prefix = f"{direction}{bank_code}{julian:02d}{hhmm}"           # 9 символов
    # Итого 25 символов — "1700501" уже есть в PDF как отдельная строка, не добавляем
    return prefix + routing + bank_letter + counter                 # 9+6+1+9=25
