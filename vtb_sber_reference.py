#!/usr/bin/env python3
"""Вертикальная стенка: последняя буква каждой строки касается одной линии.

WALL = max(x1) всех полей правой колонки.
PTS = (x1 - tm_x) / n — извлекается из исходного PDF для каждого поля (разный кернинг).
"""
from pathlib import Path
import re
import zlib

try:
    import fitz
except ImportError:
    fitz = None


def scan_vtb_unsupported_chars(pdf_path: str | Path) -> set[str]:
    """Если доступен PyMuPDF: извлекает текст правой колонки и возвращает неподдерживаемые символы."""
    if fitz is None:
        return set()
    from vtb_cmap import get_unsupported_chars
    path = Path(pdf_path)
    if not path.exists():
        return set()
    bad = set()
    try:
        doc = fitz.open(path)
        dt = doc[0].get_text("dict")
        doc.close()
        for block in dt.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bb = span.get("bbox")
                    if not bb or bb[0] < 100 or bb[0] > 270:
                        continue
                    text = span.get("text", "")
                    for c in get_unsupported_chars(text):
                        bad.add(c)
    except Exception:
        pass
    return bad

FALLBACK_WALL = 257.1
FALLBACK_PTS_FONT9 = 5.0925
FALLBACK_PTS_AMOUNT = 6.447  # font 13.5, kern -11.11111


def _count_tj_glyphs(tj_bytes: bytes) -> int:
    for kern in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333"):
        if kern in tj_bytes:
            return tj_bytes.count(kern) + 1
    return 1


def _scan_stream_blocks(data: bytes) -> list[dict]:
    """Извлечь (y, tm_x, n) из content stream."""
    blocks = []
    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = zlib.decompress(bytes(data[stream_start : stream_start + stream_len]))
        except zlib.error:
            continue
        if b"BT" not in dec or b"Tm" not in dec:
            continue
        pat = rb'1\s+0\s+0\s+1\s+([\d.]+)\s+([\d.]+)\s+Tm\s*\r?\n[^\[]*\[([^\]]*)\]\s*TJ'
        for gm in re.finditer(pat, dec):
            x, y = float(gm.group(1)), float(gm.group(2))
            if x < 50:
                continue
            n = _count_tj_glyphs(gm.group(3))
            blocks.append({"tm_x": x, "y": y, "n": n})
        break
    return blocks


def _spans_with_bbox(dt: dict) -> list[dict]:
    """Список span с (text, x1, y_center) для правой колонки."""
    out = []
    for block in dt.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bb = span.get("bbox")
                if not bb or bb[0] < 100 or bb[0] > 270 or bb[1] < 50 or bb[1] > 400:
                    continue
                text = span.get("text", "").strip()
                if not text:
                    continue
                y_center = (bb[1] + bb[3]) / 2
                out.append({"text": text, "x1": bb[2], "y": y_center})
    return out


def get_vtb_per_field_params(pdf_path: str | Path) -> dict:
    """Извлекает WALL и pts для каждого заменяемого поля из исходного PDF.

    pts = (x1 - tm_x) / n, где x1 из PyMuPDF bbox, tm_x,n из stream.
    Возвращает: wall, pts_date, pts_payer, pts_recipient, pts_amount
    """
    result = {
        "wall": FALLBACK_WALL,
        "pts_date": 4.55,
        "pts_payer": 4.66,
        "pts_recipient": 4.66,
        "pts_amount": FALLBACK_PTS_AMOUNT,
    }
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        data = path.read_bytes()
        stream_blocks = _scan_stream_blocks(data)
        doc = fitz.open(path)
        page = doc[0]
        rect = page.rect
        page_height = rect.y1 - rect.y0
        dt = page.get_text("dict")
        spans = _spans_with_bbox(dt)
        doc.close()

        # Fitz bbox: топ-левый origin, y вниз. PDF stream: низ-левый, y вверх.
        # pdf_y = page_height - fitz_y
        Y_TOL = 5.0

        def find_pts(search_words: list[str]) -> float | None:
            span = None
            for s in spans:
                if any(w in s["text"] for w in search_words):
                    span = s
                    break
            if not span:
                return None
            pdf_y = page_height - span["y"]
            best = None
            best_dy = 999
            for sb in stream_blocks:
                dy = abs(sb["y"] - pdf_y)
                if dy < best_dy and dy < Y_TOL:
                    best_dy = dy
                    best = sb
            if best and best["n"] > 0:
                return (span["x1"] - best["tm_x"]) / best["n"]
            return None

        # Собираем x1 для WALL
        x1_list = [s["x1"] for s in spans]
        result["wall"] = max(x1_list)

        if pts := find_pts(["04:47", "09.03", "03.202"]):
            result["pts_date"] = pts
        if pts := find_pts(["Александр", "Евгеньевич"]):
            result["pts_payer"] = pts
        if pts := find_pts(["Ефим", "Антонович"]):
            result["pts_recipient"] = pts
        if pts := find_pts(["1 000", "000 ₽", "₽"]):
            result["pts_amount"] = pts
    except Exception:
        pass
    return result


