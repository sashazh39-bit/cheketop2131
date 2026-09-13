#!/usr/bin/env python3
"""Generate a Yandex Bank SBP receipt from a genuine template.

The receipt is produced by JasperReports + OpenPDF. Text is stored as 2-byte
GIDs (Identity-H) inside literal ( ) strings, and OpenPDF keeps the full font's
911-glyph numbering, emptying the outlines of unused glyphs. This generator:

  * replaces field values in the content stream, re-encoding them to GIDs from
    the authentic glyph bank and re-computing the x of right-aligned values so
    their right edge stays put;
  * rebuilds the YSText-Regular font so exactly the glyphs used in this receipt
    carry outlines (the rest emptied), matching OpenPDF's structure;
  * regenerates the dependent objects (CIDSet, /W widths, ToUnicode);
  * re-serializes the PDF with a correct xref table and a fresh /ID.

Operation identifiers are not random: the SBP ID and Исх. № encode the transfer
moment, decoded from genuine receipts, so they are generated consistently.

Usage: edit DATA below (or import build()) and run.
"""

from __future__ import annotations

import copy
import datetime
import io
import json
import logging
import os
import random
import re
import uuid
import zlib

from fontTools import ttLib
from fontTools.ttLib.tables._g_l_y_f import Glyph as _Glyph

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
BANK_TTF = os.path.join(ASSETS, "ys_text_bank.ttf")
BANK_JSON = os.path.join(ASSETS, "ys_text_bank.json")
TEMPLATE = os.path.join(ASSETS, "template.pdf")


class YandexReceiptError(Exception):
    """Input the generator cannot honour (missing glyph, impossible date …)."""

MSK = datetime.timezone(datetime.timedelta(hours=3))


def msk_now() -> datetime.datetime:
    """Wall clock in Moscow, naive — every printed time on the receipt is MSK."""
    return datetime.datetime.now(MSK).replace(tzinfo=None)


SBP_EPOCH = datetime.date(2009, 7, 28)
# Trailing ten digits of an SBP operation id: the NSPK platform build that
# handled the transfer, reading as 00-11-<major>-<minor>-01. It only ever moves
# forward, so it has to be picked by payment date, not hard-coded. Boundaries
# below are the earliest payment date observed carrying each build.
SBP_SCHEME_BUILDS = [
    (datetime.date(2026, 2, 6),  "0011690101"),
    (datetime.date(2026, 8, 22), "0011831501"),
    (datetime.date(2026, 8, 29), "0011840301"),
    (datetime.date(2026, 9, 9),  "0011850501"),
]


def sbp_scheme_tail(day: datetime.date) -> str:
    known = [t for since, t in SBP_SCHEME_BUILDS if since <= day]
    if not known:
        raise YandexReceiptError(
            f"нет данных о версии схемы СБП на {day:%d.%m.%Y}")
    if day > SBP_SCHEME_BUILDS[-1][0] + datetime.timedelta(days=14):
        logger.warning("платёж %s позже последнего известного донора (%s), "
                       "версия схемы СБП может быть устаревшей",
                       f"{day:%d.%m.%Y}", f"{SBP_SCHEME_BUILDS[-1][0]:%d.%m.%Y}")
    return known[-1]
SBP_ALPHABET = "0123456789ABCDEFGHIKLMOPRTUVWXY"
ALPHA_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ── glyph bank ──────────────────────────────────────────────────────────────

class Bank:
    def __init__(self):
        with open(BANK_JSON, encoding="utf-8") as fh:
            meta = json.load(fh)
        self.upm = meta["upm"]
        self.num_glyphs = meta["num_glyphs"]
        self.char2gid = {c: v[0] for c, v in meta["chars"].items()}
        self.gid2width = {v[0]: v[1] for v in meta["chars"].values()}
        self.gid2char = {v[0]: c for c, v in meta["chars"].items()}

    def missing(self, text: str) -> set:
        return {c for c in text if c not in self.char2gid}

    def encode(self, text: str) -> list:
        return [self.char2gid[c] for c in text]

    def width(self, text: str, size: float) -> float:
        return sum(self.gid2width.get(self.char2gid[c], 0) for c in text) * size / self.upm


# ── PDF literal-string helpers ──────────────────────────────────────────────

def fmt_num(x: float) -> bytes:
    """Format a coordinate the way OpenPDF's ByteBuffer does: up to 2 decimals,
    trailing zeros and a trailing dot stripped (561.00→561, 496.30→496.3)."""
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.encode()


