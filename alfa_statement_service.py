#!/usr/bin/env python3
"""Сервис для создания/редактирования выписок Альфа-Банка.

Блоковая структура:
  Блок 1 (Операции): транзакции — суммы, телефон, код, описание
  Блок 2 (Сводка): входящий/исходящий остатки, поступления, расходы
  Блок 3 (Реквизиты): номер счёта, ФИО клиента, адрес
"""
from __future__ import annotations

import re
import zlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
BASE_PDF = BASE_DIR / "AM_17737975768134.pdf"

# ── Current template values (from AM_17737975768134.pdf) ──────────────────

BLOCK2_DEFAULTS = {
    "входящий_остаток": "94,82",
    "поступления": "10 000,00",
    "расходы": "8 000,00",
    "исходящий_остаток": "2 094,82",
    "платежный_лимит": "2 094,82",
    "текущий_баланс": "2 094,82",
}

BLOCK3_DEFAULTS = {
    "номер_счета": "40817810280480002476",
    "клиент_имя": "Жарков Ефим",
    "клиент_отчество": "Ееннадьевич",
    "индекс": "238401",
    "город": "Славск",
    "дом_кв": "12В, кв. 56",
}

BLOCK1_DEFAULTS = {
    "код_операции_расход": "C161803260049982",
    "телефон": "+7 (922) 544-15-",
    "телефон_окончание": "33",
    "сумма_расход": "8 000,00",
    "код_операции_приход": "B011803260041725",
    "получатель_сокр": "Жарков Е. Е.",
    "сумма_приход": "10 000,00",
}

BLOCK_LABELS = {
    "входящий_остаток": "Входящий остаток",
    "поступления": "Поступления",
    "расходы": "Расходы",
    "исходящий_остаток": "Исходящий остаток",
    "платежный_лимит": "Платежный лимит",
    "текущий_баланс": "Текущий баланс",
    "номер_счета": "Номер счета",
    "клиент_имя": "Клиент (Имя Фамилия)",
    "клиент_отчество": "Отчество",
    "индекс": "Индекс",
    "город": "Город",
    "дом_кв": "Дом, квартира",
    "код_операции_расход": "Код операции (расход)",
    "телефон": "Телефон",
    "телефон_окончание": "Окончание телефона",
    "сумма_расход": "Сумма расхода",
    "код_операции_приход": "Код операции (приход)",
    "получатель_сокр": "Получатель (сокр.)",
    "сумма_приход": "Сумма прихода",
}

# Tm y-coordinates for amount positions in the template (from content stream)
_AMOUNT_TM_Y = {
    "входящий_остаток": 662.497,
    "поступления": 645.447,
    "расходы": 628.397,
    "исходящий_остаток": 611.347,
    "платежный_лимит": 594.297,
    "текущий_баланс": 537.497,
    "сумма_расход_tx": 427.104,
    "сумма_приход_tx": 403.006,
}

# Right edges for amount columns
_RIGHT_EDGE_HEADER = 566.979
_RIGHT_EDGE_TX = 568.028

# Current Tm x positions in the template (AM_17737975768134.pdf)
_ORIG_TM_X = {
    662.497: 527.427,
    645.447: 511.865,
    628.397: 516.314,
    611.347: 516.314,
    594.297: 516.314,
    537.497: 516.314,
    427.104: 514.700,
    403.006: 512.914,
}


def _import_cid():
    """Lazy-import cid_patch_amount utilities."""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from cid_patch_amount import (
        patch_replacements,
        _parse_tounicode,
        _extend_tounicode_identity,
        _encode_cid,
    )
    return patch_replacements, _parse_tounicode, _extend_tounicode_identity, _encode_cid


# ── Font metrics ──────────────────────────────────────────────────────────

