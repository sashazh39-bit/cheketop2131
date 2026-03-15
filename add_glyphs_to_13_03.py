#!/usr/bin/env python3
"""Добавить глифы Ф, Ч, Ю из check(3) в целевой чек.

check(3) ToUnicode: Ф=0230, Ч=0233, Ю=023A (CID).
CIDToGIDMap: CID -> GID (не Identity, font 89 глифов).
Копируем глифы по реальному GID из donor в base.

По умолчанию база — 13.pdf (BaseFont AAHTMC, совпадение структуры с эталоном).
  --target путь к 17.pdf — для BaseFont AASONC (прохождение проверки).
Использование:
  python3 add_glyphs_to_13_03.py -o receipt.pdf
  python3 add_glyphs_to_13_03.py --target "/path/to/13-03-26_00-00 17.pdf" -o out.pdf
"""
from __future__ import annotations

import re
import random
import zlib
import sys
from datetime import datetime
from pathlib import Path
from io import BytesIO

BASE = Path(__file__).parent
DONOR = BASE / "база_чеков" / "vtb" / "СБП" / "check (3).pdf"

# 13.pdf — для совпадения структуры (BaseFont AAHTMC, obj 1,9,13,17)
TARGET_13 = [
    Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
]
# 17.pdf — для прохождения проверки (BaseFont AASONC)
TARGET_17 = [
    Path.home() / "Downloads" / "13-03-26_00-00 17.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 17.pdf",
]
TARGET = Path.home() / "Downloads" / "13-03-26_00-00 13.pdf"  # fallback

# check(3).pdf ToUnicode (из beginbfrange):
#   Uppercase А-Я:  Unicode 0x0410-0x042F → CID 0x021C-0x023B
#   Lowercase а-я:  Unicode 0x0430-0x044F → CID 0x023C-0x025B
# Полная таблица Unicode → CID в доноре:
DONOR_CIDS: dict[int, int] = {}
# А (0x0410) → CID 0x021C, Б (0x0411) → 0x021D, ..., Я (0x042F) → 0x023B
for _i in range(32):  # А-Я
    DONOR_CIDS[0x0410 + _i] = 0x021C + _i
# а (0x0430) → CID 0x023C, б (0x0431) → 0x023D, ..., я (0x044F) → 0x025B
for _i in range(32):  # а-я
    DONOR_CIDS[0x0430 + _i] = 0x023C + _i
# Ё/ё — не в доноре, fallback через е/Е; ₽ — не Cyrillic буква
# Для обратной совместимости — проверяем что Ф/Ч/Ю на месте:
assert DONOR_CIDS[0x0424] == 0x0230  # Ф
assert DONOR_CIDS[0x0427] == 0x0233  # Ч
assert DONOR_CIDS[0x042E] == 0x023A  # Ю


def _decompress_stream(raw: bytes) -> bytes:
    if raw.startswith(b"\r\n"):
        raw = raw[2:]
    elif raw.startswith(b"\n"):
        raw = raw[1:]
    return zlib.decompress(raw)


def _find_and_patch_cidtogid(data: bytearray, new_cid_to_gid: dict[int, int]) -> bool:
    """Патч CIDToGIDMap: для каждого new_cid записать GID в stream."""
    ctg_ref = re.search(rb"/CIDToGIDMap\s+(\d+)\s+0\s+R", data)
    if not ctg_ref:
        return False
    oid = int(ctg_ref.group(1))
    pat = rf"{oid}\s+0\s+obj".encode()
    m = re.search(pat, data)
    if not m:
        return False
    chunk = data[m.start() : m.start() + 300]
    stream_m = re.search(rb">>\s*stream\r?\n", chunk)
    len_m = re.search(rb"/Length\s+(\d+)", chunk)
    if not stream_m or not len_m:
        return False
    stream_len = int(len_m.group(1))
    len_pos = m.start() + len_m.start(1)
    stream_start = m.start() + stream_m.end()
    if stream_start + stream_len > len(data):
        return False
    raw = bytes(data[stream_start : stream_start + stream_len])
    try:
        dec = bytearray(_decompress_stream(raw))
    except zlib.error:
        return False
    for cid, gid in new_cid_to_gid.items():
        idx = cid * 2
        if idx + 2 <= len(dec):
            dec[idx : idx + 2] = (gid).to_bytes(2, "big")
    new_raw = _compress_stream(bytes(dec))
    delta = len(new_raw) - stream_len
    data[stream_start : stream_start + stream_len] = new_raw
    old_len_str = str(stream_len).encode()
    new_len_str = str(len(new_raw)).encode()
    data[len_pos : len_pos + len(old_len_str)] = new_len_str.ljust(len(old_len_str))[: len(old_len_str)]
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
        if startxref_m and stream_start < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            data[p : p + len(str(old_p))] = str(old_p + delta).encode()
    return True


def _get_cidtogid_map(pdf_data: bytes) -> dict[int, int] | None:
    """Извлечь CID->GID из CIDToGIDMap stream. Ищем объект по номеру."""
    ctg_ref = re.search(rb"/CIDToGIDMap\s+(\d+)\s+0\s+R", pdf_data)
    if not ctg_ref:
        return None
    oid = int(ctg_ref.group(1))
    # Ищем "N 0 obj" и следующий stream (обход бага regex с 13/16)
    pat = rf"{oid}\s+0\s+obj".encode()
    m = re.search(pat, pdf_data)
    if not m:
        return None
    chunk = pdf_data[m.start() : m.start() + 300]
    stream_m = re.search(rb">>\s*stream\r?\n", chunk)
    if not stream_m:
        return None
    len_m = re.search(rb"/Length\s+(\d+)", chunk)
    if not len_m:
        return None
    stream_len = int(len_m.group(1))
    stream_start = m.start() + stream_m.end()
    if stream_start + stream_len > len(pdf_data):
        return None
    raw = pdf_data[stream_start : stream_start + stream_len]
    try:
        dec = _decompress_stream(raw)
    except zlib.error:
        return None
    out: dict[int, int] = {}
    for i in range(0, len(dec), 2):
        if i + 2 <= len(dec):
            out[i // 2] = int.from_bytes(dec[i : i + 2], "big")
    return out


def _compress_stream(data: bytes) -> bytes:
    return zlib.compress(data, 9)


def _find_font_stream(data: bytes) -> tuple[int, int, int, bytes] | None:
    """(stream_start, stream_len, len_num_start, decompressed_data)"""
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(3))
        stream_start = m.end()
        len_num_start = m.start(3)
        dict_part = m.group(2) + m.group(4)
        if b"/Length1" not in dict_part or stream_len < 500:
            continue
        if stream_start + stream_len > len(data):
            continue
        raw = data[stream_start : stream_start + stream_len]
        try:
            dec = _decompress_stream(raw)
        except zlib.error:
            continue
        if dec[:4] in (b"\x00\x01\x00\x00", b"OTTO"):
            return stream_start, stream_len, len_num_start, dec
    return None