WALL_SOURCES = {
    "sber": "Сбербанк",
    "phone": "+7 (906)",  # Телефон — обычно самый правый
    "done": "Выполнено",
    "star": "*9426",
    "max": "max",  # максимум из всех
}


def get_vtb_alignment_params(
    pdf_path: str | Path, wall_source: str = "sber"
) -> dict:
    """WALL и PTS. wall_source: sber|phone|done|star|max."""
    result = {
        "wall": FALLBACK_WALL,
        "pts_font9": FALLBACK_PTS_FONT9,
        "pts_font13_5": FALLBACK_PTS_FONT9 * 13.5 / 9,
    }
    result["ref_right"] = result["wall"]
    result["pts_payer"] = result["pts_recipient"] = result["pts_font9"]
    result["pts_amount"] = result["pts_font13_5"]
    if fitz is None:
        return result
    path = Path(pdf_path)
    if not path.exists():
        return result
    try:
        doc = fitz.open(path)
        page = doc[0]
        dt = page.get_text("dict")
        pts9 = FALLBACK_PTS_FONT9
        sber = page.search_for("Сбербанк")
        if sber:
            r = sber[0]
            pts9 = (r.x1 - r.x0) / 8
            result["pts_font9"] = result["pts_payer"] = result["pts_recipient"] = pts9
            result["pts_font13_5"] = result["pts_amount"] = pts9 * 13.5 / 9

        # Собираем x1 всех полей правого столбца
        x1_list = []
        for b in dt.get("blocks", []):
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    bb = s.get("bbox")
                    if bb and 100 < bb[0] < 260 and 100 < bb[1] < 380:
                        x1_list.append(bb[2])

        wall = FALLBACK_WALL
        if wall_source == "sber" and sber:
            wall = float(sber[0].x1)
        elif wall_source == "phone":
            r = page.search_for("236-86-13")
            if not r:
                r = page.search_for("+7 ")
            if r:
                wall = float(r[0].x1)
            elif x1_list:
                wall = max(x1_list)
        elif wall_source == "done":
            r = page.search_for("Выполнено")
            if r:
                wall = float(r[0].x1)
        elif wall_source == "star":
            r = page.search_for("*9426")
            if r:
                wall = float(r[0].x1)
        elif wall_source == "max" and x1_list:
            wall = max(x1_list)

        result["wall"] = result["ref_right"] = wall
        doc.close()
    except Exception:
        pass
    return result


def get_sber_right_edge(pdf_path: str | Path) -> float:
    return get_vtb_alignment_params(pdf_path)["wall"]


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "Тест ВТБ/09-03-26_03-47_1.pdf"
    params = get_vtb_alignment_params(p)
    print(f"WALL = {params['wall']:.4f}")
    print(f"pts_font9 = {params['pts_font9']:.4f}")
    print(f"pts_font13_5 = {params['pts_font13_5']:.4f}")
