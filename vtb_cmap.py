#!/usr/bin/env python3
"""CMap ВТБ: кириллица и цифры → CID hex (4 символа).

Используется для кодирования произвольного текста в content stream.
Источник: 09-03-26_03-47.pdf, ToUnicode.
"""
import unicodedata

# Цифры и символы (date, amount)
_CID_DIGIT = {
    "0": "0013", "1": "0014", "2": "0015", "3": "0016", "4": "0017",
    "5": "0018", "6": "0019", "7": "001A", "8": "001B", "9": "001C",
    ".": "0011", ",": "000F", " ": "0003", ":": "001D",
    "(": "000B", ")": "000C", "+": "000E", "-": "0010",
}
# Рубль (в amount)
_CID_RUBLE = "0440"  # ₽

# Кириллица (заглавные и строчные) — ВТБ font subset
_CID_CYRILLIC = {
    "А": "021C", "Б": "021D", "В": "023E", "Г": "023F", "Д": "0240",
    "Е": "0221", "Ж": "0222", "З": "0243", "И": "0244", "Й": "0245",
    "К": "0226", "Л": "0227", "М": "0228", "Н": "0249", "О": "024A",
    "П": "022B", "Р": "022C", "С": "022D", "Т": "024E", "У": "024F",
    "Ф": "0224", "Х": "0231", "Ц": "0247",  # Ц=0247 (часто как л)
    "Ч": "0253", "Ш": "0254", "Щ": "0255", "Ъ": "0256", "Ы": "0257",
    "Ь": "0258", "Э": "0242", "Ю": "023A", "Я": "025B",
    "а": "023C", "б": "023D", "в": "023E", "г": "023F", "д": "0240",
    "е": "0241", "ж": "0223", "з": "0243", "и": "0244", "й": "0245",
    "к": "0246", "л": "0247", "м": "0248", "н": "0249", "о": "024A",
    "п": "022B", "р": "024C", "с": "024D", "т": "024E", "у": "024F",
    "ф": "0243", "х": "0231", "ц": "0247", "ч": "0253", "ш": "0254",
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