def _copy_glyph(base_font, donor_font, donor_gid: int, method: str = "deepcopy") -> bool:
    """Скопировать глиф donor_gid из donor в конец base."""
    import copy
    base_order = list(base_font.getGlyphOrder())
    donor_order = donor_font.getGlyphOrder()
    if donor_gid >= len(donor_order):
        return False
    donor_gname = donor_order[donor_gid]
    base_glyf = base_font.get("glyf")
    donor_glyf = donor_font.get("glyf")
    if donor_gname not in donor_glyf:
        return False
    new_gname = f"gid{len(base_order)}"
    base_order.append(new_gname)
    base_font.setGlyphOrder(base_order)
    donor_set = donor_font.getGlyphSet()
    donor_glyph_raw = donor_set.get(donor_gname)
    donor_head = donor_font.get("head")
    base_head = base_font.get("head")
    donor_upem = donor_head.unitsPerEm if donor_head else 1000
    base_upem = base_head.unitsPerEm if base_head else donor_upem

    def _try_pen() -> bool:
        try:
            from fontTools.pens.ttGlyphPen import TTGlyphPen
            from fontTools.pens.transformPen import TransformPen
            pen = TTGlyphPen(None)
            if abs(base_upem - donor_upem) > 1 and donor_upem:
                t = (base_upem / donor_upem, 0, 0, base_upem / donor_upem, 0, 0)
                transform_pen = TransformPen(pen, t)
                donor_glyph_raw.draw(transform_pen)
            else:
                donor_glyph_raw.draw(pen)
            base_glyf[new_gname] = pen.glyph()
            return True
        except Exception:
            return False

    if method in ("pen", "decompose") and donor_glyph_raw is not None:
        if not _try_pen():
            base_glyf[new_gname] = copy.deepcopy(donor_glyf[donor_gname])
    else:
        base_glyf[new_gname] = copy.deepcopy(donor_glyf[donor_gname])
    base_hmtx = base_font.get("hmtx")
    donor_hmtx = donor_font.get("hmtx")
    if donor_hmtx and donor_gname in donor_hmtx.metrics:
        base_hmtx.metrics[new_gname] = donor_hmtx.metrics[donor_gname]
    else:
        base_hmtx.metrics[new_gname] = (500, 0)
    return True


def _replace_glyph_in_slot(
    base_font, donor_font,
    base_cid: int, base_ctg: dict[int, int],
    donor_cid: int, donor_ctg: dict[int, int],
    method: str = "pen",
) -> bool:
    """Заменить глиф в слоте base_cid на глиф донора (donor_cid).
    method: pen (default), deepcopy, decompose."""
    base_gid = base_ctg.get(base_cid)
    donor_gid = donor_ctg.get(donor_cid, donor_cid)
    if base_gid is None or donor_gid is None:
        return False
    if base_gid == 0:
        return False  # инъекция в GID=0 (.notdef) — рендерер игнорирует изменения
    base_order = base_font.getGlyphOrder()
    donor_order = donor_font.getGlyphOrder()
    if base_gid >= len(base_order) or donor_gid >= len(donor_order):
        return False
    base_gname = base_order[base_gid]
    donor_gname = donor_order[donor_gid]
    base_glyf = base_font.get("glyf")
    donor_glyf = donor_font.get("glyf")
    if base_glyf is None or donor_glyf is None:
        return False
    donor_set = donor_font.getGlyphSet()
    if donor_gname not in donor_set:
        return False
    donor_glyph_raw = donor_set[donor_gname]
    base_head = base_font.get("head")
    donor_head = donor_font.get("head")
    base_upem = getattr(base_head, "unitsPerEm", 1000) if base_head else 1000
    donor_upem = getattr(donor_head, "unitsPerEm", 1000) if donor_head else 1000
    scale = base_upem / donor_upem if donor_upem else 1.0

    def _try_pen() -> bool:
        try:
            from fontTools.pens.ttGlyphPen import TTGlyphPen
            from fontTools.pens.transformPen import TransformPen
            from fontTools.misc.transform import Transform
            pen = TTGlyphPen(None)
            if abs(scale - 1.0) > 0.001:
                t = Transform(scale, 0, 0, scale, 0, 0)
                transform_pen = TransformPen(pen, t)
                donor_glyph_raw.draw(transform_pen)
            else:
                donor_glyph_raw.draw(pen)
            base_glyf[base_gname] = pen.glyph()
            return True
        except Exception:
            return False

    def _try_deepcopy() -> bool:
        if donor_gname not in donor_glyf:
            return False
        try:
            import copy
            base_glyf[base_gname] = copy.deepcopy(donor_glyf[donor_gname])
            if abs(scale - 1.0) > 0.001:
                g = base_glyf[base_gname]
                if hasattr(g, "coordinates"):
                    g.coordinates = [(int(x * scale), int(y * scale)) for x, y in g.coordinates]
                if hasattr(g, "flags"):
                    pass
            return True
        except Exception:
            return False

    def _try_decompose() -> bool:
        """Как pen: draw() уже раскладывает составные глифы при отрисовке."""
        try:
            from fontTools.pens.ttGlyphPen import TTGlyphPen
            from fontTools.pens.transformPen import TransformPen
            from fontTools.misc.transform import Transform
            pen = TTGlyphPen(None)
            if abs(scale - 1.0) > 0.001:
                t = Transform(scale, 0, 0, scale, 0, 0)
                transform_pen = TransformPen(pen, t)
                donor_glyph_raw.draw(transform_pen)
            else:
                donor_glyph_raw.draw(pen)
            base_glyf[base_gname] = pen.glyph()
            return True
        except Exception:
            return False

    order = (["pen", "decompose", "deepcopy"] if method == "pen"
             else [method] if method in ("deepcopy", "decompose") else ["pen"])
    for m in order:
        if m == "pen" and _try_pen():
            break
        elif m == "deepcopy" and _try_deepcopy():
            break
        elif m == "decompose" and _try_decompose():
            break
    else:
        return False

    base_hmtx = base_font.get("hmtx")
    donor_hmtx = donor_font.get("hmtx")
    if donor_hmtx and donor_gname in donor_hmtx.metrics:
        w, lsb = donor_hmtx.metrics[donor_gname]
        if abs(base_upem - donor_upem) > 1 and donor_upem:
            w = int(w * base_upem / donor_upem)
        base_hmtx.metrics[base_gname] = (w, lsb)
    return True


def _patch_w_in_place(data: bytearray, cid_widths: dict[int, tuple[int, int]]) -> bool:
    """Обновить ширины для CIDs в /W — замена на месте (структура не меняется)."""
    w_m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", data, re.DOTALL)
    if not w_m:
        return False
    content = w_m.group(1).decode("latin-1")
    for cid, (old_w, new_w) in cid_widths.items():
        if old_w == new_w:
            continue
        # 1) Простой случай: "cid [old_w]"
        pat_single = re.compile(rf"(?<!\d){cid}\s+\[\s*{re.escape(str(old_w))}\s*\]")
        new_content, n = pat_single.subn(f"{cid} [{new_w}]", content, count=1)
        if n:
            content = new_content
            continue

        # 2) Групповой случай: "start [w1 w2 w3 ...]" где cid = start + idx
        for m in re.finditer(r"(?<!\d)(\d+)\s+\[([0-9\s]+)\]", content):
            start_cid = int(m.group(1))
            widths = m.group(2).split()
            idx = cid - start_cid
            if 0 <= idx < len(widths) and widths[idx] == str(old_w):
                widths[idx] = str(new_w)
                repl = f"{start_cid} [{' '.join(widths)}]"
                content = content[:m.start()] + repl + content[m.end():]
                break
    data[w_m.start(1) : w_m.end(1)] = content.encode("latin-1")
    return True


def _resolve_target(target_arg: str | None) -> Path:
    """Найти TARGET PDF: --target или первый существующий из TARGET_17."""
    if target_arg:
        p = Path(target_arg).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Не найден: {p}")
    for p in TARGET_17:
        if p.exists():
            return p
    raise FileNotFoundError("Не найден эталон 17.pdf. Укажите --target путь.pdf")


