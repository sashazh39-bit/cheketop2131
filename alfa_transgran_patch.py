#!/usr/bin/env python3
"""Универсальный патчер трансграничных чеков Альфа-Банка.

Работает на уровне CID-байтов: парсит ToUnicode CMap из PDF, кодирует
новый текст тем же шрифтом, заменяет в content stream. Сохраняет структуру,
обновляет /Length, xref, startxref. Меняет Document /ID.

Поля:
  amount      — «Сумма перевода»    (напр. 10 RUR → 3 036 RUR)
  commission  — «Комиссия»          (напр. 0 RUR → 50 RUR)
  rate        — «Курс конвертации»  (напр. 1 RUR = 0.1140 TJS)
  credited    — «Сумма зачисления»  (напр. 1,13 TJS → 343,06 TJS)
  phone       — номер телефона      (+992000332753 → +992000332793)
  name        — имя получателя      (Шукрулло М. → Иван П.)
  operation_id — номер операции      (C821803260001144 → ...)
"""
from __future__ import annotations

import hashlib
import re
import zlib
from pathlib import Path


def _parse_tounicode(data: bytes) -> dict[int, str]:
    """Парсит ToUnicode CMap: unicode_codepoint → CID hex (4-char uppercase)."""
    uni_to_cid: dict[int, str] = {}
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b'beginbfrange' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                src_start = int(mm.group(1), 16)
                src_end = int(mm.group(2), 16)
                dest = int(mm.group(3), 16)
                for i in range(src_end - src_start + 1):
                    uni_to_cid[dest + i] = f'{src_start + i:04X}'
            if uni_to_cid:
                return uni_to_cid
        if b'beginbfchar' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                cid = mm.group(1).decode().upper().zfill(4)
                uni = int(mm.group(2).decode(), 16)
                uni_to_cid[uni] = cid
            if uni_to_cid:
                return uni_to_cid
    return uni_to_cid


def _encode_text(text: str, uni_to_cid: dict[int, str]) -> bytes | None:
    """Кодирует текст в CID hex строку <XXXX...>."""
    parts = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        parts.append(uni_to_cid[cp])
    return ('<' + ''.join(parts) + '>').encode()


def _encode_tj_array(text: str, uni_to_cid: dict[int, str], kern: str = '1') -> bytes | None:
    """Кодирует текст как TJ-массив: [ <CID> kern <CID> kern ... ] TJ."""
    parts = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        parts.append(f'<{uni_to_cid[cp]}>')
    return ('[ ' + f' {kern} '.join(parts) + ' ] TJ').encode()


def _find_tj_pattern(dec: bytes, text: str, uni_to_cid: dict[int, str]) -> bytes | None:
    """Ищет CID-паттерн text в декомпрессированном потоке.

    Пробует: Tj одним блоком, TJ-массив, plain hex вхождение.
    """
    # 1) Попробовать как Tj (один hex-блок)
    encoded = _encode_text(text, uni_to_cid)
    if encoded and encoded in dec:
        return encoded

    # 2) Попробовать как часть TJ массива — ищем hex CIDs рядом
    cids = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        cids.append(uni_to_cid[cp])
    hex_seq = ''.join(cids).lower().encode()
    if hex_seq in dec.lower():
        pos = dec.lower().find(hex_seq)
        return dec[pos:pos + len(hex_seq)]
    return None


def _text_to_cid_hex(text: str, uni_to_cid: dict[int, str]) -> str | None:
    """Кодирует текст в строку CID hex (без угловых скобок), lowercase."""
    parts = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            cp = 0xA0
        if cp not in uni_to_cid:
            return None
        parts.append(uni_to_cid[cp].lower())
    return ''.join(parts)