def gids_to_pdf_string(gids: list) -> bytes:
    raw = bytearray()
    for g in gids:
        raw += bytes(((g >> 8) & 0xFF, g & 0xFF))
    out = bytearray(b"(")
    for byte in raw:
        ch = bytes((byte,))
        if ch in (b"(", b")", b"\\"):
            out += b"\\" + ch
        elif byte == 0x0D:
            out += b"\\r"
        elif byte == 0x0A:
            out += b"\\n"
        else:
            out += ch
    out += b")"
    return bytes(out)


def unescape_pdf_string(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i:i + 1] == b"\\":
            nx = s[i + 1:i + 2]
            if nx and nx in b"01234567":            # octal escape \ddd
                j = i + 1
                oct_digits = b""
                while j < len(s) and len(oct_digits) < 3 and s[j:j + 1] in b"01234567":
                    oct_digits += s[j:j + 1]
                    j += 1
                out += bytes((int(oct_digits, 8) & 0xFF,))
                i = j
                continue
            mp = {b"r": b"\r", b"n": b"\n", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
            out += mp.get(nx, nx)
            i += 2
        else:
            out += s[i:i + 1]
            i += 1
    return bytes(out)


def iter_literal_strings(stream: bytes):
    """Yield (start_offset, raw_inner_bytes) for every ( … ) literal string,
    correctly honouring \\-escapes and balanced parentheses (binary GID data
    frequently contains bytes that look like parens/backslashes)."""
    i, n = 0, len(stream)
    while i < n:
        if stream[i] == 0x28:                       # '('
            start = i
            depth = 1
            i += 1
            buf = bytearray()
            while i < n and depth > 0:
                c = stream[i]
                if c == 0x5C:                       # backslash: copy escape verbatim
                    buf += stream[i:i + 2]
                    i += 2
                    continue
                if c == 0x28:
                    depth += 1
                elif c == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf += bytes((c,))
                i += 1
            yield start, bytes(buf)
        else:
            i += 1


def f1_used_gids(stream: bytes) -> set:
    """All GIDs shown with the F1 font anywhere in the content stream."""
    font_ops = [(m.start(), m.group(1)) for m in
                re.finditer(rb"/(F\d) [\d.]+ Tf", stream)]

    def font_at(off):
        cur = None
        for pos, f in font_ops:
            if pos < off:
                cur = f
            else:
                break
        return cur

    used = set()
    for off, inner in iter_literal_strings(stream):
        if font_at(off) != b"F1":
            continue
        raw = unescape_pdf_string(inner)
        for b0, b1 in zip(raw[0::2], raw[1::2]):
            used.add((b0 << 8) | b1)
    return used


# ── object-level PDF model (classic xref) ───────────────────────────────────

def parse_objects(pdf: bytes):
    """Return ({num: body}, physical_order). OpenPDF lays objects out in
    creation order (e.g. 5,6,7,1,…), not by number — preserving that exact
    order is required or the receipt trips the "PDF structure" check."""
    objs = {}
    order = []
    for m in re.finditer(rb"\n(\d+) 0 obj\n?(.*?)\nendobj", pdf, re.S):
        n = int(m.group(1))
        objs[n] = m.group(2)
        order.append(n)
    return objs, order


def stream_of(body: bytes):
    m = re.search(rb"(<<.*?>>)\s*stream\r?\n(.*?)\r?\nendstream", body, re.S)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def set_stream(dict_bytes: bytes, data: bytes) -> bytes:
    d = re.sub(rb"/Length\s+\d+", b"/Length %d" % len(data), dict_bytes)
    if b"/Length" not in d:
        d = d[:-2] + b"/Length %d>>" % len(data)
    # OpenPDF frames streams as `>>stream\n<data>\nendstream` (no newline
    # between the dict and the `stream` keyword).
    return d + b"stream\n" + data + b"\nendstream"


def serialize(objects: dict, order: list, root: int, info: int, docid: bytes) -> bytes:
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in order:
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    xref_pos = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n" % size
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += (b"trailer\n<</Info %d 0 R/ID [<%s><%s>]/Root %d 0 R/Size %d>>\n"
            % (info, docid, docid, root, size))
    out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)


# ── font rebuild ────────────────────────────────────────────────────────────

import struct as _struct

_OPENPDF_TABLE_ORDER = [b"glyf", b"head", b"hhea", b"hmtx", b"loca", b"maxp", b"prep"]


