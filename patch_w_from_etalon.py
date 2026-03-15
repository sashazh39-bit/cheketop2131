#!/usr/bin/env python3
"""Заменить /W массив CIDFontType2 в receipt на эталон из 13.pdf.

/W — ширины глифов. Подстановка эталона убирает след добавленных CID.
ВНИМАНИЕ: После замены CIDs для Ф,Ч,Ю не будут иметь правильных ширин — 
отображение может сломаться (наложение, сдвиги).

Использование:
  python3 patch_w_from_etalon.py receipt_obj12_patched.pdf -o receipt_w_patched.pdf
  python3 patch_w_from_etalon.py input.pdf --etalon "/path/to/13.pdf" -o out.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def find_w_array_range(data: bytes) -> tuple[int, int] | None:
    """Найти /W [ ... ] /CIDToGIDMap. Возвращает (start_inner, end_inner) — границы содержимого внутри []."""
    m = re.search(rb"/W\s*\[\s*", data)
    if not m:
        return None
    bracket_start = m.end() - 1  # позиция [
    inner_start = m.end()
    # Ищем закрывающую ] перед /CIDToGIDMap
    rest = data[inner_start:]
    depth = 1
    i = 0
    while i < len(rest) and depth > 0:
        if rest[i : i + 1] == b"[":
            depth += 1
        elif rest[i : i + 1] == b"]":
            depth -= 1
        i += 1
    if depth != 0:
        return None
    inner_end = inner_start + i - 1
    # Проверяем, что после ] идёт /CIDToGIDMap
    if not re.search(rb"\]\s*/CIDToGIDMap", data[inner_end : inner_end + 30]):
        return None
    return inner_start, inner_end


def normalize_w_spaces(content: bytes) -> bytes:
    """Схлопнуть множественные пробелы в содержимом /W до одного, сохраняя структуру."""
    return re.sub(rb"\s+", b" ", content).strip()


def replace_w_array(
    target_data: bytearray,
    etalon_data: bytes,
    normalize_spaces: bool = True,
) -> bool:
    """Заменить содержимое /W в target на содержимое из etalon."""
    target_range = find_w_array_range(bytes(target_data))
    etalon_range = find_w_array_range(etalon_data)
    if not target_range or not etalon_range:
        return False
    t_start, t_end = target_range
    e_start, e_end = etalon_range

    old_content = bytes(target_data[t_start:t_end])
    new_content = etalon_data[e_start:e_end]
    if normalize_spaces:
        new_content = normalize_w_spaces(new_content)

    delta = len(new_content) - len(old_content)
    target_data[t_start:t_end] = new_content

    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", target_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > t_end:
                    new_offset = offset + delta
                    entries[em.start(1) : em.start(1) + 10] = f"{new_offset:010d}".encode()
            target_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", target_data)
        if startxref_m:
            sx_pos = int(startxref_m.group(1))
            if sx_pos > t_end:
                p = startxref_m.start(1)
                target_data[p : p + len(str(sx_pos))] = str(sx_pos + delta).encode()

    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Заменить /W на эталон из 13.pdf")
    ap.add_argument("input", help="receipt_obj12_patched.pdf или аналогичный")
    ap.add_argument("-o", "--output", default="receipt_w_patched.pdf")
    ap.add_argument("--etalon", default=None, help="Эталон 13.pdf (по умолчанию Downloads)")
    ap.add_argument("--no-normalize", action="store_true", help="Не схлопывать лишние пробелы в /W")
    args = ap.parse_args()

    inp = Path(args.input).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1

    etalon_paths = [
        Path(args.etalon).expanduser().resolve() if args.etalon else None,
        Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
        Path(__file__).parent / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
    ]
    etalon = None
    for p in etalon_paths:
        if p and p.exists():
            etalon = p
            break
    if not etalon:
        print("[ERROR] Не найден эталон 13.pdf. Укажите --etalon путь", file=sys.stderr)
        return 1

    target_data = bytearray(inp.read_bytes())
    etalon_data = etalon.read_bytes()

    if not replace_w_array(target_data, etalon_data):
        print("[ERROR] Не удалось заменить /W", file=sys.stderr)
        return 1

    out_path = Path(args.output).resolve()
    out_path.write_bytes(target_data)
    print(f"✅ /W массив заменён на эталон из {etalon.name}")
    print(f"   Результат: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