def _extract_font_widths(pdf_bytes: bytes) -> dict[int, int]:
    """Extract CID→width mapping from first /W array (Font F1)."""
    pattern = rb'/W\s*\['
    for m in re.finditer(pattern, pdf_bytes):
        depth = 0
        for i in range(m.end() - 1, min(m.end() + 5000, len(pdf_bytes))):
            if pdf_bytes[i:i+1] == b'[':
                depth += 1
            elif pdf_bytes[i:i+1] == b']':
                depth -= 1
                if depth == 0:
                    w_str = pdf_bytes[m.end():i].decode('latin1')
                    tokens = []
                    for t in re.finditer(r'\[([^\]]*)\]|(\d+)', w_str):
                        if t.group(1) is not None:
                            tokens.append(('arr', [float(x) for x in t.group(1).split()]))
                        else:
                            tokens.append(('num', int(t.group(2))))
                    widths = {}
                    j = 0
                    while j < len(tokens):
                        if tokens[j][0] == 'num':
                            if j + 1 < len(tokens) and tokens[j+1][0] == 'arr':
                                cid = tokens[j][1]
                                for k, w in enumerate(tokens[j+1][1]):
                                    widths[cid + k] = int(w)
                                j += 2
                            else:
                                j += 1
                        else:
                            j += 1
                    return widths
                    break
    return {}


def _text_width_pt(text: str, uni_to_cid: dict, cid_widths: dict, font_size: float = 8.0) -> float:
    """Calculate text width in points."""
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0x20 and 0x20 not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        cid_hex = uni_to_cid.get(cp)
        if cid_hex:
            cid_num = int(cid_hex, 16)
            total += cid_widths.get(cid_num, 556)
        else:
            total += 556
    return total * font_size / 1000


def _calc_tm_x(text: str, right_edge: float, uni_to_cid: dict, cid_widths: dict) -> float:
    """Calculate Tm x for right-aligned text."""
    w = _text_width_pt(text, uni_to_cid, cid_widths, 8.0)
    return round(right_edge - w, 3)


# ── Character validation ─────────────────────────────────────────────────

def get_available_chars(pdf_path: Path | None = None) -> set[str]:
    """Get characters available in the PDF's fonts."""
    path = pdf_path or BASE_PDF
    if not path.exists():
        return set()
    _, _parse_tounicode, _, _ = _import_cid()
    data = path.read_bytes()
    uni_to_cid = _parse_tounicode(data)
    return {chr(cp) for cp in uni_to_cid if 0 < cp < 0x110000}


def validate_text(text: str, pdf_path: Path | None = None) -> list[str]:
    """Return list of characters in text that are NOT available in the font."""
    available = get_available_chars(pdf_path)
    missing = []
    seen = set()
    for ch in text:
        if ch not in available and not ch.isspace() and ch not in seen:
            missing.append(ch)
            seen.add(ch)
    return missing


# ── Auto-recalculate balances ─────────────────────────────────────────────

def recalc_balances(
    входящий: float,
    поступления: float,
    расходы: float,
) -> dict[str, str]:
    """Recalculate derived amounts and return all Block 2 values."""
    исходящий = входящий + поступления - расходы
    return {
        "входящий_остаток": _fmt_amount(входящий),
        "поступления": _fmt_amount(поступления),
        "расходы": _fmt_amount(расходы),
        "исходящий_остаток": _fmt_amount(исходящий),
        "платежный_лимит": _fmt_amount(исходящий),
        "текущий_баланс": _fmt_amount(исходящий),
    }


def _fmt_amount(val: float) -> str:
    """Format: 7 000,00 or 94,82"""
    if val == int(val) and val >= 1:
        s = f"{int(val):,}".replace(",", " ") + ",00"
    else:
        integer = int(val)
        frac = round((val - integer) * 100)
        int_str = f"{integer:,}".replace(",", " ") if integer >= 1000 else str(integer)
        s = f"{int_str},{frac:02d}"
    return s