def _find_and_replace_field(
    dec: bytes,
    old_text: str,
    new_text: str,
    uni_to_cid: dict[int, str],
) -> tuple[bytes, bool]:
    """Ищет old_text в потоке (различные форматы CID) и заменяет на new_text.

    Поддерживает:
    - Tj (один hex-блок)
    - TJ-массив где текст разбит на несколько <hex> блоков с керном

    Возвращает (new_dec, replaced).
    """
    nbsp = '\xa0'
    old_variants = [old_text, old_text + nbsp]
    new_with_nbsp = new_text + nbsp

    for old_v in old_variants:
        new_v = new_with_nbsp if old_v.endswith(nbsp) else new_text

        old_hex = _text_to_cid_hex(old_v, uni_to_cid)
        new_hex = _text_to_cid_hex(new_v, uni_to_cid)
        if not old_hex or not new_hex:
            continue

        # Вариант 1: Tj (один hex-блок <XXXX>)
        old_enc = ('<' + old_hex + '>').encode()
        if old_enc in dec:
            new_enc = ('<' + new_hex + '>').encode()
            return dec.replace(old_enc, new_enc, 1), True

        # Вариант 2: внутри одного большого <hex> блока
        for match in re.finditer(rb'<([0-9a-fA-F]+)>', dec):
            inner = match.group(1).decode().lower()
            if old_hex in inner:
                new_inner = inner.replace(old_hex, new_hex, 1)
                return dec.replace(match.group(0), b'<' + new_inner.encode() + b'>', 1), True

        # Вариант 3: TJ-массив — текст разбит на несколько <hex> блоков
        # Находим все TJ-массивы, собираем hex из всех блоков, ищем old_hex
        for tj_match in re.finditer(rb'\[\s*(.*?)\]\s*TJ', dec, re.DOTALL):
            tj_content = tj_match.group(1)
            hex_blocks = re.findall(rb'<([0-9a-fA-F]+)>', tj_content)
            if not hex_blocks:
                continue
            combined = ''.join(b.decode().lower() for b in hex_blocks)
            if old_hex not in combined:
                continue
            # Нашли! Собираем kern значение из массива
            kern_m = re.search(rb'>\s*(-?\d+(?:\.\d+)?)\s*<', tj_content)
            kern = kern_m.group(1).decode() if kern_m else '1'
            # Строим новый TJ: каждый CID (4 hex chars) через kern
            new_cids_list = [new_hex[i:i + 4] for i in range(0, len(new_hex), 4)]
            # Заменяем old_hex в combined и строим новый TJ
            new_combined = combined.replace(old_hex, new_hex, 1)
            new_cids_all = [new_combined[i:i + 4] for i in range(0, len(new_combined), 4)]
            new_tj_parts = [f'<{cid}>' for cid in new_cids_all]
            new_tj = ('[ ' + f' {kern} '.join(new_tj_parts) + ' ] TJ').encode()
            old_tj = tj_match.group(0)
            return dec.replace(old_tj, new_tj, 1), True

    return dec, False


def _update_xref(data: bytearray, stream_start: int, delta: int) -> None:
    xref_m = re.search(
        rb'xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)',
        data,
    )
    if xref_m:
        entries = bytearray(xref_m.group(3))
        for em in re.finditer(rb'(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)', entries):
            offset = int(em.group(1))
            if offset > stream_start:
                entries[em.start(1): em.start(1) + 10] = f'{offset + delta:010d}'.encode()
        data[xref_m.start(3): xref_m.end(3)] = bytes(entries)

    startxref_m = re.search(rb'startxref\r?\n(\d+)\r?\n', data)
    if startxref_m and delta != 0 and stream_start < int(startxref_m.group(1)):
        pos = startxref_m.start(1)
        old_pos = int(startxref_m.group(1))
        data[pos: pos + len(str(old_pos))] = str(old_pos + delta).encode()


