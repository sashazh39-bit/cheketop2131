#!/usr/bin/env python3
"""Генератор выписки Альфа-Банка «Выписка по счету» без шаблона перевода.

Донор — подлинная одностраничная выписка (Oracle BI Publisher + iText 4.2.0).
Рамки, штамп, подпись, логотип и колонтитул не пересобираются. Меняются только
текстовые поля счёта/остатка и таблица операций (добавить / убрать / заменить).
Остатки всегда пересчитываются из операций, чтобы сходилась арифметика.
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import string
import sys
import zlib
from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import obi_patcher as _op

TEMPLATE = os.path.join(_HERE, "assets", "template_statement.pdf")
ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
ARIAL_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Геометрия донора (obj 10, страница 595.3×841.9).
_X_LABEL, _X_VALUE = 37.35, 146.65
_X_BAL_L, _X_BAL_AMT = 311.85, 516.305
_X_DATE, _X_CODE, _X_DESC = 27.25, 96.4, 188.35
_Y_OP0 = 444.154
_DY_1, _DY_2 = 19.65, 24.098
_WRAP_DY = 9.199
_RULE_1, _RULE_2 = 9.546, 13.994
_HEADER_RULE_Y = 453.758
_Y_MIN = 128.0
_DESC_RIGHT = 518.0
_AMT_RIGHT = 566.979
_GRAY = ".913 .913 .913 RG .913 .913 .913 rg"
_CRLF = "\r\n"

_Y_ACC = {
    "account": 670.546,
    "opened": 659.696,
    "currency": 648.346,
    "acc_type": 636.996,
    "formed": 625.646,
    "client1": 601.598,
    "client2": 592.399,
    "addr": [583.2, 574.001, 564.802, 555.603, 546.404],
}
_Y_BAL = {
    "period": 675.296,
    "opening": 662.497,
    "inflow": 645.447,
    "outflow": 628.397,
    "closing": 611.347,
    "limit": 594.297,
    "current": 554.547,
    "debt": 537.497,
}


class StatementError(Exception):
    pass


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def _objects(pdf: bytes) -> dict[int, tuple[int, int, bytes]]:
    """obj_no -> (start_of_number, end_after_endobj, body)."""
    out = {}
    for m in re.finditer(rb"(?:\A|[\r\n])(\d+) 0 obj\s*(.*?)\s*endobj", pdf, re.S):
        n = int(m.group(1))
        out[n] = (m.start(1), m.end(), m.group(2))
    return out


def _stream_of(body: bytes) -> Optional[bytes]:
    m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
    if not m:
        return None
    raw = m.group(1)
    d = _op._decompress_stream(raw)
    return d if d is not None else raw


def _parse_tounicode(data: bytes) -> dict[str, int]:
    text = data.decode("latin-1", errors="replace")
    result: dict[str, int] = {}
    for block in re.findall(r"beginbfchar\s*\n(.*?)endbfchar", text, re.DOTALL):
        for gh, uh in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            gid = int(gh, 16)
            try:
                uni = bytes.fromhex(uh).decode("utf-16-be")
            except Exception:
                uni = chr(int(uh, 16))
            result[uni] = gid
    return result


def _parse_w(body: bytes) -> dict[int, int]:
    m = re.search(rb"/W\s*\[", body)
    if not m:
        return {}
    depth = 0
    w_end = -1
    for i, b in enumerate(body[m.end() - 1 :], start=m.end() - 1):
        if b == ord("["):
            depth += 1
        elif b == ord("]"):
            depth -= 1
            if depth == 0:
                w_end = i
                break
    if w_end < 0:
        return {}
    s = body[m.end():w_end].decode("latin-1")
    w: dict[int, int] = {}
    for gm in re.finditer(r"(\d+)\s*\[([^\]]*)\]", s):
        start = int(gm.group(1))
        nums = [int(x) for x in gm.group(2).split() if x.lstrip("-").isdigit()]
        for i, val in enumerate(nums):
            w[start + i] = val
    return w


def _load_fonts(pdf: bytes) -> dict[str, dict]:
    """F1 / F2 / F3 from the page resources."""
    objs = _objects(pdf)
    page = next(
        b for _, _, b in objs.values()
        if re.search(rb"/Type\s*/Page[^s]", b)
    )
    res = re.search(rb"/Font\s*<<([^>]*)>>", page)
    mapping = {k.decode(): int(v) for k, v in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", res.group(1))}
    fonts = {}
    for name, type0 in mapping.items():
        body = objs[type0][2]
        tu = int(re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body).group(1))
        desc_font = int(re.search(rb"/DescendantFonts\s*\[(\d+)\s+0\s+R\]", body).group(1))
        cid = objs[desc_font][2]
        fd = int(re.search(rb"/FontDescriptor\s+(\d+)\s+0\s+R", cid).group(1))
        ff = int(re.search(rb"/FontFile2\s+(\d+)\s+0\s+R", objs[fd][2]).group(1))
        bf = re.search(rb"/BaseFont\s*/([A-Za-z0-9+\#]+)", body).group(1).decode()
        fonts[name] = {
            "type0": type0,
            "tounicode": tu,
            "cid": desc_font,
            "file2": ff,
            "basefont": bf,
            "cmap": _parse_tounicode(_stream_of(objs[tu][2])),
            "widths": _parse_w(cid),
            "font_bytes": _stream_of(objs[ff][2]),
        }
    return fonts


def _replace_obj_stream(pdf: bytes, obj_no: int, uncompressed: bytes, length1: Optional[int] = None) -> bytes:
    objs = _objects(pdf)
    start, end, body = objs[obj_no]
    # iText 4.2.0 / Java Deflater DEFAULT_COMPRESSION → zlib header 0x78 0x9c.
    # level=9 produces 0x78 0xda, which checkers flag as non-iText.
    compressed = zlib.compress(uncompressed, 6)
    dict_m = re.match(rb"<<.*?>>", body, re.S)
    if not dict_m:
        raise StatementError(f"obj {obj_no}: no dictionary")
    d = dict_m.group(0)
    d = re.sub(rb"/Length\s+\d+", b"/Length %d" % len(compressed), d, count=1)
    if length1 is not None:
        if b"/Length1" in d:
            d = re.sub(rb"/Length1\s+\d+", b"/Length1 %d" % length1, d, count=1)
        else:
            d = d[:-2] + b"/Length1 %d>>" % length1
    new_body = d + b"stream\n" + compressed + b"\nendstream"
    new_obj = b"%d 0 obj" % obj_no + b"\n" + new_body + b"\nendobj"
    return pdf[:start] + new_obj + pdf[end:]


def _append_bfchar(pdf: bytes, tu_obj: int, new_map: dict[str, int]) -> bytes:
    if not new_map:
        return pdf
    objs = _objects(pdf)
    data = _stream_of(objs[tu_obj][2])
    entries = "\n".join(f"<{gid:04X}> <{ord(ch):04X}>" for ch, gid in new_map.items())
    block = f"\n{len(new_map)} beginbfchar\n{entries}\nendbfchar\n".encode()
    pos = data.find(b"endcmap")
    if pos < 0:
        raise StatementError("ToUnicode: no endcmap")
    return _replace_obj_stream(pdf, tu_obj, data[:pos] + block + data[pos:])


def _append_w(pdf: bytes, cid_obj: int, gid_to_pdfw: dict[int, int]) -> bytes:
    if not gid_to_pdfw:
        return pdf
    objs = _objects(pdf)
    start, end, body = objs[cid_obj]
    m = re.search(rb"/W\s*\[", body)
    if not m:
        raise StatementError("CIDFont: no /W")
    depth = 0
    w_end = -1
    for i, b in enumerate(body[m.end() - 1 :], start=m.end() - 1):
        if b == ord("["):
            depth += 1
        elif b == ord("]"):
            depth -= 1
            if depth == 0:
                w_end = i
                break
    extra = "".join(f" {gid} [{w}]" for gid, w in sorted(gid_to_pdfw.items())).encode()
    new_body = body[:w_end] + extra + body[w_end:]
    new_obj = b"%d 0 obj\n" % cid_obj + new_body + b"\nendobj"
    return pdf[:start] + new_obj + pdf[end:]


def _content_obj(pdf: bytes) -> int:
    """The real page content stream (largest uncompressed text stream)."""
    objs = _objects(pdf)
    best, best_n = -1, -1
    for n, (_, _, body) in objs.items():
        d = _stream_of(body)
        if d and b"1 0 0 1 27.25 444" in d:
            return n
        if d and b"Tj" in d and len(d) > best:
            best, best_n = len(d), n
    if best_n < 0:
        raise StatementError("content stream not found")
    return best_n


# ---------------------------------------------------------------------------
# Font extension (Arial / Arial Bold)
# ---------------------------------------------------------------------------

def _extend_font(font_bytes: bytes, missing: set[str], src_path: str) -> tuple[bytes, dict[str, int]]:
    if not missing:
        return font_bytes, {}
    from fontTools import ttLib as ft

    sys_tt = ft.TTFont(src_path)
    sys_cmap = sys_tt.getBestCmap()
    sys_glyf = sys_tt["glyf"]
    sys_hmtx = sys_tt["hmtx"]

    obi = ft.TTFont(io.BytesIO(font_bytes))
    obi_glyf = obi["glyf"]
    obi_hmtx = obi["hmtx"]
    order = list(obi.getGlyphOrder())
    g2id = {n: i for i, n in enumerate(order)}
    next_gid = obi["maxp"].numGlyphs

    added: dict[str, int] = {}
    for ch in sorted(missing, key=ord):
        gname = sys_cmap.get(ord(ch))
        if gname is None:
            continue
        if gname not in g2id:
            simple, adv = _op._expand_to_simple(gname, sys_glyf, sys_hmtx)
            obi_glyf[gname] = simple
            obi_hmtx.metrics[gname] = (adv, sys_hmtx[gname][1])
            order.append(gname)
            g2id[gname] = next_gid
            next_gid += 1
        added[ch] = g2id[gname]

    obi.setGlyphOrder(order)
    obi["maxp"].numGlyphs = next_gid
    buf = io.BytesIO()
    obi.save(buf)
    return buf.getvalue(), added


def _retag(pdf: bytes, old: str, family: str) -> bytes:
    tag = "".join(random.choices(string.ascii_uppercase, k=6))
    return pdf.replace(old.encode(), f"{tag}+{family}".encode())


# ---------------------------------------------------------------------------
# Text metrics / encoding
# ---------------------------------------------------------------------------

def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s


def _hex(text: str, cmap: dict[str, int]) -> str:
    missing = [ch for ch in text if ch not in cmap]
    if missing:
        raise StatementError("в шрифте нет символов: " + "".join(dict.fromkeys(missing)))
    return "".join(f"{cmap[ch]:04X}" for ch in text)


def _width(text: str, cmap: dict[str, int], widths: dict[int, int], size: float = 8.0) -> float:
    return sum(widths.get(cmap.get(ch, -1), 1000) for ch in text) / 1000.0 * size


def _tj(x: float, y: float, text: str, cmap: dict[str, int]) -> str:
    return f"1 0 0 1 {_fmt(x)} {_fmt(y)} Tm{_CRLF}<{_hex(text, cmap)}> Tj{_CRLF}"


def _wrap(text: str, cmap, widths, max_w: float, size: float = 8.0) -> list[str]:
    if _width(text, cmap, widths, size) <= max_w:
        return [text]
    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = w if not cur else cur + " " + w
        if _width(trial, cmap, widths, size) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def _fmt_money(val: float, signed: bool = True) -> str:
    n = abs(round(val, 2))
    whole = int(n)
    frac = int(round((n - whole) * 100))
    body = f"{whole:,}".replace(",", " ") + f",{frac:02d}"
    if signed and val < 0:
        return f"-{body} RUR"
    return f"{body} RUR"


def _fmt_phone_out(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    if d.startswith("7") and len(d) == 11:
        return f"+7 ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:11]}"
    return phone


def _fmt_phone_in(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    if d.startswith("8") and len(d) == 11:
        d = "7" + d[1:]
    return "+" + d if d.startswith("7") else phone


def _split_client(name: str) -> tuple[str, str]:
    parts = name.split()
    if len(parts) >= 3 and re.search(r"(вич|вна|мич|ич)$", parts[-1], re.I):
        return " ".join(parts[:-1]) + " ", parts[-1]
    if len(parts) >= 2:
        return parts[0] + " ", " ".join(parts[1:])
    return name, ""


def _wrap_addr(addr: str, cmap, widths) -> list[str]:
    if isinstance(addr, list):
        lines = list(addr)
    else:
        lines = []
        for raw in addr.replace("\r", "").split("\n"):
            lines.extend(_wrap(raw if raw.endswith(" ") or not raw else raw, cmap, widths, 132.0))
    while len(lines) < 5:
        lines.append("")
    return lines[:5]


def _make_code(kind: str, date: str, rng: random.Random) -> str:
    dd, mm, yy = date.split(".")
    yy = yy[-2:]
    prefix = {"sbp_out": "C16", "sbp_in": "C17", "payment": "C21", "transfer": "C07"}.get(kind, "C16")
    seq = rng.randint(8_000, 1_900_000)
    return f"{prefix}{dd}{mm}{yy}{seq:07d}"


def _desc_for(op: dict, rng: random.Random) -> tuple[str, list[str], bool]:
    """Return (code, description_lines, is_credit)."""
    kind = op.get("type") or "sbp_out"
    date = op["date"]
    code = op.get("code") or _make_code(kind, date, rng)
    amt = float(op["amount"])
    credit = amt > 0
    if op.get("lines"):
        return code, list(op["lines"]), credit
    raw_desc = (op.get("description") or "").strip()
    if raw_desc:
        raw_desc = raw_desc.replace("{code}", code)
        lines = [ln.strip() for ln in raw_desc.split("\n") if ln.strip()]
        return code, lines or [raw_desc], credit
    if kind == "sbp_out":
        phone = _fmt_phone_out(op.get("phone") or "+7 (900) 000-00-00")
        # как в доноре: перенос после предпоследнего дефиса телефона
        if phone.count("-") >= 2:
            a, b = phone.rsplit("-", 1)
            line1 = f"Перевод {code} через Систему быстрых платежей на {a}-"
            line2 = f"{b}. Без НДС."
        else:
            line1 = f"Перевод {code} через Систему быстрых платежей на {phone}."
            line2 = "Без НДС."
        return code, [line1, line2], False
    if kind == "sbp_in":
        phone = _fmt_phone_in(op.get("phone") or "+79000000000")
        line1 = f"Перевод {code} через Систему быстрых платежей от {phone}. "
        line2 = "Без НДС."
        return code, [line1, line2], True
    if kind == "payment":
        merch = op.get("merchant") or "SBP"
        return code, [f"Платеж {code} в {merch} через Систему быстрых платежей."], False
    if kind == "transfer":
        return code, ["Перевод денежных средств"], credit
    desc = op.get("description") or f"Перевод {code}"
    return code, [desc], credit


def _recalc(opening: float, ops: list[dict]) -> dict[str, float]:
    inflow = sum(float(o["amount"]) for o in ops if float(o["amount"]) > 0)
    outflow = sum(-float(o["amount"]) for o in ops if float(o["amount"]) < 0)
    closing = round(opening + inflow - outflow, 2)
    return {
        "opening": round(opening, 2),
        "inflow": round(inflow, 2),
        "outflow": round(outflow, 2),
        "closing": closing,
        "limit": closing,
        "current": closing,
        "debt": 0.0,
    }


# ---------------------------------------------------------------------------
# Donor defaults (parsed once)
# ---------------------------------------------------------------------------

def _donor_defaults(pdf: bytes, fonts: dict) -> dict:
    cmap = {v: k for k, v in fonts["F1"]["cmap"].items()}
    cs = _stream_of(_objects(pdf)[_content_obj(pdf)][2]).decode("latin-1")
    texts = []
    x = y = 0.0
    font = "F1"
    for m in re.finditer(
        r"(?:1 0 0 1 ([\d.]+) ([\d.]+) Tm)|(?:/(F[123])\s*\r?\n\s*([\d.]+) Tf)|(?:<([0-9A-Fa-f]+)> Tj)",
        cs,
    ):
        if m.group(1):
            x, y = float(m.group(1)), float(m.group(2))
        elif m.group(3):
            font = m.group(3)
        elif m.group(5):
            g2c = {v: k for k, v in fonts[font]["cmap"].items()}
            t = "".join(g2c.get(int(m.group(5)[i:i + 4], 16), "?") for i in range(0, len(m.group(5)), 4))
            texts.append((font, x, y, t))
    by_y: dict[float, list] = {}
    for font, x, y, t in texts:
        by_y.setdefault(round(y, 3), []).append((x, t, font))

    def col(y, x0):
        row = by_y.get(round(y, 3), [])
        row = sorted(row, key=lambda r: abs(r[0] - x0))
        return row[0][1] if row else ""

    addr = [col(y, _X_VALUE) for y in _Y_ACC["addr"]]
    ops = []
    # rows: groups of texts with y around op lines at x=27.25
    op_dates = [(y, t) for _, x, y, t in texts if abs(x - _X_DATE) < 0.2 and re.match(r"\d{2}\.\d{2}\.\d{4}$", t)]
    for y, date in op_dates:
        code = next((t for _, x, yy, t in texts if abs(yy - y) < 0.2 and abs(x - _X_CODE) < 0.2), "")
        desc1 = next((t for _, x, yy, t in texts if abs(yy - y) < 0.2 and abs(x - _X_DESC) < 0.2), "")
        desc2 = next((t for _, x, yy, t in texts if abs(yy - (y - _WRAP_DY)) < 0.3 and abs(x - _X_DESC) < 0.2), "")
        amt_s = next((t for f, x, yy, t in texts if abs(yy - y) < 0.2 and x > 500), "0")
        amt_n = amt_s.replace(" ", "").replace("RUR", "").replace(",", ".")
        sign = -1 if amt_n.startswith("-") else 1
        try:
            amount = sign * abs(float(amt_n.replace("+", "")))
        except ValueError:
            amount = 0.0
        kind = "sbp_out"
        if code.startswith("C17") or amount > 0:
            kind = "sbp_in"
        elif code.startswith("C21"):
            kind = "payment"
        elif code.startswith("C07"):
            kind = "transfer"
        op = {"date": date, "code": code, "type": kind, "amount": amount, "description": (desc1 + " " + desc2).strip()}
        op["lines"] = [desc1] + ([desc2] if desc2 else [])
        blob = desc1 + desc2
        m_phone = re.search(r"\+7(?:\s*\(\d{3}\)\s*\d{3}-\d{2}-\d{2}|\d{10})", blob)
        if m_phone:
            op["phone"] = m_phone.group(0).rstrip(".")
        m_mer = re.search(r" в (.+?) через", desc1)
        if m_mer:
            op["merchant"] = m_mer.group(1)
        ops.append(op)

    opening_s = col(_Y_BAL["opening"], 516.305).replace(" RUR", "").replace(" ", "").replace(",", ".")
    return {
        "account": col(_Y_ACC["account"], _X_VALUE),
        "opened": col(_Y_ACC["opened"], _X_VALUE),
        "currency": col(_Y_ACC["currency"], _X_VALUE),
        "acc_type": col(_Y_ACC["acc_type"], _X_VALUE),
        "formed": col(_Y_ACC["formed"], _X_VALUE),
        "client1": col(_Y_ACC["client1"], _X_VALUE),
        "client2": col(_Y_ACC["client2"], _X_VALUE),
        "address": addr,
        "period_from": re.search(r"с (\d{2}\.\d{2}\.\d{4})", col(_Y_BAL["period"], _X_BAL_L)).group(1),
        "period_to": re.search(r"по (\d{2}\.\d{2}\.\d{4})", col(_Y_BAL["period"], _X_BAL_L)).group(1),
        "opening_balance": float(opening_s),
        "ops": ops,
    }


# ---------------------------------------------------------------------------
# Content-stream rebuild
# ---------------------------------------------------------------------------

def _build_account(data: dict, cmap, widths) -> str:
    c1, c2 = data["client1"], data["client2"]
    addr = _wrap_addr(data["address"], cmap, widths)
    parts = [
        _tj(_X_LABEL, _Y_ACC["account"], "Номер счета", cmap),
        _tj(_X_VALUE, _Y_ACC["account"], data["account"], cmap),
        _tj(_X_LABEL, _Y_ACC["opened"], "Дата открытия счета", cmap),
        _tj(_X_VALUE, _Y_ACC["opened"], data["opened"], cmap),
        _tj(_X_LABEL, _Y_ACC["currency"], "Валюта счета", cmap),
        _tj(_X_VALUE, _Y_ACC["currency"], data["currency"], cmap),
        _tj(_X_LABEL, _Y_ACC["acc_type"], "Тип счета", cmap),
        _tj(_X_VALUE, _Y_ACC["acc_type"], data["acc_type"], cmap),
        _tj(_X_LABEL, _Y_ACC["formed"], "Дата формирования ", cmap),
        _tj(_X_LABEL, 616.447, "выписки", cmap),
        _tj(_X_VALUE, _Y_ACC["formed"], data["formed"], cmap),
        _tj(_X_LABEL, _Y_ACC["client1"], "Клиент", cmap),
        _tj(_X_VALUE, _Y_ACC["client1"], c1, cmap),
        _tj(_X_VALUE, _Y_ACC["client2"], c2, cmap) if c2 else "",
        _tj(_X_LABEL, 583.2, "Адрес регистрации", cmap),
    ]
    for y, line in zip(_Y_ACC["addr"], addr):
        if line:
            parts.append(_tj(_X_VALUE, y, line, cmap))
    return "".join(parts)


def _build_balance(data: dict, bal: dict, cmap, widths) -> str:
    period = f"За период с {data['period_from']} по {data['period_to']}"
    rows = [
        (_Y_BAL["period"], period, None, _X_BAL_L),
        (_Y_BAL["opening"], "Входящий остаток", bal["opening"], 316.1),
        (_Y_BAL["inflow"], "Поступления", bal["inflow"], 316.1),
        (_Y_BAL["outflow"], "Расходы", bal["outflow"], 316.1),
        (_Y_BAL["closing"], "Исходящий остаток", bal["closing"], _X_BAL_L),
        (_Y_BAL["limit"], "Платежный лимит", bal["limit"], _X_BAL_L),
        (567.346, "На дату формирования выписки", None, _X_BAL_L),
        (_Y_BAL["current"], "Текущий баланс", bal["current"], _X_BAL_L),
        (_Y_BAL["debt"], "Общая задолженность к погашению", None, _X_BAL_L),
    ]
    out = []
    for y, label, val, lx in rows:
        out.append(_tj(lx, y, label, cmap))
        if y == _Y_BAL["debt"]:
            debt = bal.get("debt", 0.0)
            s = "0" if abs(debt) < 0.005 else _fmt_money(debt, signed=False)
            w = _width(s, cmap, widths)
            out.append(_tj(_AMT_RIGHT - w, y, s, cmap))
        elif val is not None:
            s = _fmt_money(val, signed=False)
            w = _width(s, cmap, widths)
            out.append(_tj(_AMT_RIGHT - w, y, s, cmap))
    return "".join(out)


def _build_ops(ops: list[dict], cmap_f1, widths_f1, cmap_f3, widths_f3, rng) -> str:
    y = _Y_OP0
    chunks = []
    rules = []
    prepared = []
    for op in ops:
        code, lines, credit = _desc_for(op, rng)
        # wrap extra-long first line
        if len(lines) == 1:
            wrapped = _wrap(lines[0], cmap_f1, widths_f1, _DESC_RIGHT - _X_DESC)
            lines = wrapped
        two = len(lines) > 1
        prepared.append((op, code, lines, credit, two, y))
        rules.append(y - (_RULE_2 if two else _RULE_1))
        y -= _DY_2 if two else _DY_1
        if y < _Y_MIN:
            raise StatementError(
                f"слишком много операций для одной страницы ({len(ops)}). "
                "Уберите часть или сократите описания."
            )

    for op, code, lines, credit, two, y in prepared:
        if credit:
            h = _DY_2 if two else _DY_1
            rect_y = y - (13.744 if two else 9.2)
            chunks.append(f"{_GRAY}{_CRLF}ET{_CRLF}")
            chunks.append(f"27.25 {_fmt(rect_y)} 540.8 {_fmt(h)} re f{_CRLF}")
            chunks.append(f"0 0 0 RG 0 0 0 rg{_CRLF}BT{_CRLF}")
        chunks.append(_tj(_X_DATE, y, op["date"], cmap_f1))
        chunks.append(_tj(_X_CODE, y, code, cmap_f1))
        chunks.append(_tj(_X_DESC, y, lines[0], cmap_f1))
        if two:
            chunks.append(_tj(_X_DESC, y - _WRAP_DY, lines[1], cmap_f1))
        amt = _fmt_money(float(op["amount"]), signed=True)
        if credit:
            # входящие в доноре без минуса и жирным F3
            amt = _fmt_money(float(op["amount"]), signed=False)
            w = _width(amt, cmap_f3, widths_f3)
            chunks.append(f"/F3{_CRLF} 8 Tf{_CRLF}")
            chunks.append(_tj(_AMT_RIGHT - w, y, amt, cmap_f3))
            chunks.append(f"/F1{_CRLF} 8 Tf{_CRLF}")
        else:
            w = _width(amt, cmap_f1, widths_f1)
            chunks.append(_tj(_AMT_RIGHT - w, y, amt, cmap_f1))

    # правила: последняя, жирная шапка, затем межстрочные сверху вниз кроме последней
    tail = [f".498 .498 .498 RG .498 .498 .498 rg{_CRLF}ET{_CRLF}"]
    if rules:
        tail.append(f"27.25 {_fmt(rules[-1])} 540.8 .5 re f{_CRLF}")
    tail.append(f"27.25 {_fmt(_HEADER_RULE_Y)} 540.8 1.5 re f{_CRLF}")
    for ry in rules[:-1]:
        tail.append(f"27.25 {_fmt(ry)} 540.8 .5 re f{_CRLF}")
    return "".join(chunks) + "".join(tail)


def _needed_chars(data: dict, bal: dict, ops: list[dict], rng) -> tuple[set[str], set[str]]:
    f1: set[str] = set()
    # F3 Bold is only used for incoming-payment amounts.
    # Do NOT pre-seed with all digits — that adds unnecessary glyphs to the
    # font subset and creates a detectable forensic signal (extra /W entries).
    f3: set[str] = set()
    blobs = [
        data["account"], data["opened"], data["currency"], data["acc_type"],
        data["formed"], data["client1"], data["client2"],
        data["period_from"], data["period_to"],
        "Номер счетаДата открытия счетаВалюта счетаТип счетаДата формирования выпискиКлиентАдрес регистрации",
        "За период с  по Входящий остатокПоступленияРасходыИсходящий остатокПлатежный лимит",
        "На дату формирования выпискиТекущий балансОбщая задолженность к погашению",
        _fmt_money(0, False),
    ]
    if isinstance(data["address"], list):
        blobs.extend(data["address"])
    else:
        blobs.append(data["address"])
    for v in bal.values():
        blobs.append(_fmt_money(float(v), False))
    for op in ops:
        code, lines, credit = _desc_for(op, rng)
        blobs.append(op["date"])
        blobs.append(code)
        blobs.extend(lines)
        amt = _fmt_money(float(op["amount"]), signed=not credit)
        if credit:
            f3.update(amt)
        else:
            blobs.append(amt)
    f1.update(ch for b in blobs for ch in b)
    return f1, f3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_donor(template: str = TEMPLATE) -> dict:
    pdf = open(template, "rb").read()
    fonts = _load_fonts(pdf)
    return _donor_defaults(pdf, fonts)


def build(data: dict, template: str = TEMPLATE, seed: Optional[int] = None) -> bytes:
    rng = random.Random(seed)
    pdf = open(template, "rb").read()
    fonts = _load_fonts(pdf)
    donor = _donor_defaults(pdf, fonts)

    merged = dict(donor)
    merged.update({k: v for k, v in data.items() if v is not None and k != "ops"})
    if data.get("client"):
        merged["client1"], merged["client2"] = _split_client(data["client"])
    if data.get("address") is not None:
        merged["address"] = data["address"]
    if data.get("formed") and not data.get("period_from"):
        merged["period_from"] = data["formed"]
    if data.get("formed") and not data.get("period_to"):
        merged["period_to"] = data["formed"]
    ops = deepcopy(data["ops"]) if data.get("ops") is not None else deepcopy(donor["ops"])
    if not ops:
        raise StatementError("нужна хотя бы одна операция")

    opening = float(merged.get("opening_balance", donor["opening_balance"]))
    # предварительный проход для кодов (стабильно от seed)
    probe_rng = random.Random(seed)
    for op in ops:
        if not op.get("code"):
            op["code"] = _make_code(op.get("type") or "sbp_out", op["date"], probe_rng)

    bal = _recalc(opening, ops)
    for key, src in (
        ("opening", "opening_balance"),
        ("inflow", "inflow"),
        ("outflow", "outflow"),
        ("closing", "closing"),
        ("limit", "limit"),
        ("current", "current"),
        ("debt", "debt"),
    ):
        if data.get(src) is not None:
            bal[key] = float(data[src])
    need_f1, need_f3 = _needed_chars(merged, bal, ops, random.Random(seed))

    miss_f1 = {ch for ch in need_f1 if ch not in fonts["F1"]["cmap"] and ord(ch) > 31}
    miss_f3 = {ch for ch in need_f3 if ch not in fonts["F3"]["cmap"] and ord(ch) > 31}

    new_f1, add_f1 = _extend_font(fonts["F1"]["font_bytes"], miss_f1, ARIAL)
    new_f3, add_f3 = _extend_font(fonts["F3"]["font_bytes"], miss_f3, ARIAL_BOLD)
    fonts["F1"]["cmap"].update(add_f1)
    fonts["F3"]["cmap"].update(add_f3)

    if add_f1:
        pdf = _replace_obj_stream(pdf, fonts["F1"]["file2"], new_f1, length1=len(new_f1))
        pdf = _append_bfchar(pdf, fonts["F1"]["tounicode"], add_f1)
        from fontTools import ttLib as ft
        tt = ft.TTFont(io.BytesIO(new_f1))
        order = tt.getGlyphOrder()
        pdfw = {gid: round(tt["hmtx"][order[gid]][0] * 1000 / tt["head"].unitsPerEm) for gid in add_f1.values()}
        fonts["F1"]["widths"].update(pdfw)
        pdf = _append_w(pdf, fonts["F1"]["cid"], pdfw)
    if add_f3:
        pdf = _replace_obj_stream(pdf, fonts["F3"]["file2"], new_f3, length1=len(new_f3))
        pdf = _append_bfchar(pdf, fonts["F3"]["tounicode"], add_f3)
        from fontTools import ttLib as ft
        tt = ft.TTFont(io.BytesIO(new_f3))
        order = tt.getGlyphOrder()
        pdfw = {gid: round(tt["hmtx"][order[gid]][0] * 1000 / tt["head"].unitsPerEm) for gid in add_f3.values()}
        fonts["F3"]["widths"].update(pdfw)
        pdf = _append_w(pdf, fonts["F3"]["cid"], pdfw)

    cobj = _content_obj(pdf)
    cs = _stream_of(_objects(pdf)[cobj][2])
    # срезы донора
    a0 = cs.find(b"1 0 0 1 37.35 670.546 Tm")
    b0 = cs.find(b".501 .501 .501 RG")
    c0 = cs.find(b"1 0 0 1 311.85 675.296 Tm")
    d0 = cs.find(b"311.85 621.451 255.15 .5 re f")
    e0 = cs.find(b"1 0 0 1 28.35 487.697 Tm")
    f0 = cs.find(b"1 0 0 1 27.25 444.154 Tm")
    if min(a0, b0, c0, d0, e0, f0) < 0:
        raise StatementError("не найдены маркеры донора — шаблон повреждён")

    cmap1, w1 = fonts["F1"]["cmap"], fonts["F1"]["widths"]
    cmap3, w3 = fonts["F3"]["cmap"], fonts["F3"]["widths"]
    account_cs = ("/F1" + _CRLF + " 8 Tf" + _CRLF + _build_account(merged, cmap1, w1)).encode("latin-1")
    balance_cs = _build_balance(merged, bal, cmap1, w1).encode("latin-1")
    ops_cs = _build_ops(ops, cmap1, w1, cmap3, w3, random.Random(seed)).encode("latin-1")

    new_cs = (
        cs[:a0]
        + account_cs
        + cs[b0:c0]
        + balance_cs
        + cs[d0:f0]
        + ops_cs
    )
    pdf = _replace_obj_stream(pdf, cobj, new_cs)

    # уникальные теги подмножеств
    pdf = _retag(pdf, fonts["F1"]["basefont"], "Arial")
    pdf = _retag(pdf, fonts["F2"]["basefont"], "Arial#20Italic")
    pdf = _retag(pdf, fonts["F3"]["basefont"], "Arial#20Bold")

    formed = merged["formed"]
    # iText stamps /ModDate with a real wall-clock time, never a round noon.
    # Seconds and minutes are both non-zero on genuine statements.
    stamp = f"{formed} {rng.randint(9, 17):02d}:{rng.randint(1, 59):02d}:{rng.randint(1, 59):02d}"
    pdf = _op._patch_docid_moddate(pdf, stamp)
    pdf = _op._rebuild_xref_table(pdf, style="itext")
    return pdf


def list_ops(template: str = TEMPLATE) -> list[dict]:
    return parse_donor(template)["ops"]