def parse_amount(s: str) -> float | None:
    """Parse '7 000,00' or '94,82' or '10000' → float."""
    s = s.strip().replace(" ", "").replace("\u00a0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ── Extract data from check PDF ──────────────────────────────────────────

def extract_from_check(check_path: Path) -> dict[str, str]:
    """Extract fields from a check PDF for auto-filling the statement."""
    try:
        from receipt_extractor import extract_from_receipt
    except ImportError:
        return {}
    data = extract_from_receipt(check_path)
    result = {}
    if data.get("amount"):
        amt = float(data["amount"])
        result["сумма_приход"] = _fmt_amount(amt)
        result["сумма_расход"] = _fmt_amount(amt)
        result["поступления"] = _fmt_amount(amt)
        result["расходы"] = _fmt_amount(amt)
    if data.get("phone_recipient"):
        phone = data["phone_recipient"]
        if len(phone) >= 11:
            parts = re.match(r'(\+7\s*\(\d{3}\)\s*\d{3}-\d{2}-)', phone)
            if parts:
                result["телефон"] = parts.group(1)
                rest = phone[len(parts.group(1)):]
                if rest:
                    result["телефон_окончание"] = rest[:2]
    if data.get("fio_recipient"):
        fio = data["fio_recipient"]
        parts = fio.split()
        if len(parts) >= 2:
            result["клиент_имя"] = f"{parts[0]} {parts[1]}"
            initials = f"{parts[0][0]}. {parts[1][0]}."
            if len(parts) >= 3:
                result["клиент_отчество"] = parts[2]
                initials = f"{parts[0][0]}. {parts[2][0]}."
            result["получатель_сокр"] = f"{parts[0]} {initials}"
    if data.get("operation_id"):
        result["код_операции_расход"] = data["operation_id"]
    return result


# ── Main patch function ──────────────────────────────────────────────────

def patch_alfa_statement(
    changes: dict[str, str],
    output_path: Path,
    base_pdf: Path | None = None,
) -> tuple[bool, str]:
    """
    Apply changes to the Alfa Bank statement template.

    changes: dict mapping field keys to new values.
    Returns (success, error_message).
    """
    base = base_pdf or BASE_PDF
    if not base.exists():
        return False, f"Шаблон не найден: {base}"

    patch_replacements, _parse_tounicode, _extend_tounicode_identity, _encode_cid = _import_cid()

    # Merge defaults with changes
    all_fields = {}
    all_fields.update(BLOCK2_DEFAULTS)
    all_fields.update(BLOCK3_DEFAULTS)
    all_fields.update(BLOCK1_DEFAULTS)
    current = dict(all_fields)

    # Build CID replacement pairs: (old_text, new_text)
    phase1_replacements = []
    phase2_substring = []
    changed_amounts = {}

    for key, new_val in changes.items():
        old_val = current.get(key)
        if old_val is None or new_val == old_val:
            continue

        if key in ("входящий_остаток", "поступления", "расходы",
                    "исходящий_остаток", "платежный_лимит", "текущий_баланс"):
            old_rur = f"{old_val} RUR"
            new_rur = f"{new_val} RUR"
            phase1_replacements.append((old_rur, new_rur))
            changed_amounts[key] = new_val

        elif key == "сумма_расход":
            old_rur = f"-{old_val} RUR"
            new_rur = f"-{new_val} RUR"
            phase1_replacements.append((old_rur, new_rur))
            changed_amounts["сумма_расход_tx"] = new_val

        elif key == "сумма_приход":
            changed_amounts["сумма_приход_tx"] = new_val

        elif key == "телефон":
            phase2_substring.append((current["телефон"], new_val))

        elif key == "телефон_окончание":
            old_full = f"{current['телефон_окончание']}. Без НДС."
            new_full = f"{new_val}. Без НДС."
            phase1_replacements.append((old_full, new_full))

        elif key == "код_операции_расход":
            phase1_replacements.append((current[key], new_val))
            phase2_substring.append((current[key], new_val))

        elif key == "получатель_сокр":
            phase2_substring.append((current[key], new_val))

        elif key in ("номер_счета", "клиент_имя", "клиент_отчество",
                      "индекс", "город", "дом_кв", "код_операции_приход"):
            if key == "индекс":
                old_full = f"{current['индекс']}, РОССИЯ,"
                new_full = f"{new_val}, РОССИЯ,"
                phase1_replacements.append((old_full, new_full))
            elif key == "город":
                old_full = f"{current['город']}, УЛИЦА Каштановая, д."
                new_full = f"{new_val}, УЛИЦА Каштановая, д."
                phase1_replacements.append((old_full, new_full))
            else:
                phase1_replacements.append((current[key], new_val))

    # Deduplicate: longer patterns first to avoid partial matches
    phase1_replacements = list(dict.fromkeys(phase1_replacements))
    phase1_replacements.sort(key=lambda x: len(x[0]), reverse=True)

    # Handle bold transaction amount (F3 font)
    bold_amount_changed = "сумма_приход" in changes and changes["сумма_приход"] != current["сумма_приход"]

    # ── Execute patching ──────────────────────────────────────────────
    tmp = Path(tempfile.mktemp(suffix=".pdf"))

    try:
        # Phase 1: full hex CID replacements
        if phase1_replacements:
            patch_replacements(base, tmp, phase1_replacements)
        else:
            shutil.copy2(base, tmp)

        # Phase 2: substring replacements + bold amount
        data = bytearray(tmp.read_bytes())
        uni_to_cid = _parse_tounicode(data)
        if not uni_to_cid:
            return False, "ToUnicode CMap не найден"

        required = set()
        for _, new_val in phase2_substring:
            for c in new_val:
                cp = ord(c)
                required.add(0xA0 if cp == 0x20 else cp)
                if cp == 0x20:
                    required.add(0x20)
        if bold_amount_changed:
            new_amt = changes["сумма_приход"]
            for c in f"{new_amt} RUR":
                cp = ord(c)
                required.add(0xA0 if cp == 0x20 else cp)
                if cp == 0x20:
                    required.add(0x20)
        data, uni_to_cid = _extend_tounicode_identity(data, required, uni_to_cid)

        sub_count = 0
        f3_cmap = {
            ord('1'): '0011', ord('0'): '0012', ord(' '): '0010',
            ord(','): '0013', ord('R'): '0014', ord('U'): '0015',
        }
        old_bold_hex = _encode_f3(current["сумма_приход"] + " RUR", f3_cmap)

        # Collect streams to patch (can't modify data during re.finditer)
        pdf_data = bytes(data)
        stream_patches = []
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_data, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(pdf_data):
                continue
            try:
                dec = zlib.decompress(pdf_data[stream_start:stream_start + stream_len])
            except zlib.error:
                continue
            if b"BT" not in dec:
                continue

            new_dec = dec

            for old_val, new_val in phase2_substring:
                old_cid = _encode_cid(old_val, uni_to_cid)
                new_cid = _encode_cid(new_val, uni_to_cid)
                if not old_cid or not new_cid:
                    continue
                old_inner = old_cid[1:-1]
                new_inner = new_cid[1:-1]
                if old_inner in new_dec:
                    new_dec = new_dec.replace(old_inner, new_inner)
                    sub_count += 1

            if bold_amount_changed and old_bold_hex:
                new_amt = changes["сумма_приход"] + " RUR"
                new_bold_hex = _encode_f3(new_amt, f3_cmap)
                if new_bold_hex:
                    old_tag = b"<" + old_bold_hex + b">"
                    new_tag = b"<" + new_bold_hex + b">"
                    if old_tag in new_dec:
                        new_dec = new_dec.replace(old_tag, new_tag)
                        sub_count += 1
                else:
                    new_f1_cid = _encode_cid(new_amt, uni_to_cid)
                    if new_f1_cid and old_bold_hex:
                        for nl in [b"\r\n", b"\n"]:
                            old_block = b"/F3" + nl + b" 8 Tf" + nl + b"<" + old_bold_hex + b"> Tj"
                            new_block = b"/F1" + nl + b" 8 Tf" + nl + new_f1_cid + b" Tj"
                            if old_block in new_dec:
                                new_dec = new_dec.replace(old_block, new_block)
                                sub_count += 1
                                break

            if new_dec != dec:
                stream_patches.append((stream_start, stream_len, len_num_start, new_dec))

        # Apply patches in reverse order (so offsets stay valid)
        data = bytearray(pdf_data)
        for stream_start, stream_len, len_num_start, new_dec in reversed(stream_patches):
            new_raw = zlib.compress(new_dec, 9)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            data = bytearray(
                bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
            )
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str
            if len(new_len_str) != len(old_len_str):
                delta += len(new_len_str) - len(old_len_str)
            _update_xref(data, stream_start, delta)

        if stream_patches:
            tmp.write_bytes(data)

        # Phase 3: Tm position adjustments
        data = bytearray(tmp.read_bytes())
        uni_to_cid_final = _parse_tounicode(data)
        cid_widths = _extract_font_widths(bytes(data))

        # F3 widths for bold amount
        f3_widths = _extract_font_widths_nth(bytes(data), 2)

        tm_adjustments = []
        for field_key, tm_y in _AMOUNT_TM_Y.items():
            orig_x = _ORIG_TM_X.get(tm_y)
            if orig_x is None:
                continue

            if field_key == "сумма_приход_tx":
                new_text = (changes.get("сумма_приход", current["сумма_приход"]) + " RUR")
                right_edge = _RIGHT_EDGE_TX
                new_x = _calc_tm_x_f3(new_text, right_edge, f3_cmap, f3_widths)
            elif field_key == "сумма_расход_tx":
                new_text = "-" + (changes.get("сумма_расход", current["сумма_расход"]) + " RUR")
                right_edge = _RIGHT_EDGE_TX
                new_x = _calc_tm_x(new_text, right_edge, uni_to_cid_final, cid_widths)
            else:
                amt_key = field_key
                new_val = changes.get(amt_key, BLOCK2_DEFAULTS.get(amt_key, ""))
                new_text = new_val + " RUR"
                right_edge = _RIGHT_EDGE_HEADER
                new_x = _calc_tm_x(new_text, right_edge, uni_to_cid_final, cid_widths)

            old_tm = f"{orig_x:.3f} {tm_y:.3f} Tm".encode()
            new_tm = f"{new_x:.3f} {tm_y:.3f} Tm".encode()
            if len(old_tm) == len(new_tm) and old_tm != new_tm:
                tm_adjustments.append((old_tm, new_tm))

        tm_count = 0
        for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
            stream_len = int(m.group(2))
            stream_start = m.end()
            len_num_start = m.start(2)
            if stream_start + stream_len > len(data):
                continue
            try:
                dec = zlib.decompress(bytes(data[stream_start:stream_start + stream_len]))
            except zlib.error:
                continue
            if b"BT" not in dec:
                continue
            new_dec = dec
            for old_tm, new_tm in tm_adjustments:
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, new_tm)
                    tm_count += 1
            if new_dec != dec:
                new_raw = zlib.compress(new_dec, 9)
                delta = len(new_raw) - stream_len
                old_len_str = str(stream_len).encode()
                new_len_str = str(len(new_raw)).encode()
                if len(new_len_str) != len(old_len_str):
                    delta += len(new_len_str) - len(old_len_str)
                data = bytearray(
                    bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
                )
                num_end = len_num_start + len(old_len_str)
                data[len_num_start:num_end] = new_len_str
                _update_xref(data, stream_start, delta)

        tmp.write_bytes(data)
        shutil.move(str(tmp), str(output_path))
        return True, ""

    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(e)