def reorder_sfnt(ttf: bytes, order: list, checksum_adj: int = None) -> bytes:
    """Rewrite a TrueType file so its tables are physically laid out in `order`
    (OpenPDF uses alphabetical), preserving each table's checksum. Directory
    entries remain tag-sorted per spec. `head.checkSumAdjustment` is forced to
    `checksum_adj` when given — OpenPDF copies it verbatim from the source YS
    Text font (constant across receipts) rather than recomputing per spec."""
    num = _struct.unpack(">H", ttf[4:6])[0]
    entries = {}
    for i in range(num):
        tag, cs, off, ln = _struct.unpack(">4sIII", ttf[12 + 16 * i:28 + 16 * i])
        entries[tag] = (cs, ttf[off:off + ln], ln)
    seq = [t for t in order if t in entries] + \
          [t for t in sorted(entries) if t not in order]

    head = bytearray(12)  # sfnt header rebuilt below
    scaler = ttf[0:4]
    search = _struct.pack(">HHH", *_sfnt_search_params(len(seq)))
    head[0:4] = scaler
    head[4:6] = _struct.pack(">H", len(seq))
    head[6:12] = search

    dir_size = 16 * len(seq)
    data_off = 12 + dir_size
    body = bytearray()
    offsets = {}
    for tag in seq:
        cs, data, ln = entries[tag]
        offsets[tag] = data_off + len(body)
        body += data
        while len(body) % 4:
            body += b"\x00"

    directory = bytearray()
    for tag in sorted(entries):                     # directory sorted by tag
        cs, data, ln = entries[tag]
        directory += _struct.pack(">4sIII", tag, cs, offsets[tag], ln)

    out = bytearray(bytes(head) + bytes(directory) + bytes(body))
    head_off = offsets[b"head"]
    if checksum_adj is None:
        tmp = bytearray(out)
        tmp[head_off + 8:head_off + 12] = b"\x00\x00\x00\x00"
        total = 0
        for i in range(0, len(tmp), 4):
            total = (total + _struct.unpack(">I", tmp[i:i + 4].ljust(4, b"\x00"))[0]) & 0xFFFFFFFF
        checksum_adj = (0xB1B0AFBA - total) & 0xFFFFFFFF
    out[head_off + 8:head_off + 12] = _struct.pack(">I", checksum_adj)
    return bytes(out)


def _sfnt_search_params(n: int):
    entry = 0
    while (1 << (entry + 1)) <= n:
        entry += 1
    search = (1 << entry) * 16
    shift = n * 16 - search
    return search, entry, shift


def rebuild_font(used_gids: set, checksum_adj: int = None):
    """Master bank font with outlines kept only for used_gids, rest emptied.

    Many Cyrillic letters are composite glyphs that reference a Latin base by
    GID (е→e, о→o, а→a, С→C …). Those component GIDs must stay filled or the
    composite renders blank, so `used` is expanded over composite components
    transitively before anything is emptied.
    """
    tt = ttLib.TTFont(BANK_TTF, recalcBBoxes=False, recalcTimestamp=False)
    order = tt.getGlyphOrder()
    glyf = tt["glyf"]
    for name in order:
        _ = glyf[name]

    keep = set(used_gids) | {0}
    name_to_gid = {n: i for i, n in enumerate(order)}
    stack = list(keep)
    while stack:
        g = stack.pop()
        glyph = glyf[order[g]]
        if glyph.numberOfContours < 0:            # composite
            for comp in glyph.components:
                cg = name_to_gid.get(comp.glyphName)
                if cg is not None and cg not in keep:
                    keep.add(cg)
                    stack.append(cg)

    empty = _Glyph()
    empty.numberOfContours = 0
    for gid, name in enumerate(order):
        if gid not in keep:
            glyf[name] = copy.deepcopy(empty)
    tt["head"].indexToLocFormat = 0
    buf = io.BytesIO()
    tt.save(buf)
    return reorder_sfnt(buf.getvalue(), _OPENPDF_TABLE_ORDER, checksum_adj), keep


def build_cidset(used_gids: set, num_glyphs: int) -> bytes:
    n = (num_glyphs + 7) // 8
    bits = bytearray(n)
    for g in used_gids:
        bits[g >> 3] |= 0x80 >> (g & 7)
    # trim trailing zero bytes the way OpenPDF does (keeps up to last used bit)
    last = max(used_gids)
    return bytes(bits[:(last >> 3) + 1])


