"""
Oracle BI Publisher PDF text replacement engine.

Oracle BI Publisher stores text in content streams as ASCII-hex GID sequences:
    <002F002E001B000800090013> Tj

Each 4-character hex group is a 2-byte Glyph ID.  The ToUnicode CMap (which is
zlib-compressed in the PDF) maps GIDs back to Unicode codepoints.

Standard binary patchers fail here because they:
  1. Cannot find the compressed CMap (extract_cmap() returns 0 entries).
  2. Encode text as raw bytes instead of ASCII-hex strings.
  3. Accidentally corrupt font streams.

This module provides the correct replacement pipeline for OBI PDFs.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
import logging
import io
from typing import Optional

logger = logging.getLogger(__name__)

# System Tahoma path (macOS default; falls back to other paths)
_TAHOMA_PATHS = [
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Tahoma.ttf",
    "/usr/share/fonts/truetype/tahoma.ttf",
    "/Windows/Fonts/tahoma.ttf",
]
_TAHOMA_PATH: Optional[str] = next(
    (p for p in _TAHOMA_PATHS if os.path.isfile(p)), None
)

# ---------------------------------------------------------------------------
# CMap extraction from compressed streams
# ---------------------------------------------------------------------------

_STREAM_RE = re.compile(rb"(/Length\s+(\d+)[^>]*>>)\s*stream\r?\n", re.DOTALL)


def _iter_streams(pdf: bytes):
    """Yield (length_start, length_end, data_start, raw_bytes) for each stream."""
    for m in _STREAM_RE.finditer(pdf):
        length = int(m.group(2))
        data_start = m.end()
        raw = pdf[data_start: data_start + length]
        yield m.start(2), m.end(2), data_start, data_start + length, raw


def _decompress_stream(raw: bytes) -> Optional[bytes]:
    try:
        return zlib.decompress(raw)
    except zlib.error:
        pass
    for trim in range(1, min(8, len(raw))):
        try:
            return zlib.decompress(raw[:-trim])
        except zlib.error:
            continue
    return None


def extract_cmap_from_pdf(pdf: bytes) -> dict[str, int]:
    """Extract from_unicode mapping {char: gid} by searching ALL streams
    (including compressed ones) for beginbfchar/endbfchar sections.

    Returns empty dict if no CMap found.
    """
    for _, _, _, _, raw in _iter_streams(pdf):
        # Try decompressed first, then raw
        for candidate in (_decompress_stream(raw), raw):
            if candidate is None:
                continue
            if b"beginbfchar" not in candidate:
                continue
            text = candidate.decode("latin-1", errors="replace")
            result: dict[str, int] = {}

            # beginbfchar
            for block in re.findall(
                r"beginbfchar\s*\n(.*?)endbfchar", text, re.DOTALL
            ):
                for gid_hex, uni_hex in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
                ):
                    gid = int(gid_hex, 16)
                    try:
                        uni = bytes.fromhex(uni_hex).decode("utf-16-be")
                    except Exception:
                        uni = chr(int(uni_hex, 16))
                    for ch in uni:
                        result[ch] = gid

            # beginbfrange
            for block in re.findall(
                r"beginbfrange\s*\n(.*?)endbfrange", text, re.DOTALL
            ):
                for s_hex, e_hex, d_hex in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
                    block,
                ):
                    start = int(s_hex, 16)
                    end = int(e_hex, 16)
                    dst = int(d_hex, 16)
                    for i in range(end - start + 1):
                        result[chr(dst + i)] = start + i

            if result:
                logger.debug("Extracted OBI CMap: %d entries", len(result))
                return result

    logger.warning("No CMap found in PDF")
    return {}


# ---------------------------------------------------------------------------
# Text encoding helpers
# ---------------------------------------------------------------------------


def _encode_as_hex_gids(text: str, cmap: dict[str, int]) -> str:
    """Encode a Unicode string as uppercase hex GID pairs (no < > delimiters).

    Falls back to ordinal value for characters not in the CMap.
    """
    parts: list[str] = []
    for ch in text:
        gid = cmap.get(ch)
        if gid is None:
            gid = ord(ch)
            logger.debug("Char %r not in CMap, using ordinal %d", ch, gid)
        parts.append(f"{gid:04X}")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Content stream text replacement
# ---------------------------------------------------------------------------

# Matches   <HEXDATA> Tj    or    <HEXDATA> TJ
_HEX_TJ_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*Tj")


def _replace_in_content(
    stream: bytes,
    old_hex: str,
    new_hex: str,
) -> tuple[bytes, int]:
    """Replace occurrences of `old_hex` (uppercase hex GIDs, no delimiters)
    inside `<...> Tj` sequences in the decompressed content stream.

    Supports both exact matches `<OLD>` and substring matches where the old
    hex appears inside a larger hex run `<PREFIX_OLD_SUFFIX>` — which happens
    when Oracle BI Publisher appends a trailing \\xa0 (000A) to field values.
    """
    count = 0
    old_b = old_hex.upper().encode()
    new_b = new_hex.upper().encode()

    if not old_b:
        return stream, 0

    # Strategy: search for old_hex as a substring within any <HEXDATA> block.
    # Pattern: <(optional prefix)(OLD_HEX)(optional suffix)>
    hex_chars = b"[0-9A-Fa-f]"
    pattern = (
        b"<(" + hex_chars + b"*)"
        + re.escape(old_b)
        + b"(" + hex_chars + b"*)>"
    )
    new_stream, n = re.subn(
        pattern,
        b"<\\g<1>" + new_b + b"\\g<2>>",
        stream,
    )
    if n > 0:
        stream = new_stream
        count += n

    return stream, count


# ---------------------------------------------------------------------------
# Font stream protection
# ---------------------------------------------------------------------------

_TRUETYPE_MAGIC = b"\x00\x01\x00\x00"  # sfVersion 1.0
_OTF_MAGIC = b"OTTO"
_TRUE_MAGIC = b"true"


def _is_font_stream(decompressed: bytes) -> bool:
    """Return True if this looks like a TrueType/OpenType font stream."""
    if len(decompressed) < 4:
        return False
    header = decompressed[:4]
    return header in (_TRUETYPE_MAGIC, _OTF_MAGIC, _TRUE_MAGIC)


# ---------------------------------------------------------------------------
# Length / xref fixups (adapted from patcher.py)
# ---------------------------------------------------------------------------


def _update_length(pdf: bytes, start: int, end: int, new_len: int) -> bytes:
    return pdf[:start] + str(new_len).encode() + pdf[end:]


def _fix_xref(pdf: bytes, delta: int, start_offset: int) -> bytes:
    xref_start = pdf.rfind(b"xref")
    if xref_start == -1:
        return pdf
    trailer_start = pdf.find(b"trailer", xref_start)
    if trailer_start == -1:
        trailer_start = len(pdf)
    xref_section = pdf[xref_start:trailer_start]
    lines = xref_section.split(b"\n")
    new_lines: list[bytes] = []
    for line in lines:
        stripped = line.strip()
        m = re.match(rb"^(\d{10}) (\d{5}) ([nf])\s*\r?$", stripped)
        if not m:
            m = re.match(rb"^(\d{10}) (\d{5}) ([nf])", stripped)
        if m:
            offset = int(m.group(1))
            gen = m.group(2).decode()
            flag = m.group(3).decode()
            if flag == "n" and offset >= start_offset:
                offset += delta
            new_lines.append(f"{offset:010d} {gen} {flag} \r".encode())
        else:
            new_lines.append(line)
    new_xref = b"\n".join(new_lines)
    return pdf[:xref_start] + new_xref + pdf[trailer_start:]


def _find_xref_table(pdf: bytes) -> int:
    """Offset of the real xref table, or -1.

    A plain rfind(b"xref") lands inside the trailing "startxref" keyword, which
    sits *after* the table — hence the negative lookbehind.
    """
    hits = [m.start() for m in re.finditer(rb"(?<!start)xref[\r\n]", pdf)]
    return hits[-1] if hits else -1


def _update_startxref(pdf: bytes) -> bytes:
    xref_pos = _find_xref_table(pdf)
    if xref_pos == -1:
        return pdf
    m = re.search(rb"startxref\s*[\r\n]+\s*\d+", pdf)
    if m:
        pdf = pdf[: m.start()] + b"startxref\r\n%d" % xref_pos + pdf[m.end():]
    return pdf


def _rebuild_xref_table(pdf: bytes, style: Optional[str] = None) -> bytes:
    """Rewrite the xref table from the objects' actual byte offsets.

    Every stream replacement shifts the objects that follow it, and tracking
    those shifts through the whole pipeline is error-prone.  Reading the real
    offsets back out of the finished file is exact.

    style:
      "obi"   — Oracle BI Publisher receipts: 20-byte CRLF entries, no
                trailing space (`0000000015 00000 n\\r\\n`), `startxref\\r\\n`.
      "itext" — iText 4.2.0 (Alfa statements): 20-byte LF entries with a
                trailing space (`0000000015 00000 n \\n`), `startxref\\n`.
      None    — detect from /ITXT marker (statements have it, receipts don't).
    """
    if style is None:
        style = "itext" if b"/ITXT" in pdf else "obi"
    nl = b"\n" if style == "itext" else b"\r\n"
    # iText pads each 20-byte entry with a space before the line ending.
    pad = b" " if style == "itext" else b""

    xref_start = _find_xref_table(pdf)
    if xref_start == -1:
        return pdf
    header = re.match(rb"xref\r?\n(\d+)\s+(\d+)\r?\n", pdf[xref_start:])
    trailer_pos = pdf.find(b"trailer", xref_start)
    if not header or trailer_pos == -1:
        return pdf
    count = int(header.group(2))

    offsets: dict = {}
    for m in re.finditer(rb"(?:\A|[\r\n])(\d+)\s+0\s+obj", pdf):
        offsets[int(m.group(1))] = m.start(1)

    def _entry(offset: int, gen: int, flag: str) -> bytes:
        return b"%010d %05d %s%s%s" % (offset, gen, flag.encode(), pad, nl)

    entries = [_entry(0, 65535, "f")]
    missing = []
    for num in range(1, count):
        off = offsets.get(num)
        if off is None:
            missing.append(num)
            entries.append(_entry(0, 65535, "f"))
        else:
            entries.append(_entry(off, 0, "n"))
    if missing:
        logger.warning("_rebuild_xref_table: objects not found in body: %s", missing)

    new_table = b"xref%s0 %d%s" % (nl, count, nl) + b"".join(entries)
    trailer = pdf[trailer_pos:]
    trailer = re.sub(
        rb"startxref\s*[\r\n]+\s*\d+",
        b"startxref%s%d" % (nl, xref_start),
        trailer,
        count=1,
    )
    return pdf[:xref_start] + new_table + trailer


def _patch_docid_moddate(pdf: bytes, receipt_datetime: Optional[str] = None) -> bytes:
    """Fully randomize /ID (both entries identical, matching OBI convention),
    update /CreationDate and /ModDate to the receipt's actual date/time.

    The original template /ID is a unique fingerprint that fraud checkers
    use to identify known-forged PDFs.  Replacing both entries with fresh
    random bytes ensures each generated PDF has a unique, unrecognizable ID.

    receipt_datetime: optional 'DD.MM.YYYY HH:MM:SS' string extracted from
        the receipt content.  Used to set realistic /CreationDate and /ModDate.
    """
    import os
    from datetime import datetime, timezone, timedelta

    # --- Generate fresh random /ID ---
    # Oracle BI Publisher sets ID[0] at creation; iText 4.2.0 (which post-
    # processes genuine Alfa Bank statements) assigns a *different* ID[1]
    # when it modifies the file.  Using two identical IDs is a detectable
    # forensic signal — generate two independent 16-byte random values.
    id0_hex = os.urandom(16).hex().lower().encode()  # OBI original ID
    id1_hex = os.urandom(16).hex().lower().encode()  # iText-assigned ID
    new_id_hex = id0_hex  # keep for logging

    # Match the FULL /ID entry including the closing >] to avoid leaving
    # orphan brackets (the original may use ><  or > < spacing).
    m = re.search(rb"/ID\s*\[(?:\s*<[0-9A-Fa-f]+>\s*)+\]", pdf)
    if m:
        old_entry = pdf[m.start():m.end()]
        new_entry = b"/ID [<" + id0_hex + b"><" + id1_hex + b">]"
        pdf = pdf[: m.start()] + new_entry + pdf[m.end():]
        delta = len(new_entry) - len(old_entry)
        if delta:
            pdf = _fix_xref(pdf, delta, m.start())
            pdf = _update_startxref(pdf)
    else:
        logger.warning("_patch_docid_moddate: /ID not found in PDF")

    # --- Determine realistic date for receipt ---
    # Use the "Сформирована" time (HH:MM) with seconds=00 so the metadata
    # exactly matches what the checker reads from the receipt content.
    # BOTH /CreationDate and /ModDate must be IDENTICAL — OBI creates the PDF
    # once at the payment moment and never modifies it.
    receipt_dt: Optional[datetime] = None
    if receipt_datetime:
        # First try HH:MM:SS, then HH:MM (prefer the shorter one for :00 seconds)
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
            try:
                receipt_dt = datetime.strptime(receipt_datetime, fmt)
                if fmt == "%d.%m.%Y %H:%M":
                    break  # Prefer minute-level (seconds=00) to match checker
            except ValueError:
                continue

    if receipt_dt is None:
        receipt_dt = datetime.now().replace(second=0, microsecond=0)

    tz_offset = "+03'00'"  # Moscow time (Alfa-Bank)
    # SAME value for both — no 2-second offset
    date_str = receipt_dt.strftime(f"D:%Y%m%d%H%M%S{tz_offset}").encode()
    creation_str = date_str
    mod_str = date_str

    # Keep the original spacing between the key and '('.  iText writes
    # `/ModDate(D:...)` with no space; injecting one is a forensic tell.
    def _replace_date(src: bytes, key: bytes, value: bytes) -> bytes:
        m = re.search(key + rb"(\s*)\(D:[^)]*\)", src)
        if not m:
            return src
        return src[: m.start()] + key + m.group(1) + b"(" + value + b")" + src[m.end():]

    pdf = _replace_date(pdf, b"/CreationDate", creation_str)
    pdf = _replace_date(pdf, b"/ModDate", mod_str)

    # NOTE: Do NOT insert /CreationDate or /ModDate when they are absent.
    # Genuine Oracle BI Publisher receipts have neither field; injecting them
    # creates a date that the checker compares against the payment datetime and
    # flags as a mismatch ("Дата платежа не совпадает с созданием файла").

    logger.debug(
        "_patch_docid_moddate: new /ID=%s, dates=%s/%s",
        new_id_hex[:8].decode(), creation_str.decode(), mod_str.decode(),
    )
    return pdf


# ---------------------------------------------------------------------------
# Font extension — add missing glyphs from system Tahoma
# ---------------------------------------------------------------------------

# Thread-local storage for the subset prefix generated during font extension,
# so obi_patch_pdf can retrieve it and update /BaseFont & /FontName in the PDF.
_LAST_SUBSET_PREFIX: list[str] = ["OPMVEA"]  # default = template's original prefix


def _expand_to_simple(glyph_name: str, glyf_table, hmtx_table):
    """Recursively expand a (possibly composite) glyph into a simple glyph
    with all component contours merged.  Returns (simple_Glyph, advance_width).

    This avoids relying on composite component references when embedding the
    glyph into a different font (where component GIDs may differ).
    """
    import copy
    from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates

    glyph = glyf_table[glyph_name]
    adv_w = hmtx_table[glyph_name][0]

    if not glyph.isComposite():
        return copy.deepcopy(glyph), adv_w

    all_coords: list[tuple[int, int]] = []
    all_flags: list[int] = []
    all_end_pts: list[int] = []
    pt_offset = 0

    for comp in glyph.components:
        child, _ = _expand_to_simple(comp.glyphName, glyf_table, hmtx_table)
        if child.numberOfContours <= 0:
            continue
        coords, end_pts, flags = child.getCoordinates(glyf_table)
        dx, dy = comp.x, comp.y
        moved = [(int(x + dx), int(y + dy)) for (x, y) in coords]
        all_coords.extend(moved)
        all_flags.extend(list(flags))
        all_end_pts.extend(ep + pt_offset for ep in end_pts)
        pt_offset += len(moved)

    from fontTools.ttLib.tables.ttProgram import Program as _TTProg

    if not all_end_pts:
        g = Glyph()
        g.numberOfContours = 0
        g.program = _TTProg()  # empty hinting program
        return g, adv_w

    simple = Glyph()
    simple.numberOfContours = len(all_end_pts)
    simple.coordinates = GlyphCoordinates(all_coords)
    simple.flags = bytearray(all_flags)
    simple.endPtsOfContours = all_end_pts
    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    simple.xMin = min(xs); simple.xMax = max(xs)
    simple.yMin = min(ys); simple.yMax = max(ys)
    # Empty hinting program — no instructions needed for new glyphs
    simple.program = _TTProg()
    return simple, adv_w


def extend_font_for_chars(
    font_bytes: bytes,
    missing_chars: set[str],
    tahoma_path: Optional[str] = None,
) -> tuple[bytes, dict[str, int]]:
    """Extend an Oracle BI Publisher Tahoma font subset with additional glyphs.

    Copies the required glyphs from system Tahoma, decomposing any composite
    glyphs into simple outlines first.  This prevents Acrobat rendering issues
    caused by unresolved component references in CID Type2 fonts.

    Returns (extended_font_bytes, new_char_to_gid_dict) where
    new_char_to_gid_dict maps only the NEWLY added characters to their GIDs
    (the existing CMap entries are unchanged).
    """
    if not missing_chars:
        return font_bytes, {}

    tahoma_path = tahoma_path or _TAHOMA_PATH
    if not tahoma_path:
        logger.warning("extend_font_for_chars: system Tahoma not found — skipping")
        return font_bytes, {}

    try:
        from fontTools import ttLib as _ft
    except ImportError:
        logger.warning("extend_font_for_chars: fontTools not available — skipping")
        return font_bytes, {}

    try:
        sys_tt = _ft.TTFont(tahoma_path)
        sys_cmap = sys_tt.getBestCmap()
        sys_glyf = sys_tt["glyf"]
        sys_hmtx = sys_tt["hmtx"]

        obi = _ft.TTFont(io.BytesIO(font_bytes))
        obi_glyf = obi["glyf"]
        obi_hmtx = obi["hmtx"]
        existing_order = list(obi.getGlyphOrder())
        gname_to_gid: dict[str, int] = {n: i for i, n in enumerate(existing_order)}
        next_gid = obi["maxp"].numGlyphs

        # Map char → system glyph name for each missing char
        direct: dict[str, str] = {}
        for ch in missing_chars:
            cp = ord(ch)
            sys_gname = sys_cmap.get(cp)
            if sys_gname is None:
                logger.debug("extend_font: U+%04X %r not in system Tahoma", cp, ch)
                continue
            direct[ch] = sys_gname

        # Add each glyph as a SIMPLE (decomposed) outline — no composite refs
        new_order = list(existing_order)
        for sys_gname in sorted(direct.values()):
            if sys_gname in gname_to_gid:
                continue  # already present in OBI font

            simple_glyph, adv_w = _expand_to_simple(sys_gname, sys_glyf, sys_hmtx)
            lsb = sys_hmtx[sys_gname][1]

            obi_glyf[sys_gname] = simple_glyph
            obi_hmtx.metrics[sys_gname] = (adv_w, lsb)
            new_order.append(sys_gname)
            gname_to_gid[sys_gname] = next_gid
            logger.debug(
                "extend_font: added '%s' (U+%04X) as GID %d (simple, %d contours)",
                sys_gname, ord(ch) if len(direct) == 1 else 0,
                next_gid, simple_glyph.numberOfContours,
            )
            next_gid += 1

        obi.setGlyphOrder(new_order)
        obi["maxp"].numGlyphs = next_gid

        # Inject a random XXXXXX+Tahoma name table (matching Oracle BI Publisher
        # convention).  This makes the embedded font hash unique per document,
        # preventing content-based fingerprint matching across generated PDFs.
        import random as _rnd, string as _str
        subset_prefix = "".join(_rnd.choices(_str.ascii_uppercase, k=6))
        subset_name = f"{subset_prefix}+Tahoma"
        try:
            from fontTools.ttLib.tables._n_a_m_e import NameRecord as _NR
            nt = _ft.newTable("name")
            nt.names = []
            for nameID, val in [
                (1, "Tahoma"),
                (2, "Regular"),
                (4, subset_name),
                (6, subset_name),
            ]:
                rec = _NR()
                rec.nameID = nameID
                rec.platformID = 3   # Windows
                rec.platEncID = 1    # Unicode BMP
                rec.langID = 0x409   # English US
                rec.string = val.encode("utf-16-be")
                nt.names.append(rec)
            obi["name"] = nt
        except Exception as _ne:
            logger.debug("Could not add name table: %s", _ne)

        buf = io.BytesIO()
        obi.save(buf)
        extended_bytes = buf.getvalue()

        # Return the subset prefix so the caller can patch /BaseFont & /FontName
        # in the PDF to match the new random name.
        _LAST_SUBSET_PREFIX[0] = subset_prefix

        new_char_to_gid = {
            ch: gname_to_gid[sys_gname]
            for ch, sys_gname in direct.items()
            if sys_gname in gname_to_gid
        }
        logger.info(
            "extend_font: added %d simple glyph(s); mapped %d new char(s): %s",
            next_gid - obi["maxp"].numGlyphs + (next_gid - obi["maxp"].numGlyphs),
            len(new_char_to_gid),
            " ".join(f"{ch!r}→GID{gid}" for ch, gid in new_char_to_gid.items()),
        )
        return extended_bytes, new_char_to_gid

    except Exception as exc:
        logger.warning("extend_font_for_chars failed: %s", exc)
        return font_bytes, {}


def _update_glyph_widths_in_pdf(
    pdf_bytes: bytes,
    new_gid_to_advw: dict[int, int],
    units_per_em: int = 2048,
) -> bytes:
    """Append new glyph width entries to the /W array in the PDF's CID font dict.

    PDF glyph widths are in units of 1/1000 of the em square.
    new_gid_to_advw: {gid: advance_width_in_font_units}
    """
    if not new_gid_to_advw:
        return pdf_bytes

    m = re.search(rb"/W\s*\[", pdf_bytes)
    if not m:
        logger.warning("_update_glyph_widths: /W array not found — new glyphs may show wrong width")
        return pdf_bytes

    # Find closing bracket of the /W array
    depth = 0
    w_end = -1
    for i, b in enumerate(pdf_bytes[m.end() - 1:], start=m.end() - 1):
        if b == ord(b"["):
            depth += 1
        elif b == ord(b"]"):
            depth -= 1
            if depth == 0:
                w_end = i
                break

    if w_end == -1:
        logger.warning("_update_glyph_widths: could not find end of /W array")
        return pdf_bytes

    # Build new entries:  GID [pdf_width]
    scale = 1000.0 / units_per_em
    new_entries = b""
    for gid in sorted(new_gid_to_advw):
        pdf_w = round(new_gid_to_advw[gid] * scale)
        new_entries += f" {gid} [{pdf_w}]".encode()

    # Insert before the closing ] of /W
    old_len = len(pdf_bytes)
    pdf_bytes = pdf_bytes[:w_end] + new_entries + pdf_bytes[w_end:]
    delta = len(pdf_bytes) - old_len

    if delta != 0:
        pdf_bytes = _fix_xref(pdf_bytes, delta, m.start())
        pdf_bytes = _update_startxref(pdf_bytes)

    logger.debug(
        "_update_glyph_widths: added %d entries to /W array",
        len(new_gid_to_advw),
    )
    return pdf_bytes


def _update_cmap_stream_in_pdf(
    pdf_bytes: bytes,
    new_char_to_gid: dict[str, int],
) -> bytes:
    """Append new bfchar entries to the ToUnicode CMap stream in the PDF.

    Finds the (compressed) CMap stream, decompresses it, appends new
    `beginbfchar ... endbfchar` entries, recompresses, and updates /Length.
    """
    if not new_char_to_gid:
        return pdf_bytes

    total_delta = 0

    for len_start, len_end, data_start, data_end, raw in _iter_streams(pdf_bytes):
        d = _decompress_stream(raw)
        if d is None:
            d = raw
        if b"beginbfchar" not in d:
            continue

        # Build new bfchar block
        new_entries = "\n".join(
            f"<{gid:04X}> <{ord(ch):04X}>"
            for ch, gid in new_char_to_gid.items()
        )
        new_block = (
            f"\n{len(new_char_to_gid)} beginbfchar\n"
            f"{new_entries}\n"
            f"endbfchar"
        ).encode()

        # Insert before "endcmap"
        insert_pos = d.find(b"endcmap")
        if insert_pos == -1:
            continue
        new_d = d[:insert_pos] + new_block + b"\n" + d[insert_pos:]

        # Determine if original was compressed
        was_compressed = _decompress_stream(raw) is not None
        if was_compressed:
            new_raw = zlib.compress(new_d, level=6)
        else:
            new_raw = new_d

        old_len = len(raw)
        new_len = len(new_raw)
        delta = new_len - old_len

        adj_len_start = len_start + total_delta
        adj_len_end   = len_end   + total_delta
        adj_data_start = data_start + total_delta
        adj_data_end   = data_end   + total_delta

        pdf_bytes = (
            pdf_bytes[:adj_data_start]
            + new_raw
            + pdf_bytes[adj_data_end:]
        )
        old_len_bytes = str(old_len).encode()
        new_len_bytes = str(new_len).encode()
        pdf_bytes = _update_length(pdf_bytes, adj_len_start, adj_len_end, new_len)
        len_delta = len(new_len_bytes) - len(old_len_bytes)
        total_delta += delta + len_delta

        logger.debug(
            "_update_cmap_stream: added %d entries, delta=%d", len(new_char_to_gid), total_delta
        )
        break  # only one CMap stream expected

    if total_delta != 0:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, 0)
        pdf_bytes = _update_startxref(pdf_bytes)

    return pdf_bytes


# ---------------------------------------------------------------------------
# Orphan glyph removal — prevent "Подмена текста" detection
# ---------------------------------------------------------------------------


def _collect_used_gids(pdf_bytes: bytes) -> set:
    """Return the set of all GIDs (int) referenced in any content stream.

    Scans every decompressed content stream for ``<HEX> Tj`` patterns and
    collects each 16-bit GID value encoded in the hex string.
    Font streams are skipped so their binary contents don't pollute the result.
    """
    used: set = set()
    for _, _, _, _, raw in _iter_streams(pdf_bytes):
        d = _decompress_stream(raw)
        if d is None:
            continue
        if _is_font_stream(d):
            continue
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", d):
            hex_str = m.group(1).decode()
            for i in range(0, len(hex_str), 4):
                gid = int(hex_str[i:i + 4], 16)
                used.add(gid)
    logger.debug("_collect_used_gids: %d distinct GIDs referenced in content", len(used))
    return used


def _remove_orphan_entries(pdf_bytes: bytes, used_gids: set) -> bytes:
    """Remove orphan GIDs from the embedded CMap and zero out their glyph data.

    An *orphan GID* is one that has a ToUnicode CMap entry but is never
    referenced in any content stream — a direct forensic fingerprint of
    template-based text substitution ("Подмена текста").

    This function:
    1. Rebuilds the ToUnicode CMap stream to include only entries for GIDs
       that actually appear in the document content (plus GID 0 / .notdef).
    2. Zeros out the glyph outline and advance width for each orphan GID in
       the embedded TrueType font, so the font contains no recognisable shapes
       for characters absent from the document.
    """
    # ── Determine which GIDs are orphaned ────────────────────────────────────
    cmap = extract_cmap_from_pdf(pdf_bytes)
    cmap_gids = set(cmap.values())
    gids_to_remove = cmap_gids - used_gids - {0}   # always keep .notdef (GID 0)

    if not gids_to_remove:
        logger.debug("_remove_orphan_entries: no orphan GIDs found — nothing to do")
        return pdf_bytes

    logger.info(
        "_remove_orphan_entries: removing %d orphan GID(s): %s",
        len(gids_to_remove),
        sorted(gids_to_remove),
    )

    total_delta = 0

    # ── Step 1: rebuild the ToUnicode CMap as a single merged block ──────────
    # Genuine Oracle BI Publisher PDFs always have exactly ONE beginbfchar block.
    # Our pipeline appends a second block for newly-added characters; merging
    # them into one block makes the CMap structurally indistinguishable from an
    # authentic receipt and also removes the orphan entries in one pass.
    for len_start, len_end, data_start, data_end, raw in _iter_streams(pdf_bytes):
        d = _decompress_stream(raw)
        if d is None:
            d = raw
        if b"beginbfchar" not in d:
            continue

        text = d.decode("latin-1", errors="replace")

        # Collect ALL entries from ALL beginbfchar blocks, filtering orphans
        all_entries: dict = {}   # gid (int) → unicode hex string (upper)
        for block_m in re.finditer(
            r"\d+\s+beginbfchar\s*\n(.*?)endbfchar", text, re.DOTALL
        ):
            for gid_hex, uni_hex in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block_m.group(1)
            ):
                gid = int(gid_hex, 16)
                if gid not in gids_to_remove:
                    all_entries[gid] = uni_hex.upper()

        # Sort by GID — matches the ascending order OBI uses
        sorted_entries = sorted(all_entries.items())
        entry_lines = "\n".join(
            f"<{gid:04X}> <{uni}>" for gid, uni in sorted_entries
        )
        merged_block = (
            f"{len(sorted_entries)} beginbfchar\n"
            f"{entry_lines}\n"
            f"endbfchar"
        )

        # Replace the span from the first block header to the last endbfchar
        first_m = re.search(r"\d+\s+beginbfchar", text)
        last_end_m = None
        for m in re.finditer(r"endbfchar", text):
            last_end_m = m
        if first_m is None or last_end_m is None:
            break

        new_text = (
            text[:first_m.start()]
            + merged_block
            + "\n"
            + text[last_end_m.end():]
        )

        new_d = new_text.encode("latin-1")
        was_compressed = _decompress_stream(raw) is not None
        new_raw = zlib.compress(new_d, level=6) if was_compressed else new_d

        adj_ls = len_start  + total_delta
        adj_le = len_end    + total_delta
        adj_ds = data_start + total_delta
        adj_de = data_end   + total_delta

        pdf_bytes = pdf_bytes[:adj_ds] + new_raw + pdf_bytes[adj_de:]
        pdf_bytes = _update_length(pdf_bytes, adj_ls, adj_le, len(new_raw))
        len_delta = len(str(len(new_raw)).encode()) - len(str(len(raw)).encode())
        total_delta += (len(new_raw) - len(raw)) + len_delta

        logger.debug(
            "_remove_orphan_entries: CMap merged into 1 block (%d entries, delta=%d)",
            len(sorted_entries), total_delta,
        )
        break   # only one ToUnicode CMap expected

    if total_delta:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, 0)
        pdf_bytes = _update_startxref(pdf_bytes)
        total_delta = 0

    # ── Step 2: zero out orphan glyph outlines in the embedded TrueType font ──
    try:
        from fontTools import ttLib as _ft
        from fontTools.ttLib.tables._g_l_y_f import Glyph as _Glyph

        for len_start, len_end, data_start, data_end, raw in _iter_streams(pdf_bytes):
            d = _decompress_stream(raw)
            if d is None:
                continue
            if not _is_font_stream(d) or len(d) < 1000:
                continue

            tt = _ft.TTFont(io.BytesIO(d))
            order = tt.getGlyphOrder()
            glyf_t = tt["glyf"]
            hmtx_t = tt["hmtx"]

            zeroed = 0
            for gid in gids_to_remove:
                if gid >= len(order):
                    continue
                gname = order[gid]
                # Replace with an empty (zero-contour) glyph and zero width
                empty_g = _Glyph()
                empty_g.numberOfContours = 0
                glyf_t[gname] = empty_g
                hmtx_t.metrics[gname] = (0, 0)
                zeroed += 1

            if zeroed == 0:
                break   # nothing to do for the font

            buf = io.BytesIO()
            tt.save(buf)
            new_d = buf.getvalue()
            new_raw = zlib.compress(new_d, level=6)

            adj_ls = len_start  + total_delta
            adj_le = len_end    + total_delta
            adj_ds = data_start + total_delta
            adj_de = data_end   + total_delta

            pdf_bytes = pdf_bytes[:adj_ds] + new_raw + pdf_bytes[adj_de:]
            pdf_bytes = _update_length(pdf_bytes, adj_ls, adj_le, len(new_raw))
            len_delta = len(str(len(new_raw)).encode()) - len(str(len(raw)).encode())
            total_delta += (len(new_raw) - len(raw)) + len_delta

            logger.info(
                "_remove_orphan_entries: zeroed %d orphan glyph(s) in font", zeroed
            )
            break   # only one font stream expected

    except Exception as exc:
        logger.warning("_remove_orphan_entries: font glyph zeroing failed: %s", exc)

    if total_delta:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, 0)
        pdf_bytes = _update_startxref(pdf_bytes)

    return pdf_bytes


# ---------------------------------------------------------------------------
# Genuine glyph bank — authentic Oracle glyph outlines harvested from real PDFs
# ---------------------------------------------------------------------------
#
# Oracle BI Publisher subsets Tahoma 3.14, while modern systems only ship
# Tahoma 5.01 / 6.98.  The versions differ in the global hinting tables
# (fpgm/prep/cvt) and in individual glyph outlines, so a subset built from the
# system font can never be byte-identical to a genuine one.
#
# Instead of guessing Oracle's subsetter we harvest the real thing: every
# genuine receipt carries an embedded Tahoma 3.14 subset.  Their union gives a
# bank of authentic glyphs (outlines + hinting bytecode + metrics), and one of
# them serves as the structural base donating fpgm/prep/cvt/head/hhea verbatim.
#
# The union of all available receipts is pre-built into tahoma314_bank.ttf by
# build_glyph_bank.py; the PDF list below is only a fallback for when that file
# is absent.

_BANK_TTF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tahoma314_bank.ttf")
_BANK_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tahoma314_bank.json")

_GENUINE_FONT_SOURCES = [
    "/Users/aleksandrzerebatav/Desktop/документы/Новая папка/Квитанция 1.pdf",
    "/Users/aleksandrzerebatav/Downloads/Документ.pdf",
    "/Users/aleksandrzerebatav/Downloads/AM_1788461201823_patched.pdf",
]

# char → {"glyph", "width", "components": [(glyph, width), …]}
_GLYPH_BANK: Optional[dict] = None
_GLYPH_BANK_BASE: Optional[bytes] = None   # raw TTF bytes of the structural donor


def _sfnt_layout(font_bytes: bytes) -> tuple[list, Optional[bytes]]:
    """Return (tags in physical file order, raw head.checkSumAdjustment bytes).

    Oracle lays the tables out in tag order and copies head verbatim from the
    full Tahoma, so its checkSumAdjustment does NOT satisfy the spec formula.
    fontTools would recompute it — reproducing the original value keeps that
    authentic quirk.
    """
    try:
        num = struct.unpack(">H", font_bytes[4:6])[0]
        entries, head_off = [], None
        for i in range(num):
            tag, _cs, off, _ln = struct.unpack(">4sIII", font_bytes[12 + 16 * i:28 + 16 * i])
            tag = tag.decode("latin-1")
            entries.append((off, tag))
            if tag == "head":
                head_off = off
        order = [t for _o, t in sorted(entries)]
        adj = font_bytes[head_off + 8:head_off + 12] if head_off is not None else None
        return order, adj
    except Exception:
        return [], None


def save_font_like_donor(tt, donor_bytes: bytes) -> bytes:
    """Compile a TTFont the way Oracle writes its embedded fonts.

    Three things differ from a plain fontTools save:
      * loca is forced to the long (32-bit) format — Oracle always uses it,
        while fontTools picks the short one for small glyf tables;
      * tables are laid out in the donor's physical order rather than
        fontTools's own "optimized" order;
      * head.checkSumAdjustment is copied from the donor, because Oracle
        inherits it from the full Tahoma and never recomputes it (so a
        spec-conforming checksum is itself a sign of a rebuilt font).
    """
    import array as _array
    import sys as _sys
    from fontTools.ttLib.tables import _l_o_c_a as _loca_mod

    def _compile_long_loca(self, ttFont):
        locations = _array.array("I", self.locations)
        if _sys.byteorder != "big":
            locations.byteswap()
        ttFont["head"].indexToLocFormat = 1
        return locations.tobytes()

    buf = io.BytesIO()
    saved = _loca_mod.table__l_o_c_a.compile
    _loca_mod.table__l_o_c_a.compile = _compile_long_loca
    try:
        tt.save(buf, reorderTables=False)
    finally:
        _loca_mod.table__l_o_c_a.compile = saved

    donor_order, donor_adj = _sfnt_layout(donor_bytes)
    if donor_order:
        try:
            from fontTools.ttLib import reorderFontTables as _reorder
            buf.seek(0)
            ordered = io.BytesIO()
            _reorder(buf, ordered, tableOrder=donor_order)
            buf = ordered
        except Exception as exc:
            logger.debug("save_font_like_donor: table reorder failed: %s", exc)

    out = buf.getvalue()
    if donor_adj:
        num = struct.unpack(">H", out[4:6])[0]
        for i in range(num):
            tag, _cs, off, _ln = struct.unpack(">4sIII", out[12 + 16 * i:28 + 16 * i])
            if tag == b"head":
                out = out[:off + 8] + donor_adj + out[off + 12:]
                break
    return out


def _extract_font_bytes(pdf: bytes) -> Optional[bytes]:
    """Return the decompressed embedded TrueType font program from a PDF."""
    for _, _, _, _, raw in _iter_streams(pdf):
        d = _decompress_stream(raw)
        if d is not None and _is_font_stream(d):
            return d
    return None


def _load_prebuilt_bank() -> tuple[dict, Optional[bytes]]:
    """Load the pre-built glyph bank (tahoma314_bank.ttf + .json).

    Glyphs are addressed by GID because the bank font carries no post/cmap
    table, so fontTools auto-names its glyphs on load.
    """
    if not (os.path.isfile(_BANK_TTF) and os.path.isfile(_BANK_JSON)):
        return {}, None
    try:
        import json as _json
        import copy as _copy
        from fontTools import ttLib as _ft

        with open(_BANK_TTF, "rb") as fh:
            base = fh.read()
        with open(_BANK_JSON, encoding="utf-8") as fh:
            meta = _json.load(fh)

        tt = _ft.TTFont(io.BytesIO(base))
        order = tt.getGlyphOrder()
        glyf, hmtx = tt["glyf"], tt["hmtx"]
        for name in order:
            _ = glyf[name]

        bank: dict = {}
        for ch, entry in meta.get("chars", {}).items():
            gid = entry["gid"]
            if gid <= 0 or gid >= len(order):
                continue
            glyph = _copy.deepcopy(glyf[order[gid]])
            comps = [
                (_copy.deepcopy(glyf[order[cg]]), hmtx[order[cg]][0])
                for cg in entry.get("comp_gids", [])
                if 0 < cg < len(order)
            ]
            bank[ch] = {
                "glyph": glyph,
                "width": entry["width"],
                "components": comps,
            }
        logger.info("_load_prebuilt_bank: %d authentic glyphs from %d donor receipt(s)",
                    len(bank), meta.get("donors", 0))
        return bank, base
    except Exception as exc:
        logger.warning("_load_prebuilt_bank failed: %s", exc)
        return {}, None


def _harvest_glyph_bank() -> tuple[dict, Optional[bytes]]:
    """Return the authentic glyph bank, preferring the pre-built donor font.

    Returns (bank, base_font_bytes).  Cached after the first call.
    """
    global _GLYPH_BANK, _GLYPH_BANK_BASE
    if _GLYPH_BANK is not None:
        return _GLYPH_BANK, _GLYPH_BANK_BASE

    bank, base = _load_prebuilt_bank()
    if bank:
        _GLYPH_BANK, _GLYPH_BANK_BASE = bank, base
        return bank, base

    bank, base = {}, None
    try:
        from fontTools import ttLib as _ft
    except ImportError:
        _GLYPH_BANK, _GLYPH_BANK_BASE = bank, base
        return bank, base

    import copy as _copy

    for src in _GENUINE_FONT_SOURCES:
        if not os.path.isfile(src):
            continue
        try:
            with open(src, "rb") as fh:
                pdf = fh.read()
            font_bytes = _extract_font_bytes(pdf)
            cmap = extract_cmap_from_pdf(pdf)     # char → gid
            if not font_bytes or not cmap:
                continue

            tt = _ft.TTFont(io.BytesIO(font_bytes))
            order = tt.getGlyphOrder()
            glyf, hmtx = tt["glyf"], tt["hmtx"]
            # Force full decompilation before we copy anything out.
            for gname in order:
                _ = glyf[gname]

            if base is None:
                base = font_bytes

            for ch, gid in cmap.items():
                if ch in bank or gid <= 0 or gid >= len(order):
                    continue
                gname = order[gid]
                glyph = _copy.deepcopy(glyf[gname])
                comps = []
                if glyph.isComposite():
                    for comp in glyph.components:
                        cn = comp.glyphName
                        if cn not in glyf:
                            comps = None
                            break
                        comps.append(
                            (_copy.deepcopy(glyf[cn]), hmtx.metrics[cn][0])
                        )
                    if comps is None:
                        continue          # broken reference — skip this char
                bank[ch] = {
                    "glyph": glyph,
                    "width": hmtx.metrics[gname][0],
                    "components": comps,
                }
        except Exception as exc:
            logger.debug("_harvest_glyph_bank: %s failed: %s", src, exc)

    logger.info(
        "_harvest_glyph_bank: %d authentic glyphs harvested from %d sources",
        len(bank), sum(1 for s in _GENUINE_FONT_SOURCES if os.path.isfile(s)),
    )
    _GLYPH_BANK, _GLYPH_BANK_BASE = bank, base
    return bank, base


# ---------------------------------------------------------------------------
# Complete font rebuild — sequential GIDs, no gaps, single CMap block
# ---------------------------------------------------------------------------


def _rebuild_font_and_remap(pdf_bytes: bytes, tahoma_path: Optional[str] = None) -> bytes:
    """Rebuild the embedded font from scratch so the PDF is forensically clean.

    After text substitutions the font has GID gaps (removed chars) and new
    chars appended at high GIDs — both signals that a forensic checker can
    detect as "Подмена текста".

    This function eliminates all such signals in one pass:
    1. Decodes the current content streams to Unicode using the (extended) CMap.
    2. Creates a fresh Tahoma subset containing *only* those characters, with
       sequential GIDs 0, 1, 2 … — exactly how Oracle BI Publisher generates it.
    3. Builds a single ToUnicode CMap block for the new sequential GIDs.
    4. Re-encodes every content stream to use the new GIDs.
    5. Replaces the /W width array and /BaseFont//FontName in the PDF.

    Returns the patched PDF with an indistinguishable-from-genuine font structure.
    """
    tahoma_path = tahoma_path or _TAHOMA_PATH
    if not tahoma_path:
        logger.warning("_rebuild_font_and_remap: system Tahoma not found — skipping rebuild")
        return pdf_bytes

    try:
        from fontTools import ttLib as _ft
    except ImportError:
        logger.warning("_rebuild_font_and_remap: fontTools not available — skipping rebuild")
        return pdf_bytes

    # ── 1. Read the current (extended) CMap ──────────────────────────────────
    current_cmap = extract_cmap_from_pdf(pdf_bytes)   # char → old_gid
    if not current_cmap:
        return pdf_bytes
    old_gid_to_char: dict = {v: k for k, v in current_cmap.items()}

    # ── 2. Decode content streams → collect used chars IN DOCUMENT ORDER ──────
    # Forensic finding: Oracle BI Publisher builds the font subset while walking
    # the text, so GID N is simply the N-th distinct character encountered in the
    # content stream.  In a genuine receipt the first-appearance sequence is
    # therefore exactly 1, 2, 3 … N.  Alphabetically ordered glyphs (what
    # fontTools produces) scramble that sequence — an invariant no genuine OBI
    # PDF can violate, and the most likely trigger of "Подмена текста".
    # We record first-appearance order here and assign GIDs accordingly.
    ordered_chars: list = []
    all_needed: set = set()
    for _, _, _, _, raw in _iter_streams(pdf_bytes):
        d = _decompress_stream(raw)
        if d is None or _is_font_stream(d):
            continue
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", d):
            hs = m.group(1).decode()
            for i in range(0, len(hs), 4):
                old_gid = int(hs[i:i + 4], 16)
                ch = old_gid_to_char.get(old_gid)
                if ch and ch not in all_needed:
                    all_needed.add(ch)
                    ordered_chars.append(ch)

    if not all_needed:
        logger.warning("_rebuild_font_and_remap: no content text decoded — skipping rebuild")
        return pdf_bytes

    # ── 3. Assemble the font from authentic Oracle (Tahoma 3.14) glyphs ───────
    # A genuine receipt donates the structural base: its cvt /fpgm/prep hinting
    # tables and head/hhea are Tahoma 3.14 and are kept byte-for-byte, which the
    # system font (5.01 / 6.98) can never reproduce.  Glyph outlines come from
    # the harvested bank — also authentic, including their hinting bytecode.
    # Characters absent from the bank fall back to the system Tahoma, decomposed
    # to simple contours with hinting stripped (5.01 bytecode would reference
    # CVT slots that mean something different in the 3.14 base).
    import copy as _copy
    from fontTools.ttLib.tables.ttProgram import Program as _TTProgram

    bank, base_font_bytes = _harvest_glyph_bank()

    out_tt = None
    if bank and base_font_bytes:
        try:
            # recalcBBoxes=False keeps the donor's hhea/maxp/head values instead
            # of letting fontTools recompute them for the subset: Oracle copies
            # advanceWidthMax, maxPoints, bounding boxes etc. verbatim from the
            # full Tahoma.  recalcTimestamp=False keeps head.modified as-is —
            # otherwise the save would stamp the current time into the font.
            out_tt = _ft.TTFont(
                io.BytesIO(base_font_bytes),
                recalcBBoxes=False,
                recalcTimestamp=False,
            )
            for _n in out_tt.getGlyphOrder():
                _ = out_tt["glyf"][_n]        # force decompile before rewriting
            _ = out_tt["hmtx"].metrics
        except Exception as exc:
            logger.warning("_rebuild_font_and_remap: genuine base unusable: %s", exc)
            out_tt = None

    if out_tt is None:
        logger.warning("_rebuild_font_and_remap: no genuine font base available")
        return pdf_bytes

    out_upm = out_tt["head"].unitsPerEm

    # Fallback source for characters the bank doesn't cover
    fb_tt = fb_glyf = fb_hmtx = fb_cmap = None
    try:
        fb_tt = _ft.TTFont(tahoma_path)
        fb_glyf, fb_hmtx = fb_tt["glyf"], fb_tt["hmtx"]
        fb_cmap = fb_tt.getBestCmap() or {}
        if fb_tt["head"].unitsPerEm != out_upm:
            fb_tt = None                       # scaling would distort outlines
    except Exception:
        fb_tt = None

    new_glyphs: dict = {".notdef": _copy.deepcopy(out_tt["glyf"][".notdef"])}
    new_metrics: dict = {".notdef": out_tt["hmtx"][".notdef"]}
    content_glyphs: list = []
    component_glyphs: list = []
    new_char_to_gid: dict = {}
    missing: set = set()
    from_bank = 0

    def _add_private_component(glyph, width) -> str:
        """Register a component as its own private, unmapped glyph.

        Genuine OBI fonts never share a component between composites and never
        point a composite at a CMap-mapped glyph — each dependency gets a
        private duplicate.  Keeping that shape matches the real glyph count.
        """
        name = f"_comp{len(component_glyphs)}"
        new_glyphs[name] = glyph
        new_metrics[name] = (width, glyph.xMin if glyph.numberOfContours else 0)
        component_glyphs.append(name)
        return name

    for ch in ordered_chars:
        gname = f"_g{len(content_glyphs)}"
        entry = bank.get(ch)
        if entry is not None:
            glyph = _copy.deepcopy(entry["glyph"])
            width = entry["width"]
            if glyph.isComposite():
                for comp, (cg, cw) in zip(glyph.components, entry["components"]):
                    comp.glyphName = _add_private_component(_copy.deepcopy(cg), cw)
            from_bank += 1
        else:
            fb_name = fb_cmap.get(ord(ch)) if fb_tt is not None else None
            if fb_name is None:
                missing.add(ch)
                continue
            glyph, width = _expand_to_simple(fb_name, fb_glyf, fb_hmtx)
            glyph.program = _TTProgram()       # 3.14 base — drop 5.01 hinting
        new_glyphs[gname] = glyph
        new_metrics[gname] = (width, glyph.xMin if glyph.numberOfContours else 0)
        content_glyphs.append(gname)
        new_char_to_gid[ch] = len(content_glyphs)   # GID = position, .notdef = 0

    if missing:
        logger.warning("_rebuild_font_and_remap: chars unavailable in any source: %r",
                       "".join(sorted(missing)))

    # ── 4. Commit the glyph set: .notdef, content in text order, components ───
    glyph_order_new = [".notdef"] + content_glyphs + component_glyphs
    out_tt.setGlyphOrder(glyph_order_new)
    _glyf = out_tt["glyf"]
    _glyf.glyphs = {n: new_glyphs[n] for n in glyph_order_new}
    _glyf.glyphOrder = glyph_order_new
    out_tt["hmtx"].metrics = {n: new_metrics[n] for n in glyph_order_new}
    out_tt["maxp"].numGlyphs = len(glyph_order_new)

    logger.info(
        "_rebuild_font_and_remap: %d glyphs (%d authentic / %d fallback) + %d components",
        len(glyph_order_new), from_bank, len(content_glyphs) - from_bank,
        len(component_glyphs),
    )

    # Compatibility alias for the width/CMap steps below
    sys_tt, sys_upm = out_tt, out_upm

    # ── 5. Strip tables that don't exist in genuine OBI embedded fonts ────────
    # OBI fonts contain only: glyf head hhea hmtx loca maxp cvt  fpgm prep
    # Any additional table (cmap, OS/2, name, post, GDEF, GPOS, GSUB, hdmx,
    # gasp, VDMX, …) is a forensic fingerprint that the font is NOT from OBI.
    _OBI_TABLES = {"cvt ", "fpgm", "glyf", "head", "hhea", "hmtx",
                   "loca", "maxp", "prep"}
    for _tag in list(sys_tt.keys()):
        if _tag not in _OBI_TABLES and _tag != "GlyphOrder":  # GlyphOrder is virtual
            try:
                del sys_tt[_tag]
            except Exception:
                pass

    # Random subset prefix lives only in the PDF /BaseFont + /FontName
    # dictionaries — NOT inside the font binary (OBI never puts it there).
    import random as _rnd, string as _str
    prefix = "".join(_rnd.choices(_str.ascii_uppercase, k=6))
    sname  = f"{prefix}+Tahoma"

    # Oracle always emits the long (32-bit) loca format, while fontTools picks
    # the short one whenever the glyf table is small enough — a 160-byte
    # discrepancy and a structural giveaway.  Force long format while saving.
    new_font_bytes = save_font_like_donor(sys_tt, base_font_bytes)

    # ── 6. Build new single-block ToUnicode CMap stream ──────────────────────
    new_gid_to_char: dict = {v: k for k, v in new_char_to_gid.items()}
    # Always include GID 0 → '?' at the start (OBI convention)
    gid0_char = old_gid_to_char.get(0, "?")
    cmap_entries = {0: gid0_char}
    cmap_entries.update(new_gid_to_char)
    sorted_entries = sorted(cmap_entries.items())  # [(gid, char), ...]

    # Oracle terminates every CMap line with CRLF, not LF.
    elines = "\r\n".join(f"<{gid:04X}> <{ord(ch):04X}>" for gid, ch in sorted_entries)
    new_cmap_text = (
        "/CIDInit /ProcSet findresource begin\r\n"
        "12 dict begin begincmap /CIDSystemInfo\r\n"
        "<< /Registry (Oracle) /Ordering(UCS) /Supplement 0 >> def\r\n"
        "/CMapName /Oracle-Identity-UCS def\r\n"
        "1 begincodespacerange\r\n"
        "<0000> <FFFF>\r\n"
        "endcodespacerange\r\n"
        f"{len(sorted_entries)} beginbfchar\r\n"
        f"{elines}\r\n"
        "endbfchar\r\n"
        "endcmap\r\n"
        "CMapName currentdict /CMap defineresource pop\r\n"
        "end end\r\n"
    )
    new_cmap_raw = zlib.compress(new_cmap_text.encode("latin-1"), level=6)

    # ── 7. Build new /W array (widths for every glyph in the new font) ────────
    # Oracle truncates the 1000/upm conversion (473 for advance 970, not 474)
    # and writes one CID per entry, ten entries per CRLF-terminated line.
    _entries = []
    for _gid, gname in enumerate(glyph_order_new):
        adv = sys_tt["hmtx"][gname][0]
        _entries.append(f"{_gid} [{int(adv * 1000 / sys_upm)}]")
    _lines = [
        " ".join(_entries[i:i + 10]) for i in range(0, len(_entries), 10)
    ]
    new_w_payload = ("[ " + "\r\n ".join(_lines) + " ]").encode("latin-1")

    # ── 8. Pass 1 — replace font stream and CMap stream ──────────────────────
    total_delta = 0
    font_done   = False
    cmap_done   = False
    for ls, le, ds, de, raw in list(_iter_streams(pdf_bytes)):
        adj_ls = ls + total_delta
        adj_le = le + total_delta
        adj_ds = ds + total_delta
        adj_de = de + total_delta

        d = _decompress_stream(raw)

        if not font_done and d is not None and _is_font_stream(d) and len(d) > 1000:
            new_raw = zlib.compress(new_font_bytes, level=6)
            pdf_bytes = pdf_bytes[:adj_ds] + new_raw + pdf_bytes[adj_de:]
            pdf_bytes = _update_length(pdf_bytes, adj_ls, adj_le, len(new_raw))
            ld = len(str(len(new_raw)).encode()) - len(str(len(raw)).encode())
            total_delta += len(new_raw) - len(raw) + ld
            font_done = True
            logger.debug("_rebuild: replaced font stream (%d bytes compressed)", len(new_raw))
            continue

        if not cmap_done:
            dd = d if d is not None else raw
            if b"beginbfchar" in dd:
                pdf_bytes = pdf_bytes[:adj_ds] + new_cmap_raw + pdf_bytes[adj_de:]
                pdf_bytes = _update_length(pdf_bytes, adj_ls, adj_le, len(new_cmap_raw))
                ld = len(str(len(new_cmap_raw)).encode()) - len(str(len(raw)).encode())
                total_delta += len(new_cmap_raw) - len(raw) + ld
                cmap_done = True
                logger.debug("_rebuild: replaced CMap stream (%d entries)", len(sorted_entries))
                continue

    if total_delta:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, 0)
        pdf_bytes = _update_startxref(pdf_bytes)
        total_delta = 0

    # ── 9. Pass 2 — re-encode content streams with new GIDs ──────────────────
    # Capture the mapping snapshots as local defaults to avoid closure issues
    _o2c = old_gid_to_char
    _c2g = new_char_to_gid

    def _recode(m, _o2c=_o2c, _c2g=_c2g):
        hs  = m.group(1).decode()
        out = ""
        for i in range(0, len(hs), 4):
            og  = int(hs[i:i + 4], 16)
            ch  = _o2c.get(og)
            ng  = _c2g.get(ch, og) if ch else og
            out += f"{ng:04X}"
        return f"<{out}> Tj".encode()

    for ls, le, ds, de, raw in list(_iter_streams(pdf_bytes)):
        d = _decompress_stream(raw)
        if d is None or _is_font_stream(d):
            continue
        dd = d if d is not None else raw
        if b"beginbfchar" in dd:
            continue  # skip the already-replaced CMap stream

        new_d, n = re.subn(rb"<([0-9A-Fa-f]+)>\s*Tj", _recode, d)
        if n == 0:
            continue

        adj_ls = ls + total_delta
        adj_le = le + total_delta
        adj_ds = ds + total_delta
        adj_de = de + total_delta

        new_raw = zlib.compress(new_d, level=6)
        pdf_bytes = pdf_bytes[:adj_ds] + new_raw + pdf_bytes[adj_de:]
        pdf_bytes = _update_length(pdf_bytes, adj_ls, adj_le, len(new_raw))
        ld = len(str(len(new_raw)).encode()) - len(str(len(raw)).encode())
        total_delta += len(new_raw) - len(raw) + ld

    if total_delta:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, 0)
        pdf_bytes = _update_startxref(pdf_bytes)

    # ── 10. Replace the /W array ──────────────────────────────────────────────
    m_w = re.search(rb"/W\s*\[", pdf_bytes)
    if m_w:
        depth = 0
        w_end = -1
        for i, b in enumerate(pdf_bytes[m_w.end() - 1:], start=m_w.end() - 1):
            if b == ord(b"["):
                depth += 1
            elif b == ord(b"]"):
                depth -= 1
                if depth == 0:
                    w_end = i
                    break
        if w_end != -1:
            old_w = pdf_bytes[m_w.start(): w_end + 1]
            new_w = b"/W " + new_w_payload
            pdf_bytes = pdf_bytes[:m_w.start()] + new_w + pdf_bytes[w_end + 1:]
            dw = len(new_w) - len(old_w)
            if dw:
                pdf_bytes = _fix_xref(pdf_bytes, dw, m_w.start())
                pdf_bytes = _update_startxref(pdf_bytes)

    # ── 11. Update /BaseFont and /FontName ────────────────────────────────────
    fn_bytes = sname.encode()
    pdf_bytes = re.sub(rb"/BaseFont\s*/[A-Z]{6}\+Tahoma", b"/BaseFont /" + fn_bytes, pdf_bytes)
    pdf_bytes = re.sub(rb"/FontName\s*/[A-Z]{6}\+Tahoma",  b"/FontName /"  + fn_bytes, pdf_bytes)

    logger.info(
        "_rebuild_font_and_remap: %d chars, %d GIDs (0..%d), prefix %s",
        len(new_char_to_gid),
        len(glyph_order_new),
        max(new_gid_to_char, default=0),
        prefix,
    )
    return pdf_bytes


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def obi_patch_pdf(
    pdf_bytes: bytes,
    replacements: list[dict],
    clean_font: Optional[bytes] = None,
) -> bytes:
    """Apply text replacements to an Oracle BI Publisher PDF.

    Replacement dicts have keys: field, old_value, new_value.

    If `clean_font` is provided (pre-extracted from the original template),
    the font stream is restored to this clean copy after patching (patcher
    can corrupt the font stream through accidental byte matches).

    Automatically detects characters in new_values that are missing from the
    embedded font CMap and extends the font with those glyphs from system Tahoma.

    Returns the patched PDF bytes with correct /Length, xref, startxref,
    /ID, and /ModDate.
    """
    if not replacements:
        return pdf_bytes

    cmap = extract_cmap_from_pdf(pdf_bytes)

    # --- Detect and handle missing characters ---
    all_new_chars: set[str] = set()
    for r in replacements:
        for ch in r.get("new_value", ""):
            all_new_chars.add(ch)

    missing_chars = {ch for ch in all_new_chars if ch not in cmap and ord(ch) > 0x001F}

    if missing_chars:
        logger.info(
            "obi_patch_pdf: missing chars %r — extending font",
            "".join(sorted(missing_chars, key=ord)),
        )
        base_font = clean_font  # extend the CLEAN font, not a potentially-corrupted one
        if base_font is None:
            # Try extracting from the PDF itself
            for _ls, _le, ds, de, raw in _iter_streams(pdf_bytes):
                d = _decompress_stream(raw)
                if d is not None and _is_font_stream(d) and len(d) > 1000:
                    base_font = d
                    break

        if base_font is not None:
            extended_font, new_char_to_gid = extend_font_for_chars(base_font, missing_chars)
            if new_char_to_gid:
                # Update clean_font to be the extended version
                clean_font = extended_font
                # Merge new GID mappings into CMap
                cmap.update(new_char_to_gid)
                # Update the ToUnicode CMap stream in the PDF
                pdf_bytes = _update_cmap_stream_in_pdf(pdf_bytes, new_char_to_gid)
                # Update /W array so Acrobat uses the correct advance widths
                # (otherwise new GIDs fall back to /DW=1000 — visible gap)
                try:
                    from fontTools import ttLib as _ft
                    _obi = _ft.TTFont(__import__("io").BytesIO(extended_font))
                    _order = _obi.getGlyphOrder()
                    new_gid_to_advw = {
                        gid: _obi["hmtx"][_order[gid]][0]
                        for gid in new_char_to_gid.values()
                        if gid < len(_order)
                    }
                    upm = _obi["head"].unitsPerEm
                    pdf_bytes = _update_glyph_widths_in_pdf(pdf_bytes, new_gid_to_advw, upm)
                except Exception as _exc:
                    logger.warning("obi_patch_pdf: /W update failed: %s", _exc)
                logger.info(
                    "obi_patch_pdf: extended font with %d new char(s): %s",
                    len(new_char_to_gid),
                    "".join(new_char_to_gid.keys()),
                )
    else:
        # Even when no chars are missing, randomize the subset prefix to prevent
        # font-hash fingerprint matching across different generated PDFs.
        import random as _rnd2, string as _str2
        _LAST_SUBSET_PREFIX[0] = "".join(_rnd2.choices(_str2.ascii_uppercase, k=6))
    if not cmap:
        logger.warning(
            "obi_patch_pdf: CMap not found — text replacement may be incomplete"
        )

    total_delta = 0
    first_change_offset: Optional[int] = None
    font_stream_info: Optional[dict] = None  # track for restoration

    streams_raw = list(_iter_streams(pdf_bytes))

    for len_start, len_end, data_start, data_end, raw in streams_raw:
        decompressed = _decompress_stream(raw)
        if decompressed is None:
            # Not a compressed stream — skip (also skip raw font data)
            continue

        # Protect font streams from modification
        if _is_font_stream(decompressed):
            if clean_font is not None:
                # Record for later restoration
                font_stream_info = {
                    "len_start": len_start,
                    "len_end": len_end,
                    "data_start": data_start,
                    "data_end": data_end,
                    "old_len": len(raw),
                }
                logger.debug("Recorded font stream at %d for clean restoration", data_start)
            continue

        modified = decompressed
        any_replaced = False

        for repl in replacements:
            old_val = repl.get("old_value", "")
            new_val = repl.get("new_value", "")
            if not old_val:
                continue

            old_hex = _encode_as_hex_gids(old_val, cmap)
            new_hex = _encode_as_hex_gids(new_val, cmap)

            modified, cnt = _replace_in_content(modified, old_hex, new_hex)
            if cnt > 0:
                any_replaced = True
                logger.info(
                    "Replaced %r -> %r (field: %s)",
                    old_val, new_val, repl.get("field", "?"),
                )

        if not any_replaced:
            continue

        new_raw = zlib.compress(modified, level=6)
        old_len = len(raw)
        new_len = len(new_raw)
        delta = new_len - old_len

        # Adjust for running offset delta
        adj_len_start = len_start + total_delta
        adj_len_end = len_end + total_delta
        adj_data_start = data_start + total_delta
        adj_data_end = data_end + total_delta

        pdf_bytes = (
            pdf_bytes[:adj_data_start]
            + new_raw
            + pdf_bytes[adj_data_end:]
        )

        old_len_bytes = str(old_len).encode()
        new_len_bytes = str(new_len).encode()
        pdf_bytes = _update_length(pdf_bytes, adj_len_start, adj_len_end, new_len)
        len_delta = len(new_len_bytes) - len(old_len_bytes)
        total_delta += delta + len_delta

        if first_change_offset is None:
            first_change_offset = data_start

    # --- Restore clean font if provided and font stream was found ---
    if clean_font is not None and font_stream_info is not None:
        new_font_raw = zlib.compress(clean_font, level=6)
        fi = font_stream_info
        adj_len_start = fi["len_start"] + total_delta
        adj_len_end = fi["len_end"] + total_delta
        adj_data_start = fi["data_start"] + total_delta
        adj_data_end = fi["data_end"] + total_delta
        old_font_raw = pdf_bytes[adj_data_start:adj_data_end]
        old_len = len(old_font_raw)
        new_len = len(new_font_raw)
        delta = new_len - old_len

        pdf_bytes = (
            pdf_bytes[:adj_data_start]
            + new_font_raw
            + pdf_bytes[adj_data_end:]
        )
        old_len_bytes = str(old_len).encode()
        new_len_bytes = str(new_len).encode()
        pdf_bytes = _update_length(pdf_bytes, adj_len_start, adj_len_end, new_len)
        len_delta = len(new_len_bytes) - len(old_len_bytes)
        total_delta += delta + len_delta
        if first_change_offset is None:
            first_change_offset = fi["data_start"]
        logger.info("Restored clean font stream (%d bytes)", len(clean_font))

    if total_delta != 0 and first_change_offset is not None:
        pdf_bytes = _fix_xref(pdf_bytes, total_delta, first_change_offset)
        pdf_bytes = _update_startxref(pdf_bytes)

    # --- Full font rebuild: sequential GIDs, no gaps, single CMap block ------
    # Replaces the old orphan-removal + /BaseFont patching steps.
    # _rebuild_font_and_remap creates a fresh Tahoma subset for exactly the
    # characters used in the final document, assigns sequential GIDs 0,1,2,...
    # (identical to what Oracle BI Publisher generates natively), re-encodes
    # all content streams, and updates /W, /BaseFont, /FontName.
    pdf_bytes = _rebuild_font_and_remap(pdf_bytes)

    # Extract receipt datetime from replacements.
    # Priority: "formed_date" style (HH:MM, no seconds) so that the PDF
    # metadata /CreationDate exactly matches what the checker reads from the
    # "Сформирована" field (checker treats HH:MM as HH:MM:00).
    import re as _re
    receipt_dt_str: Optional[str] = None
    for r in replacements:
        nv = r.get("new_value", "")
        # Prefer HH:MM (no seconds) — matches "Сформирована" time
        m_dt2 = _re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})(?!\s*:\d)", nv.replace("\xa0", " "))
        if m_dt2:
            receipt_dt_str = f"{m_dt2.group(1)} {m_dt2.group(2)}"
            break  # Stop at first HH:MM match (formed_date field)
    if not receipt_dt_str:
        # Fallback: use HH:MM:SS but strip seconds to get :00
        for r in replacements:
            nv = r.get("new_value", "")
            m_dt = _re.search(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}):\d{2}", nv.replace("\xa0", " "))
            if m_dt:
                receipt_dt_str = f"{m_dt.group(1)} {m_dt.group(2)}"
                break

    pdf_bytes = _patch_docid_moddate(pdf_bytes, receipt_dt_str)

    # Last step: the xref table must describe the finished file, not the
    # intermediate states the patching passes left behind.
    pdf_bytes = _rebuild_xref_table(pdf_bytes)

    logger.info("obi_patch_pdf complete. delta=%d bytes", total_delta)
    return pdf_bytes