def _encode_f3(text: str, f3_cmap: dict) -> bytes | None:
    """Encode text using F3 (Bold) CMap."""
    parts = []
    for ch in text:
        cp = ord(ch)
        if cp not in f3_cmap:
            return None
        parts.append(f3_cmap[cp])
    return "".join(parts).encode()


def _calc_tm_x_f3(text: str, right_edge: float, f3_cmap: dict, f3_widths: dict) -> float:
    """Calculate Tm x for F3 (Bold) font."""
    total = 0
    for ch in text:
        cp = ord(ch)
        cid_hex = f3_cmap.get(cp)
        if cid_hex:
            cid_num = int(cid_hex, 16)
            total += f3_widths.get(cid_num, 556)
        else:
            total += 556
    w = total * 8.0 / 1000
    return round(right_edge - w, 3)


def _extract_font_widths_nth(pdf_bytes: bytes, n: int) -> dict[int, int]:
    """Extract CID→width from the Nth /W array (0-indexed)."""
    pattern = rb'/W\s*\['
    idx = 0
    for m in re.finditer(pattern, pdf_bytes):
        if idx < n:
            idx += 1
            continue
        depth = 0
        for i in range(m.end() - 1, min(m.end() + 5000, len(pdf_bytes))):
            if pdf_bytes[i:i+1] == b'[':
                depth += 1
            elif pdf_bytes[i:i+1] == b']':
                depth -= 1
                if depth == 0:
                    w_str = pdf_bytes[m.end():i].decode('latin1')
                    tokens = []
                    for t in re.finditer(r'\[([^\]]*)\]|(\d+)', w_str):
                        if t.group(1) is not None:
                            tokens.append(('arr', [float(x) for x in t.group(1).split()]))
                        else:
                            tokens.append(('num', int(t.group(2))))
                    widths = {}
                    j = 0
                    while j < len(tokens):
                        if tokens[j][0] == 'num':
                            if j + 1 < len(tokens) and tokens[j+1][0] == 'arr':
                                cid = tokens[j][1]
                                for k, w in enumerate(tokens[j+1][1]):
                                    widths[cid + k] = int(w)
                                j += 2
                            else:
                                j += 1
                        else:
                            j += 1
                    return widths
                    break
    return {}