def build_w_array(used_gids: set, gid2width: dict) -> bytes:
    """OpenPDF groups consecutive GIDs into one entry: 40[645 671] = CID 40,41."""
    gids = sorted(used_gids)
    parts = []
    i = 0
    while i < len(gids):
        j = i
        while j + 1 < len(gids) and gids[j + 1] == gids[j] + 1:
            j += 1
        run = gids[i:j + 1]
        ws = b" ".join(b"%d" % gid2width.get(g, 0) for g in run)
        parts.append(b"%d[%s]" % (run[0], ws))
        i = j + 1
    return b"/W [" + b"".join(parts) + b"]"


def build_tounicode(used_gids: set, gid2char: dict) -> bytes:
    """Replicate OpenPDF's ToUnicode exactly (TTX+0 / T42UV, bfrange, lowercase
    hex, `end end` footer)."""
    lines = []
    for g in sorted(used_gids):
        ch = gid2char.get(g)
        if ch is None:
            continue
        lines.append(b"<%04x><%04x><%04x>" % (g, g, ord(ch)))
    body = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo\n<< /Registry (TTX+0)\n/Ordering (T42UV)\n/Supplement 0\n>> def\n"
            b"/CMapName /TTX+0 def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<0000><FFFF>\nendcodespacerange\n"
            b"%d beginbfrange\n" % len(lines) + b"\n".join(lines)
            + b"\nendbfrange\nendcmap\nCMapName currentdict /CMap defineresource pop\nend end\n")
    return body


# ── operation identifiers ───────────────────────────────────────────────────

def make_sbp_id(transfer: datetime.datetime, rng: random.Random = random) -> str:
    utc = transfer - datetime.timedelta(hours=3)
    day = (utc.date() - SBP_EPOCH).days
    # genuine receipts only ever put a digit, M or N in the last slot of this
    # group (observed: 8541M, 43105, 62817, 53916, 8170N, 3800M)
    rand = ("".join(rng.choice("0123456789") for _ in range(4))
            + rng.choice("0123456789MN"))
    counter = f"0B10{rng.randint(1, 20):02d}"
    sbp = (rng.choice("AB") + f"{day:04d}" + utc.strftime("%H%M")
           + f"{transfer.second:02d}" + rand + counter
           + sbp_scheme_tail(utc.date()))
    assert len(sbp) == 32
    return sbp


def make_ref_number(op_date: datetime.date, rng: random.Random = random) -> str:
    return op_date.strftime("%Y%m%d") + f"{rng.randrange(10**7):07d}"


AUTH_LETTERS = "IJKLMNPRTZ"      # every letter seen in genuine codes, and only
                                 # those: RIR998 L47R8J 42T4PZ RK833L T0N5MR R9N031


def make_auth_code(rng: random.Random = random) -> str:
    """Six characters, two to four of them letters, as in every genuine code."""
    slots = rng.sample(range(6), rng.randint(2, 4))
    return "".join(rng.choice(AUTH_LETTERS) if i in slots
                   else rng.choice("0123456789") for i in range(6))


# ── field layout (keyed by the y-coordinate of the value run) ───────────────

def format_amount(value) -> str:
    """Money the way Java's ru_RU DecimalFormat writes it: comma decimal mark
    and U+00A0 between thousands groups (a plain space is the tell-tale sign of
    a hand-edited amount)."""
    if isinstance(value, str):
        clean = value.replace("\u00a0", "").replace(" ", "").replace(",", ".")
        value = float(clean)
    whole, frac = divmod(round(float(value) * 100), 100)
    groups = []
    while whole >= 1000:
        whole, tail = divmod(whole, 1000)
        groups.append(f"{tail:03d}")
    groups.append(str(whole))
    return "\u00a0".join(reversed(groups)) + f",{frac:02d}"