def _extract_id_from_pdf(pdf_path: Path) -> str | None:
    """Извлечь первый hex из /ID [<id1><id2>]."""
    data = pdf_path.read_bytes()
    m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>', data)
    return m.group(1).decode() if m else None


def _check_metadata(data: bytes) -> dict[str, str | None]:
    """Проверить метаданные PDF."""
    out = {}
    for key, pattern in [
        ("CreationDate", rb'/CreationDate\s*\(([^)]+)\)'),
        ("ModDate", rb'/ModDate\s*\(([^)]+)\)'),
        ("Producer", rb'/Producer\s*\(([^)]*)\)'),
        ("Creator", rb'/Creator\s*\(([^)]*)\)'),
        ("Title", rb'/Title\s*\(([^)]*)\)'),
    ]:
        m = re.search(pattern, data)
        out[key] = m.group(1).decode(errors="replace").strip() if m else None
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>', data)
    out["DocumentID"] = id_m.group(1).decode() if id_m else None
    return out


def find_best_base_pdf(
    fio_text: str,
    sbp_dir: Path,
    *,
    current_base_ctg: "dict[int, int] | None" = None,
    verbose: bool = True,
) -> "tuple[Path | None, set[str]]":
    """Найти PDF в sbp_dir с максимальным числом нативных заглавных глифов для FIO.

    Возвращает (путь, множество_доп_букв) или (None, set()) если лучше текущей базы нет.
    Буквы в возвращаемом множестве — те, что нативно есть в найденном PDF, но ОТСУТСТВУЮТ
    в текущей базе (13.pdf). Т.е. именно те буквы, которые нам даёт этот PDF «в подарок».
    """
    from vtb_cmap import _CID_CYRILLIC
    from find_reusable_cids import _get_cidtogid_map

    # CID → char для заглавного диапазона 021C-023B
    cid_to_uc: dict[int, str] = {
        int(cid_hex, 16): ch
        for ch, cid_hex in _CID_CYRILLIC.items()
        if ch.isupper() and 0x021C <= int(cid_hex, 16) <= 0x023B
    }

    # Заглавные буквы, нужные в ФИО
    needed_uc = {ch for ch in fio_text if ch.isupper() and ch in _CID_CYRILLIC}
    needed_cids = {int(_CID_CYRILLIC[ch], 16) for ch in needed_uc}

    # Базовые буквы из текущего базового PDF (13.pdf)
    base_native_cids: set[int] = set()
    if current_base_ctg:
        base_native_cids = {cid for cid, gid in current_base_ctg.items() if cid in cid_to_uc and gid > 0}

    # CIDs нужные, но отсутствующие в текущей базе
    missing_needed_cids = needed_cids - base_native_cids

    if not missing_needed_cids:
        if verbose:
            print(f"[auto-base] Все заглавные буквы уже есть в текущей базе: {''.join(sorted(needed_uc))}", flush=True)
        return None, set()

    if verbose:
        missing_chars = ''.join(cid_to_uc.get(c, '?') for c in sorted(missing_needed_cids))
        print(f"[auto-base] Ищу PDF с нативными глифами для: {missing_chars}", flush=True)

    # Исключаем донорский PDF (check (3).pdf) — он используется для инъекции глифов,
    # не должен быть базой (иначе Document ID донора используется дважды).
    donor_name = DONOR.name

    # Эталонные Y-координаты из базового PDF (13.pdf) для проверки совместимости layout.
    # Кандидаты с другим набором Y не смогут правильно пропатчить поля.
    def _get_tm_ys(pdf_bytes: bytes) -> "frozenset[float]":
        import re as _re, zlib as _zlib
        ys: set[float] = set()
        for m in _re.finditer(rb'\d+\s+0\s+obj.*?stream\r?\n(.*?)endstream', pdf_bytes, _re.DOTALL):
            try:
                dec = _zlib.decompress(m.group(1).lstrip(b'\r\n'))
            except Exception:
                dec = m.group(1)
            for tm in _re.finditer(rb'(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+Tm', dec):
                try:
                    ys.add(round(float(tm.group(6)), 1))
                except Exception:
                    pass
        return frozenset(ys)

    # Базовые Y из текущего target PDF (13.pdf) — кандидат должен совпадать полностью
    ref_bytes = (current_base_ctg and None) or None  # placeholder — вычислим через sbp_dir/../../../
    # Находим базовый 13.pdf для эталона Y-координат
    _ref_paths = [
        Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
        sbp_dir / "13-03-26_00-00 13.pdf",
    ]
    _ref_ys: "frozenset[float] | None" = None
    for _rp in _ref_paths:
        if _rp.exists():
            _ref_ys = _get_tm_ys(_rp.read_bytes())
            break

    # Ожидаемые Y-координаты плательщика и получателя (из 13.pdf layout).
    # Если get_field_align_raw находит Y, отличный от ожидаемого более чем на 15 pt, PDF небезопасен.
    _EXPECTED_PAYER_Y = 227.25   # из эталонного 13.pdf
    _EXPECTED_RECIPIENT_Y = 203.25
    _Y_SAFETY_TOL = 15.0

    # FIO-only слоты: CID → набор символов, которые «естественно» его используют (из vtb_cmap)
    _SLOT_NATURAL: "dict[int, frozenset[str]]" = {
        0x0221: frozenset({"Е", "е"}),
        0x0222: frozenset({"Ж", "ж"}),
        0x023F: frozenset({"Г", "г"}),
    }
    _FIO_SLOT_LIST = [0x0221, 0x0222, 0x023F]

    def _is_fully_renderable(ctg: "dict[int, int]") -> bool:
        """Вернуть True если все символы из fio_text можно рендерить через данный CIDToGIDMap.

        Символы с GID=0 могут быть инъецированы в FIO-only слоты (при условии что слот
        имеет GID>0 и не конфликтует с другим символом из ФИО).
        """
        from vtb_cmap import _CID_CYRILLIC, _CID_DIGIT
        _all_vtb: "dict[str, str]" = {**_CID_CYRILLIC, **_CID_DIGIT}

        fio_chars = frozenset(ch for ch in fio_text if ch.isalpha())

        # Собираем символы ФИО с GID=0 в данной базе
        blank_chars: list[str] = []
        for ch in sorted(fio_chars):
            cid_str = _all_vtb.get(ch)
            if not cid_str:
                continue
            cid = int(cid_str, 16)
            if ctg.get(cid, 0) == 0:
                blank_chars.append(ch)

        if not blank_chars:
            return True

        # Жадное распределение blank_chars по слотам
        used_slots: set[int] = set()
        for ch in blank_chars:
            placed = False
            for slot in _FIO_SLOT_LIST:
                if slot in used_slots:
                    continue
                if ctg.get(slot, 0) == 0:
                    continue  # слот без глифа — инъекция в него запрещена (GID=0)
                # Конфликт: другой символ из ФИО использует этот слот как родной
                other_naturals = _SLOT_NATURAL.get(slot, frozenset()) - {ch}
                if other_naturals & fio_chars:
                    continue
                used_slots.add(slot)
                placed = True
                break
            if not placed:
                return False
        return True

    best_path: "Path | None" = None
    best_extra: set[str] = set()
    best_score = 0
    best_is_real = False  # предпочитаем реальные чеки (дата в имени) над спец-файлами

    for pdf_path in sorted(sbp_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        if pdf_path.name == donor_name:
            continue  # пропускаем донора
        try:
            pdf_bytes_cand = pdf_path.read_bytes()
            ctg = _get_cidtogid_map(pdf_bytes_cand)
            if not ctg:
                continue
            # Проверяем layout-совместимость (Y-координаты полей должны совпадать с эталоном)
            if _ref_ys is not None:
                cand_ys = _get_tm_ys(pdf_bytes_cand)
                if cand_ys != _ref_ys:
                    continue  # несовместимый layout — пропускаем
            # Проверяем безопасность поля-детекции (get_field_align_raw не перепутал поля).
            # Если найденная Y для плательщика/получателя далека от ожидаемой → пропускаем.
            try:
                from vtb_sber_reference import get_field_align_raw as _gar
                _raw_y = _gar(pdf_path).get("y", {})
                _py = _raw_y.get("payer")
                _ry = _raw_y.get("recipient")
                if _py is not None and abs(_py - _EXPECTED_PAYER_Y) > _Y_SAFETY_TOL:
                    continue  # payer Y смещён → PDF небезопасен
                if _ry is not None and abs(_ry - _EXPECTED_RECIPIENT_Y) > _Y_SAFETY_TOL:
                    continue  # recipient Y смещён → PDF небезопасен
            except Exception:
                pass  # если fitz недоступен — не фильтруем по этому критерию
            # Проверяем что все символы ФИО (включая строчные) рендерятся в этом PDF
            if not _is_fully_renderable(ctg):
                continue  # хотя бы один символ ФИО не может быть ни нативным, ни инъецированным
            # Буквы, которые есть нативно в этом PDF И нужны в ФИО, НО отсутствуют в текущей базе
            extra_cids = {cid for cid in missing_needed_cids if ctg.get(cid, 0) > 0}
            score = len(extra_cids)
            # Предпочитаем: (1) больший score, (2) реальный чек (имя начинается с даты DD-MM-YY)
            is_real = bool(pdf_path.name[:2].isdigit() and pdf_path.name[2] == "-")
            better = (score > best_score) or (score == best_score and is_real and not best_is_real)
            if better and score > 0:
                best_score = score
                best_path = pdf_path
                best_extra = {cid_to_uc[cid] for cid in extra_cids if cid in cid_to_uc}
                best_is_real = is_real
        except Exception:
            continue

    if best_path and best_score > 0:
        if verbose:
            print(f"[auto-base] Лучший кандидат: {best_path.name}", flush=True)
            print(f"[auto-base] Нативные extra-буквы: {''.join(sorted(best_extra))} (+{best_score})", flush=True)
        return best_path, best_extra
    return None, set()


def _check_caps_mode(target_path: Path, payer: str, recipient: str) -> int:
    """Показать какие заглавные буквы в ФИО будут заглавными в PDF."""
    from vtb_cmap import _CID_CYRILLIC, _CID_DIGIT
    from find_reusable_cids import _get_cidtogid_map

    _all_vtb = {**_CID_CYRILLIC, **_CID_DIGIT}
    base_ctg = _get_cidtogid_map(target_path.read_bytes())
    fio_text = " ".join(filter(None, [payer, recipient]))
    fio_unis = {ord(ch) for ch in fio_text}

    _HARDCODED_CANDS: dict[int, int] = {0x0424: 0x0222, 0x0427: 0x023F, 0x042E: 0x0221}
    _FIO_ONLY_SLOTS = [0x0221, 0x0222, 0x023F]

    _cid_int_to_fio_chars: dict[int, set[int]] = {}
    for _ch, _cid_hex in _all_vtb.items():
        _cid_int_to_fio_chars.setdefault(int(_cid_hex, 16), set()).add(ord(_ch))

    def _needs_donor(ch: str) -> bool:
        cid_hex = _all_vtb.get(ch)
        if not cid_hex:
            return True
        cid = int(cid_hex, 16)
        if base_ctg is not None:
            gid = base_ctg.get(cid, 0)
            if gid == 0:
                return True
            if ord(ch) in _HARDCODED_CANDS:
                return True
            return False
        return False

    reuse_map: dict[int, int] = {}
    used_slots: set[int] = set()

    for target_uni, slot_cid in _HARDCODED_CANDS.items():
        if target_uni not in fio_unis:
            continue
        conflict = _cid_int_to_fio_chars.get(slot_cid, set()) & fio_unis - {target_uni}
        if not conflict:
            reuse_map[target_uni] = slot_cid
            used_slots.add(slot_cid)

    _fio_first_pos: dict[int, int] = {}
    for _pos, _fio_ch in enumerate(fio_text):
        _u = ord(_fio_ch)
        if _u not in _fio_first_pos:
            _fio_first_pos[_u] = _pos

    extra_unis = sorted(
        {ord(ch) for ch in fio_text if ord(ch) in DONOR_CIDS and ord(ch) not in reuse_map and _needs_donor(ch)},
        key=lambda u: _fio_first_pos.get(u, 99999),
    )

    for uni in extra_unis:
        for slot in _FIO_ONLY_SLOTS:
            if slot in used_slots:
                continue
            conflict = _cid_int_to_fio_chars.get(slot, set()) & fio_unis - {uni}
            if not conflict:
                reuse_map[uni] = slot
                used_slots.add(slot)
                break

    results: list[tuple[str, str]] = []
    for ch in fio_text:
        if not ch.isupper():
            continue
        uni = ord(ch)
        if uni in reuse_map:
            results.append((ch, "✅ ЗАГЛАВНАЯ"))
        elif _needs_donor(ch):
            results.append((ch, "⚠️  строчная (нет слота)"))
        else:
            results.append((ch, "✅ ЗАГЛАВНАЯ (нативный глиф)"))

    seen: set[str] = set()
    print(f"\nФИО плательщика : {payer}")
    print(f"ФИО получателя  : {recipient}")
    print(f"Доступно слотов : {3 - len(used_slots)} из 3")
    print(f"\nРезультат для каждой заглавной буквы:")
    any_warn = False
    for ch, status in results:
        if ch in seen:
            continue
        seen.add(ch)
        print(f"  {ch}  {status}")
        if "строчная" in status:
            any_warn = True

    if any_warn:
        print(f"\n  Почему не хватает слотов:")
        slot_names = {0x0221: "Е(0221)", 0x0222: "Ж(0222)", 0x023F: "г(023F)"}
        blocking_fio_chars: set[str] = set()
        for slot in _FIO_ONLY_SLOTS:
            if slot in used_slots:
                used_by = [chr(u) for u, s in reuse_map.items() if s == slot]
                print(f"    Слот {slot_names[slot]}: занят буквой {''.join(used_by)}")
            else:
                conflict = sorted(chr(u) for u in _cid_int_to_fio_chars.get(slot, set()) if u in fio_unis)
                if conflict:
                    blocking_fio_chars.update(conflict)
                    # Определяем в чьём ФИО эта буква
                    sources = []
                    if payer and any(c in payer for c in conflict):
                        sources.append("плательщик")
                    if recipient and any(c in recipient for c in conflict):
                        sources.append("получатель")
                    src = "/".join(sources) if sources else "ФИО"
                    print(f"    Слот {slot_names[slot]}: заблокирован буквами {', '.join(conflict)} (в ФИО {src})")
        if blocking_fio_chars:
            print(f"\n  💡 Совет: уберите '{', '.join(sorted(blocking_fio_chars))}' из ФИО чтобы освободить слоты.")
            print(f"     Например: замените инициал 'Ж.' → 'Д.' или 'Магомед' → 'Мамед'")
    else:
        print(f"\n  ✅ Все заглавные буквы будут заглавными")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Добавить Ф,Ч,Ю из check(3) в 13-03-26")
    ap.add_argument("-o", "--output", default="receipt_13_03_with_glyphs.pdf")
    ap.add_argument("--payer", default="Филипп Юсаев Ч.")
    ap.add_argument("--recipient", default="Филипп Юсаев Ч.")
    ap.add_argument("--phone", default=None)
    ap.add_argument("--bank", default=None, help="Банк (напр. Т-Банк)")
    ap.add_argument("--amount", type=int, default=None, help="Сумма в рублях (напр. 10000)")
    ap.add_argument("--operation-id", default=None, help="ID операции СБП (32 hex-символа); если не задан, генерируется через gen_sbp_operation_id")
    ap.add_argument("--time", default=None, help="Время перевода HH:MM (напр. 19:31)")
    ap.add_argument("--date", default="13.03.2026", help="Дата DD.MM.YYYY")
    ap.add_argument("--target", default=None, help="База PDF (по умолчанию 13.pdf для структуры, 17.pdf для проверки)")
    ap.add_argument("--id-from", default=None, help="PDF, из которого взять Document ID; затем меняется 1 символ для уникальности")
    ap.add_argument("--replace", action="store_true", help="REPLACE: перезаписать существующие CID (Ф,Ч,Ю) — без изменений структуры PDF")
    ap.add_argument("--hybrid-safe", action="store_true",
                    help="Гибрид: все Ф/Ч/Ю через REPLACE (0222/023F/0221), CIDToGIDMap=эталон.")
    ap.add_argument("--preserve-w", action="store_true",
                    help="Не обновлять /W (сохраняет /W=эталон, для bot-совместимости).")
    ap.add_argument("--method", default="pen", choices=["pen", "deepcopy", "decompose"],
                    help="Способ копирования глифов для REPLACE: pen (по умол.), deepcopy, decompose")
    ap.add_argument("--check-caps", action="store_true",
                    help="Проверить какие заглавные буквы в ФИО будут заглавными (не генерировать PDF)")
    ap.add_argument("--auto-base", action="store_true",
                    help="Автоматически выбрать лучший базовый PDF из --base-dir с максимумом нативных заглавных букв для ФИО")
    ap.add_argument("--base-dir", default=None,
                    help="Папка для поиска базового PDF (по умол. база_чеков/vtb/СБП)")
    args = ap.parse_args()

    try:
        from fontTools.ttLib import TTFont
    except ImportError as e:
        print(f"[ERROR] pip install fonttools: {e}", file=sys.stderr)
        return 1

    donor_path = DONOR.resolve()
    target_path = None
    if args.target:
        target_path = Path(args.target).expanduser().resolve()
    else:
        for p in TARGET_13:
            if p.exists():
                target_path = p.resolve()
                break
        if target_path is None:
            for p in TARGET_17:
                if p.exists():
                    target_path = p.resolve()
                    break
        if target_path is None:
            target_path = TARGET.resolve()
    if not target_path.exists():
        print(f"[ERROR] Не найден: {target_path}", file=sys.stderr)
        return 1
    if not donor_path.exists():
        print(f"[ERROR] Не найден: {donor_path}", file=sys.stderr)
        return 1

    # --- --auto-base: выбираем лучший базовый PDF из базы чеков ---
    if args.auto_base:
        sbp_dir = Path(args.base_dir).expanduser().resolve() if args.base_dir else (BASE / "база_чеков" / "vtb" / "СБП")
        if sbp_dir.is_dir():
            from find_reusable_cids import _get_cidtogid_map as _ctg_for_autobase
            cur_ctg = _ctg_for_autobase(target_path.read_bytes())
            fio_for_search = " ".join(filter(None, [args.payer, args.recipient]))
            best_base, extra_chars = find_best_base_pdf(fio_for_search, sbp_dir, current_base_ctg=cur_ctg, verbose=True)
            if best_base:
                target_path = best_base
                print(f"[auto-base] Используем базу: {target_path.name}", file=sys.stderr)
            else:
                print(f"[auto-base] Текущая база оптимальна: {target_path.name}", file=sys.stderr)
        else:
            print(f"[WARN] --base-dir не найден: {sbp_dir}", file=sys.stderr)

    # --- --check-caps: диагностика заглавных букв до генерации PDF ---
    if args.check_caps:
        return _check_caps_mode(target_path, args.payer, args.recipient)

    tgt_data = bytearray(target_path.read_bytes())
    src_data = donor_path.read_bytes()

    tgt_font_info = _find_font_stream(bytes(tgt_data))
    src_font_info = _find_font_stream(src_data)
    if not tgt_font_info or not src_font_info:
        print("[ERROR] Font stream не найден", file=sys.stderr)
        return 1

    tgt_stream_start, tgt_stream_len, tgt_len_pos, tgt_font_bytes = tgt_font_info
    _, _, _, src_font_bytes = src_font_info

    base_font = TTFont(BytesIO(tgt_font_bytes))
    donor_font = TTFont(BytesIO(src_font_bytes))
    cid_to_gid = _get_cidtogid_map(src_data)
    if not cid_to_gid:
        print("[WARN] CIDToGIDMap не найден, пробуем CID=GID", file=sys.stderr)
        cid_to_gid = {cid: cid for cid in DONOR_CIDS.values()}

    from vtb_patch_from_config import _parse_cid_widths
    from copy_font_cmap import _find_font_and_tounicode, _parse_tounicode_from_stream
    base_w = _parse_cid_widths(bytes(tgt_data))
    _, base_tu, _ = _find_font_and_tounicode(tgt_data)
    base_uni_cid = _parse_tounicode_from_stream(base_tu) if base_tu else {}
    donor_order = donor_font.getGlyphOrder()
    donor_n = len(donor_order)
    ctg_donor = _get_cidtogid_map(src_data)
    base_ctg = _get_cidtogid_map(bytes(tgt_data))

    uni_to_new_cid: dict[int, str] = {}
    _case_fallback_unis: set[int] = set()  # записи uni_to_new_cid добавленные через case-fallback (не менять ToUnicode)
    new_cid_widths: list[tuple[int, int]] = []
    w_patch: dict[int, tuple[int, int]] = {}  # REPLACE: cid -> (old_w, new_w)

    if args.replace:
        from find_reusable_cids import find_reusable
        if args.hybrid_safe:
            from vtb_cmap import _CID_CYRILLIC, _CID_DIGIT
            _all_vtb = {**_CID_CYRILLIC, **_CID_DIGIT}
            # Обратный маппинг: CID (int) → множество Unicode code points, использующих его
            _cid_int_to_fio_chars: dict[int, set[int]] = {}
            for _ch, _cid_hex in _all_vtb.items():
                _cid_int_to_fio_chars.setdefault(int(_cid_hex, 16), set()).add(ord(_ch))

            fio_text = " ".join(filter(None, [args.payer, args.recipient]))
            fio_unis = {ord(ch) for ch in fio_text}

            # Кандидаты safe-слотов: {target_uni: slot_cid}
            # Слот используем ТОЛЬКО если: (a) буква нужна в ФИО, (b) нет конфликта (другая буква ФИО не использует тот же CID)
            _HARDCODED_CANDS = {0x0424: 0x0222, 0x0427: 0x023F, 0x042E: 0x0221}
            reuse_map: dict[int, int] = {}
            used_slots: set[int] = set()

            for target_uni, slot_cid in _HARDCODED_CANDS.items():
                if target_uni not in fio_unis:
                    continue  # буква не нужна
                # Проверяем: другие буквы ФИО используют тот же CID?
                conflict = _cid_int_to_fio_chars.get(slot_cid, set()) & fio_unis - {target_uni}
                if conflict:
                    cnames = [chr(u) for u in conflict]
                    print(f"[INFO] hybrid-safe: слот 0x{slot_cid:04X} пропущен — конфликт с {cnames} из ФИО", file=sys.stderr)
                    continue
                reuse_map[target_uni] = slot_cid
                used_slots.add(slot_cid)

            # Дополнительные буквы из ФИО, чьих глифов НЕТ в базовом шрифте.
            # Используем CIDToGIDMap: GID=0 → пустой нотдеф-слот (буква отсутствует).
            def _needs_donor_glyph(ch: str) -> bool:
                """True если буква отсутствует в базовом шрифте (CID→GID=0) ИЛИ
                имеет неправильный глиф (буква в HARDCODED_CANDS, но не получила свой слот)."""
                cid_hex = _all_vtb.get(ch)
                if not cid_hex:
                    return True
                cid = int(cid_hex, 16)
                if base_ctg is not None:
                    gid = base_ctg.get(cid, 0)
                    if gid == 0:
                        return True  # глиф отсутствует → нужен донор
                    # Буква в HARDCODED_CANDS (Ф, Ч, Ю): её vtb_cmap CID содержит неправильный
                    # глиф в базовом шрифте, поэтому нужен слот даже если GID > 0.
                    if ord(ch) in _HARDCODED_CANDS and ord(ch) not in reuse_map:
                        return True
                    return False
                return cid not in base_w  # fallback если нет CIDToGIDMap

            # Сортируем по первому вхождению в тексте ФИО: первые буквы имён важнее.
            _fio_first_pos: dict[int, int] = {}
            for _pos, _fio_ch in enumerate(fio_text):
                _u = ord(_fio_ch)
                if _u not in _fio_first_pos:
                    _fio_first_pos[_u] = _pos

            extra_unis = sorted(
                {
                    ord(ch) for ch in fio_text
                    if ord(ch) in DONOR_CIDS
                    and ord(ch) not in reuse_map  # уже получил hardcoded слот
                    and _needs_donor_glyph(ch)
                },
                key=lambda u: _fio_first_pos.get(u, 99999),
            )

            if extra_unis:
                need_all = frozenset(fio_unis)
                # Известные FIO-only слоты в 13.pdf (Е→0x0221, Ж→0x0222, г→0x023F).
                # Пробуем их все для каждой буквы, приоритет — порядок в extra_unis (первые буквы имён).
                _FIO_ONLY_SLOTS = [0x0221, 0x0222, 0x023F]
                for uni in extra_unis:
                    if uni in reuse_map:
                        continue
                    for slot in _FIO_ONLY_SLOTS:
                        if slot in used_slots:
                            continue
                        if (base_ctg or {}).get(slot, 0) == 0:
                            continue  # GID=0 в базе — инъекция в .notdef запрещена
                        conflict = _cid_int_to_fio_chars.get(slot, set()) & fio_unis - {uni}
                        if not conflict:
                            reuse_map[uni] = slot
                            used_slots.add(slot)
                            break
                # Для оставшихся без слота — пробуем unused CIDs через find_reusable.
                remaining = [u for u in extra_unis if u not in reuse_map]
                if remaining:
                    _, dyn_map = find_reusable(target_path, target_unis=remaining, need_uni=need_all)
                    for uni, slot in dyn_map.items():
                        conflict = _cid_int_to_fio_chars.get(slot, set()) & fio_unis - {uni}
                        if slot not in used_slots and not conflict:
                            reuse_map[uni] = slot
                            used_slots.add(slot)
                missing = [chr(u) for u in extra_unis if u not in reuse_map]
                if missing:
                    print(f"[WARN] hybrid-safe: нет безопасных слотов для {missing}. Буквы используют базовый шрифт.", file=sys.stderr)

            # Case-fallback: для букв без слота ищем вариант другого регистра с GID>0.
            # Пример: М (uppercase, GID=0) → м (lowercase, GID=45) → рендерится нормально.
            # Пример: б (lowercase, GID=0) → Б (uppercase, GID=26) → рендерится как Б.
            # Изменение только в TJ-потоке (uni_to_new_cid), CIDToGIDMap не трогаем.
            for uni in extra_unis:
                if uni in reuse_map or uni in uni_to_new_cid:
                    continue
                ch = chr(uni)
                # Пробуем вариант другого регистра
                other_case_uni = ord(ch.lower()) if ch.isupper() else (ord(ch.upper()) if ch.islower() else None)
                if other_case_uni and other_case_uni != uni:
                    alt_cid_hex = _all_vtb.get(chr(other_case_uni))
                    if alt_cid_hex:
                        alt_cid = int(alt_cid_hex, 16)
                        alt_gid = (base_ctg or {}).get(alt_cid, 0)
                        if alt_gid > 0:  # у альтернативного варианта есть глиф
                            uni_to_new_cid[uni] = alt_cid_hex
                            _case_fallback_unis.add(uni)  # не обновлять ToUnicode для этого CID
                            print(f"[INFO] hybrid-safe: {ch!r} → {chr(other_case_uni)!r} CID {alt_cid_hex} (case-fallback, GID {alt_gid})", file=sys.stderr)
                            continue
                # Нет ни слота, ни fallback → буква рендерится как пустой глиф
                print(f"[WARN] hybrid-safe: {ch!r} — нет глифа и нет fallback. Буква будет пустой.", file=sys.stderr)
        else:
            _, reuse_map = find_reusable(target_path)
        if not reuse_map:
            if args.hybrid_safe and not extra_unis:
                # Все буквы уже нативные в базовом PDF — инъекция не нужна, продолжаем патчинг
                print(f"[INFO] hybrid-safe: все буквы ФИО нативные в базе, инъекция глифов не требуется.", file=sys.stderr)
            else:
                print(f"[ERROR] REPLACE: слоты не найдены. Используйте ADD.", file=sys.stderr)
                return 1
        for target_uni, base_cid in reuse_map.items():
            donor_cid = DONOR_CIDS.get(target_uni)
            if donor_cid is None:
                continue
            if _replace_glyph_in_slot(base_font, donor_font, base_cid, base_ctg or {}, donor_cid, ctg_donor or {}, method=args.method):
                uni_to_new_cid[target_uni] = f"{base_cid:04X}"
        scale = 0.49
        for ref_uni in (0x0438, 0x043F, 0x043B):
            cid_h = base_uni_cid.get(ref_uni)
            if not cid_h:
                continue
            cid = int(cid_h, 16)
            base_ref_w = base_w.get(cid)
            donor_gid = (ctg_donor or {}).get(cid, cid)
            if base_ref_w and 0 <= donor_gid < len(donor_order):
                donor_ref_w = donor_font.get("hmtx").metrics.get(donor_order[donor_gid], (0, 0))[0]
                if donor_ref_w > 0:
                    scale = base_ref_w / donor_ref_w
                    break
        for target_uni, base_cid in reuse_map.items():
            donor_cid = DONOR_CIDS.get(target_uni)
            if donor_cid is None:
                continue
            donor_gid = (ctg_donor or {}).get(donor_cid, donor_cid)
            if donor_gid < len(donor_order):
                w_donor = donor_font.get("hmtx").metrics.get(donor_order[donor_gid], (500, 0))[0]
                new_w = int(w_donor * scale)
                old_w = base_w.get(base_cid)
                if old_w is None:
                    # CID не был в /W (GID-0 слот или другой пустой CID) → добавляем новую запись
                    new_cid_widths.append((base_cid, new_w))
                elif old_w != new_w:
                    w_patch[base_cid] = (old_w, new_w)
        # hybrid_safe: Ю теперь REPLACE(0221), ADD-блок не нужен.
        # CIDToGIDMap остаётся идентичен эталону — критично для прохождения бота.
    else:
        # ADD mode: добавляем только глифы для букв из нового ФИО (не весь алфавит).
        fio_text_add = " ".join(filter(None, [args.payer, args.recipient]))
        needed_unis_add = {ord(ch) for ch in fio_text_add if ord(ch) in DONOR_CIDS}
        donor_cids_to_add = {uni: cid for uni, cid in DONOR_CIDS.items() if uni in needed_unis_add} \
            if needed_unis_add else DONOR_CIDS  # fallback на весь DONOR_CIDS если нет FIO
        n_base = len(base_font.getGlyphOrder())
        for uni, cid in donor_cids_to_add.items():
            donor_gid = cid_to_gid.get(cid, cid)
            if donor_gid >= donor_n:
                print(f"[WARN] CID 0x{cid:03X} -> GID {donor_gid} вне диапазона 0..{donor_n - 1}", file=sys.stderr)
                continue
            if _copy_glyph(base_font, donor_font, donor_gid, method=args.method):
                uni_to_new_cid[uni] = f"{n_base:04X}"
                n_base += 1
        if not uni_to_new_cid:
            print("[ERROR] Не удалось добавить глифы", file=sys.stderr)
            return 1
        base_hmtx = base_font.get("hmtx")
        donor_hmtx = donor_font.get("hmtx")
        scale = 0.49
        for ref_uni in (0x0438, 0x043F, 0x043B):
            cid_h = base_uni_cid.get(ref_uni)
            if not cid_h:
                continue
            cid = int(cid_h, 16)
            base_ref_w = base_w.get(cid)
            donor_gid = (ctg_donor or {}).get(cid, cid)
            if base_ref_w and 0 <= donor_gid < len(donor_order):
                donor_ref_w = donor_hmtx.metrics.get(donor_order[donor_gid], (0, 0))[0]
                if donor_ref_w > 0:
                    scale = base_ref_w / donor_ref_w
                    break
        for uni, cid_hex in uni_to_new_cid.items():
            cid = int(cid_hex, 16)
            gname = f"gid{cid}"
            w_raw = base_hmtx.metrics.get(gname, (500, 0))[0]
            w = int(w_raw * scale)
            new_cid_widths.append((cid, w))

    if not uni_to_new_cid and not args.replace:
        print("[ERROR] Не удалось получить маппинг Ф,Ч,Ю", file=sys.stderr)
        return 1

    out_buf = BytesIO()
    base_font.save(out_buf)
    new_font_bytes = out_buf.getvalue()
    base_font.close()
    donor_font.close()

    new_compressed = _compress_stream(new_font_bytes)
    delta = len(new_compressed) - tgt_stream_len
    tgt_data[tgt_stream_start : tgt_stream_start + tgt_stream_len] = new_compressed
    tgt_data[tgt_len_pos : tgt_len_pos + len(str(tgt_stream_len))] = str(len(new_compressed)).encode()
    if delta != 0:
        xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", tgt_data)
        if xref_m:
            entries = bytearray(xref_m.group(3))
            for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                offset = int(em.group(1))
                if offset > tgt_stream_start:
                    entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
            tgt_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
        startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", tgt_data)
        if startxref_m and tgt_stream_start < int(startxref_m.group(1)):
            p = startxref_m.start(1)
            old_p = int(startxref_m.group(1))
            tgt_data[p : p + len(str(old_p))] = str(old_p + delta).encode()

    from copy_font_cmap import _parse_tounicode_from_stream, _find_font_and_tounicode, _build_tounicode_stream

    def _add_tounicode_entries(dec: bytes, cid_uni_pairs: list[tuple[int, int]]) -> bytes:
        """Добавить beginbfchar блок в существующий CMap (сохраняет структуру эталона)."""
        block = b"\n" + str(len(cid_uni_pairs)).encode() + b" beginbfchar\n"
        for cid, uni in cid_uni_pairs:
            block += f"<{cid:04X}> <{uni:04X}>\n".encode()
        block += b"endbfchar\n"
        endcmap = dec.find(b"endcmap")
        if endcmap < 0:
            return dec
        return dec[:endcmap] + block + dec[endcmap:]

    _, tgt_tu, tgt_info = _find_font_and_tounicode(bytes(tgt_data))
    if tgt_tu:
        ti = tgt_info.get("tounicode", {})
        if ti:
            tu_start = ti["stream_start"]
            tu_len = ti["stream_len"]
            tu_len_pos = ti.get("len_num_start", 0)
            if args.replace and reuse_map:
                # REPLACE: добавить маппинги в существующий stream (сохраняет beginbfrange эталона).
                # case-fallback записи НЕ добавляем в ToUnicode: их CID уже имеет правильный маппинг,
                # и перезапись сломала бы статичный текст (напр. к→К в "плательщика").
                try:
                    dec = _decompress_stream(bytes(tgt_data[tu_start : tu_start + tu_len]))
                    cid_uni_pairs = [(int(c, 16), u) for u, c in uni_to_new_cid.items()
                                     if u not in _case_fallback_unis]
                    new_dec = _add_tounicode_entries(dec, cid_uni_pairs)
                    new_tu = _compress_stream(new_dec)
                except (zlib.error, KeyError):
                    uni_to_cid = _parse_tounicode_from_stream(tgt_tu)
                    reuse_cids = {int(c, 16) for _u, c in uni_to_new_cid.items()
                                  if _u not in _case_fallback_unis}
                    uni_to_cid = {u: c for u, c in uni_to_cid.items() if int(c, 16) not in reuse_cids}
                    for uni, cid in uni_to_new_cid.items():
                        if uni not in _case_fallback_unis:
                            uni_to_cid[uni] = cid
                    new_tu = _build_tounicode_stream(uni_to_cid)
            else:
                uni_to_cid = _parse_tounicode_from_stream(tgt_tu)
                if args.replace and reuse_map:
                    reuse_cids = {int(c, 16) for _u, c in uni_to_new_cid.items()
                                  if _u not in _case_fallback_unis}
                    uni_to_cid = {u: c for u, c in uni_to_cid.items() if int(c, 16) not in reuse_cids}
                for uni, cid in uni_to_new_cid.items():
                    if uni not in _case_fallback_unis:
                        uni_to_cid[uni] = cid
                new_tu = _build_tounicode_stream(uni_to_cid)
        ti = tgt_info.get("tounicode", {})
        if ti:
            tu_start = ti["stream_start"]
            tu_len = ti["stream_len"]
            tu_len_pos = ti.get("len_num_start", 0)
            tgt_data[tu_start : tu_start + tu_len] = new_tu
            delta_tu = len(new_tu) - tu_len
            tgt_data[tu_len_pos : tu_len_pos + len(str(tu_len))] = str(len(new_tu)).encode()
            if delta_tu != 0:
                xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", tgt_data)
                if xref_m:
                    entries = bytearray(xref_m.group(3))
                    for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                        offset = int(em.group(1))
                        if offset > tu_start:
                            entries[em.start(1) : em.start(1) + 10] = f"{offset + delta_tu:010d}".encode()
                    tgt_data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
                startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", tgt_data)
                if startxref_m and tu_start < int(startxref_m.group(1)):
                    p = startxref_m.start(1)
                    old_p = int(startxref_m.group(1))
                    tgt_data[p : p + len(str(old_p))] = str(old_p + delta_tu).encode()

    # Сохраняем ИСХОДНЫЕ ширины /W до замены глифов (нужны для корректного расчёта scale)
    from vtb_patch_from_config import _parse_cid_widths as _pcw_orig
    original_cid_widths = _pcw_orig(bytes(tgt_data))

    if args.replace and w_patch and not args.preserve_w:
        _patch_w_in_place(tgt_data, w_patch)
    if new_cid_widths:
        w_m = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", tgt_data, re.DOTALL)
        if w_m:
            widths_str = " ".join(str(w) for _, w in new_cid_widths)
            first_cid = new_cid_widths[0][0]
            insert = f" {first_cid} [{widths_str}]".encode()
            tail = w_m.group(1).rstrip()
            new_content = tail + insert
            tgt_data[w_m.start(1) : w_m.end(1)] = new_content
        cid_to_gid_patch = {cid: cid for cid, _ in new_cid_widths}
        _find_and_patch_cidtogid(tgt_data, cid_to_gid_patch)

    from vtb_patch_from_config import patch_from_values
    from vtb_test_generator import update_creation_date

    phone = args.phone or f"+7 ({random.randint(900,999)}) {random.randint(100,999)}-{random.randint(10,99)}-{random.randint(10,99)}"

    if args.time:
        date_str = f"{args.date}, {args.time}"
        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y, %H:%M")
            meta_date = dt.strftime("D:%Y%m%d%H%M00+03'00'")
        except ValueError:
            date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
            meta_date = datetime.now().strftime("D:%Y%m%d%H%M00+03'00'")
    else:
        date_str = datetime.now().strftime("%d.%m.%Y, %H:%M")
        meta_date = datetime.now().strftime("D:%Y%m%d%H%M00+03'00'")

    id_from_pdf = _extract_id_from_pdf(Path(args.id_from)) if args.id_from else None

    temp_pdf = BASE / ".temp_13_03_mod.pdf"
    temp_pdf.write_bytes(tgt_data)

    custom_uni_to_cid = dict(base_uni_cid)
    custom_uni_to_cid.update(uni_to_new_cid)

    operation_id = args.operation_id
    if operation_id is None and args.date and args.time:
        try:
            from datetime import datetime as _dt
            from vtb_cmap import gen_sbp_operation_id
            dt = _dt.strptime(f"{args.date}, {args.time}", "%d.%m.%Y, %H:%M")
            op_time_moscow = f"{dt.hour:02d}:{dt.minute:02d}"
            operation_id = gen_sbp_operation_id(
                dt.date(), op_time_moscow,
                direction="A",
                recipient_bank=args.bank or "",
            )
        except Exception:
            pass

    try:
        out = patch_from_values(
            tgt_data,
            temp_pdf,
            date_str=date_str,
            payer=args.payer,
            recipient=args.recipient,
            phone=phone,
            bank=args.bank,
            amount=args.amount,
            operation_id=operation_id,
            keep_metadata=True,
            override_uni_to_cid=custom_uni_to_cid,
            original_cid_widths=original_cid_widths,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        temp_pdf.unlink(missing_ok=True)
        return 1

    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', out_arr)
    if id_m:
        if id_from_pdf:
            # Явно передан --id-from: используем тот ID (меняем 1 символ для уникальности)
            hex1 = id_from_pdf if isinstance(id_from_pdf, str) else id_from_pdf.decode()
            c = hex1[-1]
            idx = "0123456789ABCDEF".find(c.upper())
            new_c = "0123456789ABCDEF"[(idx + 1) % 16]
            new1 = hex1[:-1] + new_c
            id_method = f"из {args.id_from}, 1 символ изменён"
        else:
            # Путь Б: берём /ID из базового PDF и меняем ровно 1 символ.
            # Это критично: бот помечает PDF как подделку если /ID изменён полностью.
            base_pdf_bytes = target_path.read_bytes()
            base_id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', base_pdf_bytes)
            if base_id_m:
                hex1 = base_id_m.group(1).decode().upper()
                c = hex1[-1]
                idx = "0123456789ABCDEF".find(c)
                new_c = "0123456789ABCDEF"[(idx + 1) % 16]
                new1 = hex1[:-1] + new_c
                id_method = f"из {target_path.name}, 1 символ изменён"
            else:
                import os as _os
                new1 = _os.urandom(16).hex().upper()
                id_method = "os.urandom(16) — резервный (ID не найден в базе)"
        slot_len = id_m.end(1) - id_m.start(1)
        new_enc = new1.encode().ljust(slot_len)[:slot_len]
        out_arr[id_m.start(1) : id_m.end(1)] = new_enc
        out_arr[id_m.start(2) : id_m.end(2)] = new_enc

    # Автоматически патчим ToUnicode с актуальным reuse_map (для корректного копирования текста).
    # Это заменяет вызов add_tounicode_cyrillic.py с хардкодом Ф/Ч/Ю.
    if args.replace and reuse_map:
        try:
            from add_tounicode_cyrillic import find_tounicode_stream, _parse_cmap_to_cid_uni
            import zlib as _zlib2
            tu = find_tounicode_stream(bytes(out_arr))
            if tu:
                tu_start, tu_len, len_pos = tu
                raw_tu = bytes(out_arr[tu_start:tu_start + tu_len])
                dec_tu = _zlib2.decompress(raw_tu)
                existing_cid_to_uni = _parse_cmap_to_cid_uni(dec_tu)
                # Добавляем только те CID→Unicode которых ещё нет
                new_entries = []
                for target_uni, slot_cid in reuse_map.items():
                    if slot_cid not in existing_cid_to_uni:
                        new_entries.append((slot_cid, target_uni))
                if new_entries:
                    bfchar_block = f"\n{len(new_entries)} beginbfchar\n".encode()
                    for cid, uni in new_entries:
                        bfchar_block += f"<{cid:04X}> <{uni:04X}>\n".encode()
                    bfchar_block += b"endbfchar\n"
                    # Вставляем перед "endcmap"
                    insert_at = dec_tu.rfind(b"endcmap")
                    if insert_at != -1:
                        new_dec_tu = dec_tu[:insert_at] + bfchar_block + dec_tu[insert_at:]
                        new_raw_tu = _zlib2.compress(new_dec_tu)
                        delta = len(new_raw_tu) - tu_len
                        out_arr[tu_start:tu_start + tu_len] = new_raw_tu
                        # Обновляем /Length
                        old_len_str = str(tu_len).encode()
                        new_len_str = str(tu_len + delta).encode()
                        len_num_end = len_pos + len(old_len_str)
                        out_arr[len_pos:len_num_end] = new_len_str
        except Exception as _e_tu:
            print(f"[WARN] ToUnicode auto-patch: {_e_tu}", file=sys.stderr)

    out_path = Path(args.output).resolve()
    out_path.write_bytes(out_arr)

    temp_pdf.unlink(missing_ok=True)
    print("✅ Готово:", out_path)
    added = list(uni_to_new_cid.keys())
    bf = "AAHTMC" if " 13.pdf" in target_path.name else target_path.stem[:8].upper()
    print(f"   База: {target_path.name}")
    print(f"   Дата в чеке: {date_str}")
    print(f"   Document ID: {id_method if id_m else 'не найден'}")
    print(f"   Глифы: {''.join(chr(u) for u in added)}" if added else "")
    meta = _check_metadata(bytes(out_arr))
    print("   Метаданные: CreationDate=" + (meta.get("CreationDate") or "—") + ", Producer=" + (meta.get("Producer") or "—") + ", ID=" + (meta.get("DocumentID", "")[:16] + "…" if meta.get("DocumentID") and len(meta.get("DocumentID", "")) > 16 else (meta.get("DocumentID") or "—")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