def _replace_stream(data: bytearray, stream_start: int, stream_len: int,
                    len_num_start: int, new_dec: bytes) -> None:
    """Compress new_dec, replace stream in data, update /Length and xref."""
    new_raw = zlib.compress(new_dec, 9)
    delta = len(new_raw) - stream_len
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    if len(new_len_str) != len(old_len_str):
        delta += len(new_len_str) - len(old_len_str)
    data[:] = bytearray(
        bytes(data[:stream_start]) + new_raw + bytes(data[stream_start + stream_len:])
    )
    num_end = len_num_start + len(old_len_str)
    data[len_num_start:num_end] = new_len_str
    _update_xref(data, stream_start, delta)


def _update_xref(data: bytearray, stream_start: int, delta: int) -> None:
    """Update xref offsets and startxref after stream modification."""
    if delta == 0:
        return
    xref_m = re.search(
        rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1):em.start(1) + 10] = f"{offset + delta:010d}".encode()
        data[xref_m.start(3):xref_m.end(3)] = bytes(entries)
    startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
    if startxref_m and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        data[pos:pos + len(str(old_pos))] = str(old_pos + delta).encode()


# ── Convenience: format blocks for display ────────────────────────────────

def format_block2(vals: dict | None = None) -> str:
    """Format Block 2 for Telegram display."""
    v = dict(BLOCK2_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"📊 Блок 2 — Сводка\n"
        f"├ Входящий остаток: {v['входящий_остаток']} RUR\n"
        f"├ Поступления: {v['поступления']} RUR\n"
        f"├ Расходы: {v['расходы']} RUR\n"
        f"├ Исходящий остаток: {v['исходящий_остаток']} RUR\n"
        f"├ Платежный лимит: {v['платежный_лимит']} RUR\n"
        f"└ Текущий баланс: {v['текущий_баланс']} RUR"
    )


def format_block3(vals: dict | None = None) -> str:
    """Format Block 3 for Telegram display."""
    v = dict(BLOCK3_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"👤 Блок 3 — Реквизиты\n"
        f"├ Счёт: {v['номер_счета']}\n"
        f"├ Клиент: {v['клиент_имя']}\n"
        f"├ Отчество: {v['клиент_отчество']}\n"
        f"├ Индекс: {v['индекс']}\n"
        f"├ Город: {v['город']}\n"
        f"└ Дом/кв: {v['дом_кв']}"
    )


def format_block1(vals: dict | None = None) -> str:
    """Format Block 1 for Telegram display."""
    v = dict(BLOCK1_DEFAULTS)
    if vals:
        v.update(vals)
    return (
        f"💳 Блок 1 — Операции\n"
        f"├ Код (расход): {v['код_операции_расход']}\n"
        f"├ Телефон: {v['телефон']}{v['телефон_окончание']}\n"
        f"├ Сумма расхода: -{v['сумма_расход']} RUR\n"
        f"├ Код (приход): {v['код_операции_приход']}\n"
        f"├ Получатель: {v['получатель_сокр']}\n"
        f"└ Сумма прихода: {v['сумма_приход']} RUR (жирный)"
    )
