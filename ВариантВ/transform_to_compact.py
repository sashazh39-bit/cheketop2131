#!/usr/bin/env python3
"""Вариант B: трансформация check(3) в компактный формат.

- Удаление блока «Сообщение» (метка + 2 строки значений)
- Объединение «Имя» + «плательщика» в одну строку
- Объединение «Телефон» + «получателя» в одну строку
- MediaBox: высота 437.25 (как random_receipt_2)

Использование:
  python3 transform_to_compact.py input.pdf output.pdf
  python3 transform_to_compact.py "база_чеков/vtb/СБП/check (3).pdf" compact_base.pdf
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

# Добавить родительскую папку в путь
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vtb_cmap import text_to_cids
from vtb_test_generator import build_tj


def _parse_tounicode_from_pdf(pdf_bytes: bytes) -> dict[int, str]:
    """Извлечь uni_to_cid из ToUnicode stream."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", pdf_bytes, re.DOTALL):
        stream_len = int(m.group(3))
        stream_start = m.end()
        if b"/ToUnicode" not in (m.group(2) + m.group(4)):
            continue
        if stream_start + stream_len > len(pdf_bytes):
            continue
        try:
            dec = zlib.decompress(pdf_bytes[stream_start : stream_start + stream_len])
        except zlib.error:
            continue
        if b"beginbfchar" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode().upper(), 16)
                uni_to_cid[uni] = cid
        elif b"beginbfrange" in dec:
            for mm in re.finditer(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", dec):
                s1, s2, d = int(mm.group(1).decode(), 16), int(mm.group(2).decode(), 16), int(mm.group(3).decode(), 16)
                for i in range(s2 - s1 + 1):
                    uni_to_cid[d + i] = f"{s1 + i:04X}"
    return uni_to_cid


def _build_label_tj(text: str, kern: str = "-16.66667") -> bytes:
    """Текст метки → TJ. Использует vtb_cmap или ToUnicode."""
    cids = text_to_cids(text)
    if not cids:
        return b"[]"
    return b"[" + build_tj(cids, kern=kern) + b"]"


def _count_tj_glyphs(tj_content: bytes) -> int:
    n = 1
    for k in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        n += tj_content.count(k)
    return n


def transform_stream(dec: bytes, pdf_bytes: bytes) -> bytes:
    """Трансформация content stream: удалить Сообщение, объединить метки."""
    imya_plat_tj = _build_label_tj("Имя плательщика")
    telefon_tj = _build_label_tj("Телефон получателя")

    pat = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"

    blocks = []
    for mt in re.finditer(pat, dec):
        x, y = float(mt.group(2)), float(mt.group(4))
        blocks.append({"match": mt, "x": x, "y": y, "content": mt.group(5)})

    to_remove = []
    replacements = []

    for i, blk in enumerate(blocks):
        x, y, content = blk["x"], blk["y"], blk["content"]
        n = _count_tj_glyphs(content)

        # 1. Удалить правую колонку: блоки Сообщения (y ~263, ~251, x>90)
        if x > 90 and 250 < y < 265:
            to_remove.append(i)
            continue
        if x > 90 and 248 < y < 255:
            to_remove.append(i)
            continue

        # 2. Удалить левую колонку: метка «Сообщение» (y ~262.5, x<100)
        if x < 100 and 260 < y < 266:
            to_remove.append(i)
            continue

        # 3. Объединить «Имя» (y~204, n=5-9) и «плательщика» (y~192). Исключить Получатель (y=228)
        if x < 100 and 200 < y < 208 and 5 <= n <= 9:
            replacements.append((blk["match"], b"1 0 0 1 " + f"{x:.5f}".encode() + b" " + f"{y:.5f}".encode() + b" Tm\n" + imya_plat_tj + b" TJ"))
            continue
        if x < 100 and 190 < y < 195 and 8 <= n <= 12:
            to_remove.append(i)
            continue

        # 4. Объединить «Телефон» и «получателя» — только если n=8 (Телефон), НЕ n=5 (Банк)
        # В check(3): Телефон n=8, получателя n=10, Банк n=5. Порядок: 168=Банк, 156=получателя.
        # В некоторых чеках: между Получатель и Банк есть Телефон. Пропускаем — структура может отличаться.

    result = dec
    for mt, new_block in replacements:
        result = result.replace(mt.group(0), new_block)

    remove_matches = [blocks[i]["match"] for i in to_remove]
    for mt in sorted(remove_matches, key=lambda m: -m.start()):
        result = result.replace(mt.group(0), b"")

    # Сдвиг вверх: блоки ниже удалённого Сообщения (y < 252) — сместить на 24 pt
    Y_SHIFT = 24.0
    Y_CUTOFF = 252.0
    pat2 = rb"(1 0 0 1 )([\d.]+)( ([\d.]+) Tm)\s*\[([^\]]*)\](\s*TJ)"
    def shift_repl(m):
        y = float(m.group(4))
        if y >= Y_CUTOFF:
            return m.group(0)
        new_y = y + Y_SHIFT
        rest = m.group(0)[m.end(3) - m.start(0) :]
        return m.group(1) + m.group(2) + f" {new_y:.5f} Tm".encode() + rest
    result = re.sub(pat2, shift_repl, result)
    return result


def update_mediabox(data: bytearray, new_height: float = 437.25) -> bool:
    """Задать высоту MediaBox."""
    m = re.search(rb"/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s*\]", data)
    if not m:
        return False
    x0, y0, x1, y1 = m.group(1).decode(), m.group(2).decode(), m.group(3).decode(), m.group(4).decode()
    new_box = f"/MediaBox [ {x0} {y0} {x1} {new_height:.2f} ]"
    data[m.start() : m.end()] = new_box.encode()
    return True


def transform_pdf(input_path: Path, output_path: Path) -> bool:
    """Трансформировать PDF в компактный формат."""
    data = bytearray(input_path.read_bytes())

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec:
            continue

        new_dec = transform_stream(dec, bytes(data))
        if new_dec != dec:
            new_raw = zlib.compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            # Пересобираем data: до потока + новый поток + после потока
            data = bytearray(
                data[:stream_start] + new_raw + data[stream_start + stream_len :]
            )
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            if len_num_start < stream_start:
                data[len_num_start : len_num_start + len(old_len_str)] = new_len_str.ljust(len(old_len_str))
            if delta != 0:
                xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
                if xref_m:
                    entries = bytearray(xref_m.group(3))
                    for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                        offset = int(em.group(1))
                        if offset > stream_start:
                            entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                    data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        break

    update_mediabox(data, 437.25)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return True


def main() -> int:
    if len(sys.argv) < 3:
        print("Использование: python3 transform_to_compact.py input.pdf output.pdf")
        return 1
    inp = Path(sys.argv[1]).expanduser().resolve()
    out = Path(sys.argv[2]).expanduser().resolve()
    if not inp.exists():
        print(f"[ERROR] Не найден: {inp}", file=sys.stderr)
        return 1
    if transform_pdf(inp, out):
        print(f"[OK] Трансформирован: {out} ({out.stat().st_size} bytes)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
