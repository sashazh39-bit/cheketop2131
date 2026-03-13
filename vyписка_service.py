#!/usr/bin/env python3
"""Сервис для работы с выписками: сканирование, патчинг, расчёты."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    fitz = None

BASE = Path(__file__).parent
BASE_STATEMENT = BASE / "база_выписок" / "vyписка_один_платеж.pdf"
# Эталон: ФИО и сумма одной операции для замены при выписке по чеку
BASE_OLD_FIO = "Жеребятьев Александр Евгеньевич"
BASE_AMOUNT = 6135




def _parse_cid_to_uni(pdf_bytes: bytes) -> dict[int, int]:
    """Извлечь CID→Unicode из ToUnicode (beginbfrange)."""
    cid_to_uni = {}
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(pdf_bytes):
            continue
        try:
            import zlib
            dec = zlib.decompress(pdf_bytes[stream_start : stream_start + stream_len])
        except Exception:
            continue
        if b"beginbfrange" not in dec:
            continue
        for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
            cid_s, cid_e, uni_s = int(mm.group(1), 16), int(mm.group(2), 16), int(mm.group(3), 16)
            for i in range(cid_e - cid_s + 1):
                cid_to_uni[cid_s + i] = uni_s + i
        return cid_to_uni
    return cid_to_uni


def get_available_chars(pdf_path: Path) -> set[str]:
    """Символы, доступные в шрифтах выписки (ToUnicode CMap)."""
    data = pdf_path.read_bytes()
    cid_to_uni = _parse_cid_to_uni(data)
    return {chr(v) for v in cid_to_uni.values() if 0 < v < 0x110000}


def get_missing_chars(pdf_path: Path, text: str) -> set[str]:
    """Символы из text, которых нет в шрифтах выписки."""
    available = get_available_chars(pdf_path)
    return {c for c in text if c not in available and not c.isspace()}


# Недоступные символы (по плану): ЁГЕЖЗЙЛУФХЦЧШЩЪЫЬЭЮЯжщъэ
UNAVAILABLE_RU = set("ЁГЕЖЗЙЛУФХЦЧШЩЪЫЬЭЮЯжщъэ")


def scan_statement_amounts(pdf_path: Path) -> list[tuple[str, str]]:
    """
    Извлечь денежные значения из операций выписки.
    Возвращает список (old_text, suggested_new) для замен.
    Паттерны: -X.XX RUB, X.XX в колонке Расход.
    """
    if not fitz:
        return []
    path = Path(pdf_path)
    if not path.exists():
        return []
    try:
        doc = fitz.open(path)
        page = doc[0]
        dt = page.get_text("dict")
        doc.close()
    except Exception:
        return []

    amounts = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "")
                # -10.00 RUB, -50.00 RUB, 10.00, 50.00 (в колонке Расход)
                if re.match(r"^-\d+\.\d{2}\s*RUB$", t):
                    amounts.append((t, t))
                elif re.match(r"^\d+\.\d{2}$", t) and span.get("bbox", [0])[0] > 250:
                    amounts.append((t, t))
    return list(dict.fromkeys(amounts))


def scan_statement_transactions(pdf_path: Path) -> list[float]:
    """Извлечь суммы расходов из операций (положительные числа в колонке Расход)."""
    if not fitz:
        return []
    path = Path(pdf_path)
    if not path.exists():
        return []
    try:
        doc = fitz.open(path)
        page = doc[0]
        dt = page.get_text("dict")
        doc.close()
    except Exception:
        return []

    expenses = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "")
                bbox = span.get("bbox", [0, 0, 0, 0])
                # Расход — справа (x > 250)
                if re.match(r"^\d+\.\d{2}$", t) and bbox[0] > 250:
                    try:
                        expenses.append(float(t))
                    except ValueError:
                        pass
    return expenses


def calculate_balance_and_expenses(
    transactions: list[float], balance_start: float
) -> tuple[float, float]:
    """Расходы = сумма трат, Баланс_конец = Баланс_нач - Расходы."""
    expenses = sum(transactions)
    balance_end = balance_start - expenses
    return balance_end, expenses


def _format_amount(val: float) -> str:
    """Формат суммы: 1,000.00 (запятая как разделитель тысяч)."""
    s = f"{val:,.2f}"
    return s  # Python: 1,000.00


def patch_statement(
    in_path: Path,
    out_path: Path,
    replacements: dict[str, Any],
) -> tuple[bool, str]:
    """
    Применить замены к выписке.
    replacements: {
      "amounts": [(10, 10000), (50, 1000)],  # пары (from, to)
      "phone": "+7 999 123-45-67",
      "balance_start": 55242.65,
      "balance_end": 48242.65,
      "expenses": 7000.00,
      "fio": "Иванов Иван Иванович",
      "old_fio": "Вислоусов Демид Андреевич",  # для patch_fio
      "application_id": "B606...",
    }
    """
    try:
        from patch_vyписка_13_03 import _extract_pdf_id, _restore_pdf_id
        from patch_vyписка_fio_stream import patch_fio_in_stream
    except ImportError:
        return False, "Модули patch_vyписка_* не найдены"

    if not fitz:
        return False, "PyMuPDF не установлен"

    in_path = Path(in_path)
    out_path = Path(out_path)
    if not in_path.exists():
        return False, f"Файл не найден: {in_path}"

    orig_id = _extract_pdf_id(in_path.read_bytes()) if in_path.exists() else None

    # Копируем вход во временный файл
    import shutil
    import tempfile
    tmp_in = Path(tempfile.mktemp(suffix=".pdf"))
    tmp_out = Path(tempfile.mktemp(suffix=".pdf"))
    shutil.copy2(in_path, tmp_in)

    doc = fitz.open(tmp_in)
    page = doc[0]
    dt = page.get_text("dict")
    orig_meta = dict(doc.metadata or {})

    amounts = replacements.get("amounts", [])
    if isinstance(amounts, list) and amounts:
        # Преобразуем в замены для patch: (old, new) как строки
        repl_list = []
        for pair in amounts:
            if len(pair) >= 2:
                old_val, new_val = pair[0], pair[1]
                # Для чисел >= 1000 в PDF используется запятая: 6,135.00
                old_str = _format_amount(float(old_val)) if isinstance(old_val, (int, float)) else str(old_val)
                new_str = _format_amount(float(new_val))
                if old_val < 0 or str(old_val).startswith("-"):
                    repl_list.append((f"-{old_str} RUB", f"-{new_str} RUB"))
                    repl_list.append((old_str, new_str))
                else:
                    repl_list.append((old_str, new_str))
                    repl_list.append((f"-{old_str} RUB", f"-{new_str} RUB"))

        for old_text, new_text in repl_list:
            for block in dt.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text") != old_text:
                            continue
                        bbox = span.get("bbox")
                        if not bbox or len(bbox) != 4:
                            continue
                        x0, y0, x1, y1 = bbox
                        fontsize = float(span.get("size", 7.0))
                        c = int(span.get("color", 0))
                        color = (
                            ((c >> 16) & 255) / 255.0,
                            ((c >> 8) & 255) / 255.0,
                            (c & 255) / 255.0,
                        )
                        if "50.00" in old_text and x0 < 250:
                            continue
                        if "10.00" in old_text and x0 < 250:
                            continue
                        rect = fitz.Rect(x0, y0, x1, y1)
                        page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))
                        try:
                            tw = fitz.get_text_length(new_text, fontname="helv", fontsize=fontsize)
                        except (ValueError, TypeError):
                            tw = len(new_text) * fontsize * 0.55
                        insert_x = x1 - tw
                        baseline_y = y1 - fontsize * 0.2
                        page.insert_text(
                            fitz.Point(insert_x, baseline_y),
                            new_text,
                            fontsize=fontsize,
                            fontname="helv",
                            color=color,
                        )

    # balance_end, expenses — в шапке: [0]=баланс нач, [1]=поступления, [2]=баланс конец, [3]=расходы
    balance_end = replacements.get("balance_end")
    expenses = replacements.get("expenses")
    header_spans = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                st = span.get("text", "")
                bbox = span.get("bbox")
                if not bbox or bbox[1] > 280:
                    continue
                if re.match(r"^[\d,]+\.\d{2}\s*RUB$", st):
                    header_spans.append((span, bbox[1], bbox[0]))
    header_spans.sort(key=lambda x: (x[1], x[2]))
    if balance_end is not None and len(header_spans) >= 3:
        span = header_spans[2][0]
        bbox = span.get("bbox")
        if bbox:
            fs = float(span.get("size", 9))
            c = int(span.get("color", 0))
            color = ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255
            be_str = _format_amount(float(balance_end)) + " RUB"
            page.draw_rect(fitz.Rect(*bbox), color=(1, 1, 1), fill=(1, 1, 1))
            tw = fitz.get_text_length(be_str, fontname="helv", fontsize=fs)
            page.insert_text(
                fitz.Point(bbox[2] - tw, bbox[3] - fs * 0.2),
                be_str,
                fontsize=fs,
                fontname="helv",
                color=color,
            )
    if expenses is not None and len(header_spans) >= 4:
        span = header_spans[3][0]
        bbox = span.get("bbox")
        if bbox:
            fs = float(span.get("size", 9))
            c = int(span.get("color", 0))
            color = ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255
            ex_str = _format_amount(float(expenses)) + " RUB"
            page.draw_rect(fitz.Rect(*bbox), color=(1, 1, 1), fill=(1, 1, 1))
            tw = fitz.get_text_length(ex_str, fontname="helv", fontsize=fs)
            page.insert_text(
                fitz.Point(bbox[2] - tw, bbox[3] - fs * 0.2),
                ex_str,
                fontsize=fs,
                fontname="helv",
                color=color,
            )

    doc.set_metadata(orig_meta)
    doc.save(tmp_out, garbage=0, deflate=True)
    doc.close()
    try:
        tmp_in.unlink()
    except OSError:
        pass

    # ФИО
    old_fio = replacements.get("old_fio", BASE_OLD_FIO)
    new_fio = replacements.get("fio")
    if new_fio:
        missing = get_missing_chars(tmp_out, new_fio)
        if missing:
            try:
                tmp_out.unlink()
            except OSError:
                pass
            return False, f"Недоступные символы в ФИО: {''.join(missing)}"
        patch_fio_in_stream(tmp_out, old_fio, new_fio, tmp_out, allow_len_mismatch=True)

    shutil.move(str(tmp_out), str(out_path))

    if orig_id and out_path.exists():
        _restore_pdf_id(out_path, orig_id)

    return True, ""
