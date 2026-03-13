#!/usr/bin/env python3
"""Отчёт координат последних букв правой колонки чека ВТБ СБП.

x1 = правая граница bbox (последняя буква). WALL = эталон из «Выполнено».

Использование:
  python3 report_last_letter_coords.py receipt.pdf
  python3 report_last_letter_coords.py receipt.pdf --csv
  python3 report_last_letter_coords.py receipt.pdf --tol 1.0
"""
import argparse
import re
import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    fitz = None


# Порядок полей для отчёта (сверху вниз по чеку)
_FIELD_ORDER = ("Выполнено", "ID операции", "дата", "счёт", "ФИО (отправитель)", "ФИО (получатель)", "телефон", "банк", "сумма")


def _field_hint(text: str) -> str:
    """Подсказка поля по содержимому."""
    if "Выполнено" in text:
        return "Выполнено"
    if re.search(r"\d{2}\.\d{2}\.\d{4}", text):
        return "дата"
    if re.search(r"\*\d{4}", text):
        return "счёт"
    if re.search(r"\+7\s*\(\d{3}\)", text) or re.search(r"\d{3}-\d{2}-\d{2}", text):
        return "телефон"
    if re.search(r"\d+\s*₽", text) or (re.search(r"\d", text) and "₽" in text):
        return "сумма"
    if re.search(r"[AB]\d{4}[0-9A-Fa-f]{10,}", text):
        return "ID операции"
    if "Сбербанк" in text or "ВТБ" in text or "Альфа" in text or "Т-Банк" in text or "Т‑Банк" in text:
        return "банк"
    if text and len(text) >= 3 and re.search(r"[а-яА-ЯёЁ]", text):
        if any(x in text for x in ["ович", "евич", "овна", "ич", "чна"]):
            return "ФИО"
    return "—"


def _assign_fio_hints(spans: list[dict]) -> None:
    """Различаем отправителя и получателя по Y (выше = отправитель)."""
    fio_spans = [s for s in spans if s["hint"] == "ФИО"]
    for i, s in enumerate(fio_spans):
        s["hint"] = "ФИО (отправитель)" if i == 0 else "ФИО (получатель)"


def get_right_column_spans(pdf_path: Path) -> list[dict]:
    """Извлечь spans правой колонки: x1, y, text, last_char."""
    if fitz is None:
        return []
    out = []
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]
        dt = page.get_text("dict")
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if not bb or bb[1] < 50 or bb[1] > 400:
                        continue
                    # Правая колонка: значения справа (x1 > 180, иначе попадёт левая подпись)
                    if 100 <= bb[0] <= 270 and bb[2] > 180:
                        text = (span.get("text", "") or "").strip()
                        if text:
                            last_char = text[-1] if text else ""
                            out.append({
                                "x1": round(bb[2], 4),
                                "y": round((bb[1] + bb[3]) / 2, 2),
                                "text": text[:50],
                                "last_char": last_char,
                                "hint": _field_hint(text),
                            })
        doc.close()
    except Exception:
        pass
    out.sort(key=lambda s: -s["y"])
    _assign_fio_hints(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Отчёт x1 последних букв правой колонки")
    ap.add_argument("pdf", type=Path, help="PDF чек")
    ap.add_argument("--csv", action="store_true", help="Вывод в CSV")
    ap.add_argument("--tol", type=float, default=1.0, help="Допуск для «Съезд» (pt)")
    ap.add_argument("--visual-out", type=Path, help="Сохранить копию PDF")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"Файл не найден: {args.pdf}")
        return 1

    spans = get_right_column_spans(args.pdf)
    if not spans:
        print("Не удалось извлечь spans (нужен PyMuPDF)")
        return 1

    wall_span = next((s for s in spans if s["hint"] == "Выполнено"), None)
    wall = wall_span["x1"] if wall_span else max(s["x1"] for s in spans)

    if args.csv:
        print("hint,x1,y,last_char,text")
        for s in spans:
            txt = s["text"].replace('"', '""')
            print(f'{s["hint"]},{s["x1"]},{s["y"]},{s["last_char"]},"{txt}"')
        return 0

    print(f"PDF: {args.pdf.name}")
    print(f"WALL = {wall:.4f}" + (' (последняя буква «Выполнено»)' if wall_span else ' (max x1)'))
    print()
    drift = [s for s in spans if abs(s["x1"] - wall) > args.tol]
    ok_spans = [s for s in spans if abs(s["x1"] - wall) <= args.tol]

    # Краткая сводка
    print("Сводка: правая граница (x1) последней буквы каждого поля")
    print(f"  OK:    {', '.join(s['hint'] for s in ok_spans) or '—'}")
    if drift:
        print(f"  Съезд: {', '.join(s['hint'] for s in drift)}")
    print()
    print(f"{'Поле':<22} | {'x1':>10} | {'Δ':>6} | статус | текст")
    print("-" * 75)
    for s in spans:
        delta = s["x1"] - wall
        status = "✓ OK" if abs(delta) <= args.tol else "✗ съезд"
        txt = (s["text"][:22] + "…") if len(s["text"]) > 25 else s["text"]
        print(f"{s['hint']:<22} | {s['x1']:>10.4f} | {delta:>+6.2f} | {status:<8} | {txt}")

    if args.visual_out:
        import shutil
        shutil.copy2(args.pdf, args.visual_out)
        print()
        print(f"Сохранено: {args.visual_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