def _mutate_doc_id(data: bytearray) -> bool:
    """Мутирует Document /ID — ставит MD5 от нового содержимого."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', bytes(data))
    if not id_m:
        return False
    h = hashlib.md5(bytes(data)).hexdigest().upper()
    new_id1 = h.encode()
    new_id2 = h.encode()
    # Заменяем оба ID
    old1 = id_m.group(1)
    old2 = id_m.group(2)
    full_old = id_m.group(0)
    full_new = full_old.replace(old1, new_id1[:len(old1)].ljust(len(old1), b'0'), 1)
    full_new = full_new.replace(old2, new_id2[:len(old2)].ljust(len(old2), b'0'), 1)
    data[id_m.start(): id_m.end()] = full_new
    return True


def get_available_chars(pdf_path: str | Path) -> set[str]:
    """Возвращает набор символов, доступных в CMap PDF."""
    data = Path(pdf_path).read_bytes()
    uni_to_cid = _parse_tounicode(data)
    chars = set()
    for cp in uni_to_cid:
        try:
            c = chr(cp)
            if c.isprintable() or c == '\xa0':
                chars.add(c)
        except ValueError:
            pass
    return chars


def check_text_available(pdf_path: str | Path, text: str) -> list[str]:
    """Проверяет, есть ли все символы text в CMap PDF. Возвращает список недостающих."""
    data = Path(pdf_path).read_bytes()
    uni_to_cid = _parse_tounicode(data)
    missing = []
    for c in text:
        cp = ord(c)
        if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
            continue
        if cp not in uni_to_cid:
            missing.append(c)
    return list(dict.fromkeys(missing))


def patch_transgran(
    pdf_data: bytes,
    *,
    amount: str | None = None,
    commission: str | None = None,
    rate: str | None = None,
    credited: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, str | None, bytes | None]:
    """Патчит трансграничный чек Альфа-Банка.

    Каждый параметр — строка формата "OLD=NEW", например:
      amount="10 RUR=3 036 RUR"
      phone="+992000332753=+992000332793"
      name="Шукрулло М.=Иван П."

    Возвращает (ok, error_msg, new_data).
    """
    data = bytearray(pdf_data)
    uni_to_cid = _parse_tounicode(data)
    if not uni_to_cid:
        return False, 'ToUnicode CMap не найден в PDF', None

    replacements: list[tuple[str, str, str]] = []
    for label, value in [
        ('Сумма перевода', amount),
        ('Комиссия', commission),
        ('Курс конвертации', rate),
        ('Сумма зачисления', credited),
        ('Телефон', phone),
        ('Получатель', name),
        ('Номер операции', operation_id),
    ]:
        if not value:
            continue
        if '=' not in value:
            return False, f'{label}: формат должен быть "старое=новое"', None
        old_val, new_val = value.split('=', 1)
        old_val = old_val.strip()
        new_val = new_val.strip()
        if not old_val or not new_val:
            return False, f'{label}: пустое значение', None
        replacements.append((old_val, new_val, label))

    if not replacements:
        return False, 'Нет замен', None

    # Проверяем доступность символов для новых значений
    for old_val, new_val, label in replacements:
        missing = []
        for c in new_val:
            cp = ord(c)
            if cp == 0x20 and cp not in uni_to_cid and 0xA0 in uni_to_cid:
                continue
            if cp not in uni_to_cid:
                missing.append(c)
        if missing:
            return False, f'{label}: символы отсутствуют в шрифте: {"".join(missing)}', None

    total_replaced = 0
    errors = []

    for m in re.finditer(
        rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL
    ):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start: stream_start + stream_len]))
        except zlib.error:
            continue
        if b'BT' not in dec:
            continue

        new_dec = dec
        for old_val, new_val, label in replacements:
            new_dec, replaced = _find_and_replace_field(new_dec, old_val, new_val, uni_to_cid)
            if replaced:
                total_replaced += 1

        if new_dec == dec:
            continue

        new_raw = zlib.compress(new_dec, 9)
        old_len_str = str(stream_len).encode()
        new_len_str = str(len(new_raw)).encode()
        delta = len(new_raw) - stream_len + (len(new_len_str) - len(old_len_str))

        data = bytearray(
            data[:stream_start] + new_raw + data[stream_start + stream_len:]
        )
        data[len_num_start: len_num_start + len(old_len_str)] = new_len_str

        _update_xref(data, stream_start, delta)
        break

    if total_replaced == 0:
        return False, 'Ни одна замена не применена. Проверьте исходные значения.', None

    _mutate_doc_id(data)
    return True, None, bytes(data)


def extract_transgran_fields(pdf_data: bytes) -> dict[str, str]:
    """Извлекает текстовые поля из трансграничного чека через fitz.

    Возвращает dict с ключами: amount, commission, rate, credited, datetime,
    name, phone, account, operation_id.
    """
    try:
        import fitz
    except ImportError:
        return {}

    doc = fitz.open(stream=pdf_data, filetype='pdf')
    if doc.page_count == 0:
        return {}
    page = doc[0]
    text = page.get_text()
    doc.close()

    fields: dict[str, str] = {}
    lines = [l.strip() for l in text.split('\n')]

    FIELD_MAP = [
        ('Сумма перевода', 'amount'),
        ('Комиссия', 'commission'),
        ('Курс конвертации', 'rate'),
        ('Сумма зачисления', 'credited'),
        ('Дата и время перевода', 'datetime'),
        ('Получатель', 'name'),
        ('Номер телефона', 'phone'),
        ('Счёт списания', 'account'),
        ('Счет списания', 'account'),
        ('Номер операции', 'operation_id'),
    ]

    for i, line in enumerate(lines):
        for label, key in FIELD_MAP:
            if label in line.replace('\xa0', ' '):
                for j in range(i + 1, min(i + 4, len(lines))):
                    val = lines[j].replace('\xa0', ' ').strip()
                    if val and val not in ('\xa0', ' '):
                        fields[key] = val
                        break
                break
    return fields


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Использование: python3 alfa_transgran_patch.py input.pdf [--extract]')
        sys.exit(1)

    inp = Path(sys.argv[1])
    if '--extract' in sys.argv:
        fields = extract_transgran_fields(inp.read_bytes())
        for k, v in fields.items():
            print(f'  {k}: {v}')
        sys.exit(0)

    print('Используйте как модуль или через бота.')