def build(data: dict, template: str = TEMPLATE, out_path: str = None,
          meta: dict = None, seed=None) -> bytes:
    """Render one receipt. `meta`, when given, receives the generated
    identifiers (Исх. №, код авторизации, СБП, /CreationDate, filename).

    `seed` makes the whole file deterministic: the same seed always yields the
    same identifiers, /CreationDate, /ID and font subset tags. Pass the payment
    id so one payment always opens the exact same receipt in the app."""
    rng = random.Random(seed) if seed is not None else random
    bank = Bank()
    pdf = open(template, "rb").read()
    objects, phys_order = parse_objects(pdf)

    transfer = datetime.datetime.strptime(data["transfer"], "%d.%m.%Y %H:%M:%S")
    ref_date = datetime.datetime.strptime(data["doc_date"], "%d.%m.%Y").date()

    missing_latin = [c for c in AUTH_LETTERS + "AB" if c not in bank.char2gid]
    if missing_latin:
        raise YandexReceiptError("в банке нет глифов для идентификаторов: "
                                 + " ".join(missing_latin))
    sbp_id = data.get("sbp_id") or make_sbp_id(transfer, rng)
    ref_no = data.get("ref_no") or make_ref_number(ref_date, rng)
    auth = data.get("auth_code") or make_auth_code(rng)

    # value text keyed by the run's y-coordinate (from the template layout)
    values = {
        812.72: f"Исх. № {ref_no}",
        797.72: f"Дата {ref_date:%d.%m.%Y}",
        736.72: f"{transfer:%d.%m.%Y} в {transfer:%H:%M}",
        686.72: data.get("status", "Выполнено"),
        661.72: data.get("bank_from", "Яндекс Банк"),
        636.72: data["phone_from"],
        611.72: data["fio_from"],
        586.72: data["phone_to"],
        561.72: data["fio_to"],
        536.72: data["bank_to"],
        486.72: f"{format_amount(data['amount'])} \u20bd",
        436.72: auth,
        411.72: sbp_id,
    }

    miss = set()
    for v in values.values():
        miss |= bank.missing(v)
    if miss:
        raise YandexReceiptError(
            "нет глифов для символов: " + "".join(sorted(miss)))

    # ── rewrite the content stream (obj 7) ──────────────────────────────────
    cdict, cdata = stream_of(objects[7])
    stream = zlib.decompress(cdata)

    run_re = re.compile(
        rb"(1 0 0 1 )([\d.]+)( )([\d.]+)( Tm\s*/F1 )(10)( Tf\s*0 0 0 rg\s*)\((.*?)\)(\s*Tj)",
        re.S)

    def repl(m):
        x = float(m.group(2)); y = float(m.group(4))
        # labels sit at x=20; only the right-hand value column is replaced, so a
        # label sharing a value's y-coordinate is never overwritten.
        if x < 200:
            return m.group(0)
        new_text = values.get(round(y, 2))
        if new_text is None:
            return m.group(0)
        raw = unescape_pdf_string(m.group(8))
        old_gids = [(b0 << 8) | b1 for b0, b1 in zip(raw[0::2], raw[1::2])]
        old_full = "".join(bank.gid2char.get(g, "") for g in old_gids)
        # Preserve the value's exact leading whitespace (JasperReports emits a
        # varying number of leading spaces per field); never inject our own.
        prefix = old_full[:len(old_full) - len(old_full.lstrip(" "))]
        new_full = prefix + new_text
        size = 10.0
        old_w = sum(bank.gid2width.get(g, 0) for g in old_gids) * size / bank.upm
        right = x + old_w
        new_x = right - bank.width(new_full, size)
        new_str = gids_to_pdf_string([bank.char2gid[c] for c in new_full])
        return (m.group(1) + fmt_num(new_x) + m.group(3) + m.group(4)
                + m.group(5) + m.group(6) + m.group(7) + new_str + m.group(9))

    new_stream = run_re.sub(repl, stream)

    # ── collect used F1 gids from the FINAL stream ───────────────────────────
    used = f1_used_gids(new_stream)

    objects[7] = set_stream(cdict, zlib.compress(new_stream, 6))

    # ── rebuild F1 font + dependents ─────────────────────────────────────────
    # Two distinct sets (as in genuine receipts):
    #   declared = GIDs actually referenced in the content stream — this is what
    #              CIDSet / /W / ToUnicode list (no .notdef, no components).
    #   keep     = declared + .notdef + composite components — the glyph OUTLINES
    #              the font must physically carry so composites render.
    declared = used
    # Copy the source font's head.checkSumAdjustment verbatim (OpenPDF does not
    # recompute it — it is constant across genuine receipts).
    src_font = zlib.decompress(stream_of(objects[11])[1])
    src_adj = None
    _num = _struct.unpack(">H", src_font[4:6])[0]
    for _i in range(_num):
        _tag, _cs, _off, _ln = _struct.unpack(">4sIII", src_font[12 + 16 * _i:28 + 16 * _i])
        if _tag == b"head":
            src_adj = _struct.unpack(">I", src_font[_off + 8:_off + 12])[0]
    font_bytes, keep = rebuild_font(declared, src_adj)
    fdict, _ = stream_of(objects[11])
    fdict = re.sub(rb"/Length1\s+\d+", b"/Length1 %d" % len(font_bytes), fdict)
    objects[11] = set_stream(fdict, zlib.compress(font_bytes, 6))

    csdict, _ = stream_of(objects[10])
    objects[10] = set_stream(csdict, zlib.compress(
        build_cidset(declared, bank.num_glyphs), 6))

    # /W is a nested array: [3[200]40[645 671]…]; match one level of nesting.
    objects[13] = re.sub(rb"/W \[(?:[^\[\]]|\[[^\]]*\])*\]",
                         build_w_array(declared, bank.gid2width), objects[13], flags=re.S)

    tdict, _ = stream_of(objects[14])
    objects[14] = set_stream(tdict, zlib.compress(
        build_tounicode(declared, bank.gid2char), 6))

    # ── file CreationDate ─────────────────────────────────────────────────────
    # The PDF is produced when the customer downloads the receipt, so creation
    # always follows the transfer (genuine gaps run from one minute to months)
    # and the document date is that moment in MSK. Times printed on the receipt
    # are MSK; /CreationDate is UTC (Z) and there is no /ModDate.
    if data.get("creation_date"):
        crdate = data["creation_date"].encode()
    else:
        earliest = transfer + datetime.timedelta(minutes=1)
        day_end = datetime.datetime.combine(ref_date, datetime.time(23, 59, 59))
        lo = max(earliest, datetime.datetime.combine(ref_date, datetime.time()))
        now_msk = msk_now()
        hi = min(earliest + datetime.timedelta(hours=8), day_end, now_msk)
        if hi < lo:
            raise YandexReceiptError("платёж слишком близко к текущему времени "
                                     f"(МСК сейчас {now_msk:%d.%m %H:%M})")
        created = lo + datetime.timedelta(
            seconds=rng.randint(0, int((hi - lo).total_seconds())))
        assert created >= transfer and created.date() == ref_date
        crdate = b"D:%sZ" % (created - datetime.timedelta(hours=3)
                             ).strftime("%Y%m%d%H%M%S").encode()
    objects[28] = re.sub(rb"/CreationDate\(D:[^)]*\)",
                         b"/CreationDate(%s)" % crdate, objects[28])

    # ── fresh font subset tags ────────────────────────────────────────────────
    # OpenPDF draws six random capitals per embedded subset, so no two genuine
    # receipts ever share one. Keeping the template's tags would fingerprint
    # every file we produce as a copy of the same document.
    tagged = [n for n in objects
              if objects[n] and (b"/BaseFont" in objects[n] or b"/FontName" in objects[n])]
    old_tags = sorted({m.group(1) for n in tagged
                       for m in re.finditer(rb"/([A-Z]{6})\+", objects[n])})
    fresh = set()
    for old in old_tags:
        while True:
            new = "".join(rng.choice(ALPHA_UPPER) for _ in range(6)).encode()
            if new not in fresh and new not in old_tags:
                break
        fresh.add(new)
        for n in tagged:
            objects[n] = objects[n].replace(old + b"+", new + b"+")

    # ── re-serialize (preserving OpenPDF's physical object order) ─────────────
    docid = (data.get("docid") or
             "".join(rng.choice("0123456789abcdef") for _ in range(32))).encode()
    result = serialize(objects, phys_order, root=27, info=28, docid=docid)

    logger.info("квитанция собрана: Исх. № %s, код %s, СБП %s, глифов %d, %d байт",
                ref_no, auth, sbp_id, len(used), len(result))
    if meta is not None:
        # a UUID filename like Yandex Bank's own; stable when a seed is given
        fid = uuid.UUID(int=rng.getrandbits(128), version=4) if seed is not None \
            else uuid.uuid4()
        meta.update(ref_no=ref_no, auth_code=auth, sbp_id=sbp_id,
                    creation_date=crdate.decode(), glyphs=len(used),
                    size=len(result), filename=f"{fid}.pdf")

    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(result)
    return result


def build_variants(data: dict, count: int = 3, template: str = TEMPLATE) -> list:
    """Render `count` receipts for the same payment, each with its own
    identifiers, /CreationDate, /ID and font subset tags.

    Fraudex accepts roughly half of the receipts we submit, and its verdict is
    fixed per file while being unrelated to any printed field: two files with
    byte-identical text have been accepted and rejected respectively. So the
    caller is meant to submit these one by one and keep the first that passes.
    """
    if count < 1:
        raise YandexReceiptError("count должен быть не меньше 1")
    out = []
    for _ in range(count):
        meta = {}
        pdf = build(data, template=template, meta=meta)
        out.append({"pdf": pdf, **meta})
    return out
