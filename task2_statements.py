#!/usr/bin/env python3
"""
Task 2: Two Alfa Bank statement variants from AM_1774134591446.pdf (verified-passing base).

  - Version A: formation date == operation date
  - Version B: formation date != operation date

Uses the same PDF structure as the passing original (not AM_1774109927283.pdf).
Amounts keep the same character widths as the original for stable layout.

Document /ID: только один hex-символ во второй строке (patch_document_id_one_nibble),
а не полная замена — иначе те же эвристики, что в CHECK_VERIFICATION_RULES.md для чеков.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Prefer project copy, then Downloads (same source as task3)
_BASE_CANDIDATES = [
    Path(__file__).parent / "AM_1774134591446.pdf",
    Path("/Users/aleksandrzerebatav/Downloads/AM_1774134591446.pdf"),
]
BASE_PDF = next((p for p in _BASE_CANDIDATES if p.exists()), _BASE_CANDIDATES[0])
OUTPUT_A = Path(__file__).parent / "test_statement_same_date.pdf"
OUTPUT_B = Path(__file__).parent / "test_statement_diff_date.pdf"

# Original AM_1774134591446.pdf
OLD_ACCOUNT = "40817810980480002476"
OLD_DATE = "25.02.2026"
OLD_PERIOD = "За период с 25.02.2026 по 25.02.2026"
OLD_OP_C = "C822502260006543"
OLD_OST = "OST1_5KSH0001I0M"  # 16 chars; второй subset — через multi-CMap в patch_replacements

COMMISSION_DESC = (
    "Комиссия за перевод по номеру телефона. Получатель 992920001499, \n"
    "Таджикистан. C822502260006543"
)
TRANSFER_DESC = "Перевод за рубеж по номеру телефона C822502260006543"


def _fmt_balance(val: float) -> str:
    """Like '1 852,90' — thousands + space + 3 digits + comma + cents (8 chars for 1_000–9_999.99)."""
    val = max(1000.0, min(9999.99, round(val, 2)))
    integer = int(val)
    frac = int(round((val - integer) * 100)) % 100
    return f"{integer // 1000} {integer % 1000:03d},{frac:02d}"


def _fmt_expense_summary(val: float) -> str:
    """Like '40,00' — 5 chars."""
    val = max(10.0, min(99.99, round(val, 2)))
    integer = int(val)
    frac = int(round((val - integer) * 100)) % 100
    return f"{integer},{frac:02d}"


def _fmt_neg_tx(val: float) -> str:
    """Like '-30,00' — 6 chars."""
    val = abs(val)
    val = max(10.0, min(99.99, round(val, 2)))
    integer = int(val)
    frac = int(round((val - integer) * 100)) % 100
    return f"-{integer:02d},{frac:02d}"


def make_ost_code() -> str:
    """Same length as OST1_5KSH0001I0M (16 chars). Суффикс только из символов оригинала — иначе нет глифов в том же ToUnicode, что и у эталонной строки."""
    suffix_charset = "".join(dict.fromkeys(OLD_OST[5:]))  # "5KSH0001I0M" → уникальные в порядке
    suffix = "".join(random.choices(suffix_charset, k=11))
    return f"OST1_{suffix}"


def make_op_code(date_str: str) -> str:
    """Same length as original C822502260006543 (16 chars) for stable stream patching."""
    parts = date_str.split(".")
    dd, mm, yy = parts[0].zfill(2), parts[1].zfill(2), parts[2][2:]
    suffix = "".join(str(random.randint(0, 9)) for _ in range(9))
    s = f"C{dd}{mm}{yy}{suffix}"
    if len(s) < 16:
        s += "".join(str(random.randint(0, 9)) for _ in range(16 - len(s)))
    return s[:16]


def make_valid_account(last4: str) -> str:
    from patch_account_last4 import build_valid_account, _find_bik

    bik = _find_bik(OLD_ACCOUNT)
    if not bik:
        print(f"  [WARN] BIK not found for {OLD_ACCOUNT}, using default")
        bik = "044525593"
    result = build_valid_account(OLD_ACCOUNT, last4, bik)
    if not result:
        raise ValueError(f"Cannot build valid account with last4={last4}")
    return result


def build_content_replacements(
    *,
    new_account: str,
    new_op_c: str,
    new_ost: str,
    incoming: str,
    expenses: str,
    outgoing: str,
    tx1: str,
    tx2: str,
) -> list[tuple[str, str]]:
    """Longest patterns first for patch_replacements."""
    new_comm = (
        "Комиссия за перевод по номеру телефона. Получатель 992920001499, \n"
        f"Таджикистан. {new_op_c}"
    )
    new_transfer = f"Перевод за рубеж по номеру телефона {new_op_c}"
    return [
        (COMMISSION_DESC, new_comm),
        (TRANSFER_DESC, new_transfer),
        (OLD_ACCOUNT, new_account),
        ("1 852,90 RUR", f"{incoming} RUR"),
        ("1 812,90 RUR", f"{outgoing} RUR"),
        ("40,00 RUR", f"{expenses} RUR"),
        ("-30,00 RUR", f"{tx1} RUR"),
        ("-10,00 RUR", f"{tx2} RUR"),
        (OLD_OST, new_ost),
        (OLD_OP_C, new_op_c),
    ]


def apply_patches(
    base_pdf: Path,
    output_pdf: Path,
    *,
    incoming: str,
    expenses: str,
    outgoing: str,
    tx1: str,
    tx2: str,
    op_date: str,
    form_date: str,
    new_account: str,
    new_op_c: str,
    new_ost: str,
    id_flip_pos_from_end: int = 0,
) -> None:
    from cid_patch_amount import patch_replacements
    from patch_id import patch_document_id_one_nibble, patch_moddate

    content_reps = build_content_replacements(
        new_account=new_account,
        new_op_c=new_op_c,
        new_ost=new_ost,
        incoming=incoming,
        expenses=expenses,
        outgoing=outgoing,
        tx1=tx1,
        tx2=tx2,
    )

    print(f"  Step 1: content replacements ({len(content_reps)} patterns)...")
    ok1 = patch_replacements(base_pdf, output_pdf, content_reps)
    if not ok1:
        print("  [ERROR] content patch_replacements failed", file=sys.stderr)
        sys.exit(1)

    date_reps: list[tuple[str, str]] = []
    if op_date == form_date:
        date_reps.append((OLD_PERIOD, f"За период с {op_date} по {op_date}"))
        date_reps.append((OLD_DATE, op_date))
    else:
        date_reps.append((OLD_PERIOD, f"За период с {op_date} по {form_date}"))
        date_reps.append((OLD_DATE, op_date))
        date_reps.append((f"выписки\n{op_date}", f"выписки\n{form_date}"))

    print(f"  Step 2: date replacements (op={op_date}, form={form_date})...")
    ok2 = patch_replacements(output_pdf, output_pdf, date_reps)
    if not ok2:
        print("  [WARN] Some date replacements may not have applied")

    print(
        f"  Step 3: document ID (один символ во 2-й строке, pos_from_end={id_flip_pos_from_end})..."
    )
    if not patch_document_id_one_nibble(
        output_pdf, which=2, pos_from_end=id_flip_pos_from_end
    ):
        print("  [WARN] /ID minimal patch failed", file=sys.stderr)

    print(f"  Step 4: ModDate -> {form_date}...")
    if not patch_moddate(output_pdf, form_date):
        print("  [WARN] /ModDate not found", file=sys.stderr)

    print(f"  Written: {output_pdf}")


def _print_id_vs_etalon(etalon_pdf: Path, verified: dict) -> None:
    """Проверка: ID[0] как у эталона, во ID[1] изменён ровно один ниббл."""
    import re

    raw = etalon_pdf.read_bytes()
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw)
    if not m or not verified.get("doc_id") or not verified.get("doc_id2"):
        print("  ID vs эталон: (не сравнить)")
        return
    e1, e2 = m.group(1).decode().lower(), m.group(2).decode().lower()
    g1, g2 = verified["doc_id"].lower(), verified["doc_id2"].lower()
    same0 = e1 == g1
    diff2 = sum(1 for a, b in zip(e2, g2) if a != b) + abs(len(e2) - len(g2))
    print(f"  ID[0] как у эталона: {same0}  отличий в ID[1]: {diff2} символ(ов)")


def verify_output(pdf_path: Path) -> dict:
    import re

    import fitz

    doc = fitz.open(str(pdf_path))
    text = doc[0].get_text()
    meta = doc.metadata
    doc.close()

    raw = pdf_path.read_bytes()
    id_m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", raw)
    doc_id1 = id_m.group(1).decode() if id_m else None
    doc_id2 = id_m.group(2).decode() if id_m else None
    md_space = re.search(rb"/ModDate(\s*)\(", raw)
    space_after = md_space.group(1) if md_space else None

    return {
        "text": text,
        "doc_id": doc_id1,
        "doc_id2": doc_id2,
        "ids_different": doc_id1 != doc_id2,
        "modDate": meta.get("modDate", ""),
        "moddate_no_space": space_after == b"",
        "size": pdf_path.stat().st_size,
    }


def main() -> None:
    print("=== Task 2: Statement variants (base = passing AM_1774134591446.pdf) ===\n")

    if not BASE_PDF.exists():
        print(f"[ERROR] Base PDF not found. Tried:\n  " + "\n  ".join(str(p) for p in _BASE_CANDIDATES), file=sys.stderr)
        sys.exit(1)
    print(f"Base: {BASE_PDF}\n")

    random.seed(42)

    def pick_amounts() -> tuple[str, str, str, str, str]:
        """incoming, expenses_summary, outgoing, tx1, tx2 — consistent math."""
        a = round(random.uniform(15.0, 45.0), 2)
        b = round(random.uniform(15.0, 45.0), 2)
        expenses_f = round(a + b, 2)
        outgoing_f = round(random.uniform(1200.0, 8500.0), 2)
        incoming_f = round(outgoing_f + expenses_f, 2)
        if incoming_f > 9999.99:
            outgoing_f = round(9999.99 - expenses_f, 2)
            incoming_f = round(outgoing_f + expenses_f, 2)
        return (
            _fmt_balance(incoming_f),
            _fmt_expense_summary(expenses_f),
            _fmt_balance(outgoing_f),
            _fmt_neg_tx(a),
            _fmt_neg_tx(b),
        )

    inc_a, exp_a, out_a, tx1_a, tx2_a = pick_amounts()
    random.seed(99)
    inc_b, exp_b, out_b, tx1_b, tx2_b = pick_amounts()

    op_date_a = "10.03.2026"
    form_date_a = "10.03.2026"
    op_date_b = "05.02.2026"
    form_date_b = "20.03.2026"

    print("Generating valid account numbers...")
    last4_a, last4_b = "3719", "4826"
    account_a = make_valid_account(last4_a)
    account_b = make_valid_account(last4_b)
    print(f"  Version A account: {account_a}")
    print(f"  Version B account: {account_b}")

    op_c_a = make_op_code(op_date_a)
    op_c_b = make_op_code(op_date_b)
    ost_a = make_ost_code()
    ost_b = make_ost_code()
    print(f"  Version A C-code: {op_c_a}  OST: {ost_a}")
    print(f"  Version B C-code: {op_c_b}  OST: {ost_b}")

    # ── Version A ─────────────────────────────────────────────
    print(f"\n--- Version A (form = op = {op_date_a}) ---")
    print(f"  Amounts: in={inc_a}, exp={exp_a}, out={out_a}, tx={tx1_a}, {tx2_a}")
    apply_patches(
        BASE_PDF,
        OUTPUT_A,
        incoming=inc_a,
        expenses=exp_a,
        outgoing=out_a,
        tx1=tx1_a,
        tx2=tx2_a,
        op_date=op_date_a,
        form_date=form_date_a,
        new_account=account_a,
        new_op_c=op_c_a,
        new_ost=ost_a,
        id_flip_pos_from_end=0,
    )
    ra = verify_output(OUTPUT_A)
    print("\nVersion A verification:")
    _print_id_vs_etalon(BASE_PDF, ra)
    print(f"  Size: {ra['size']:,}  ID[0]!=ID[1]: {ra['ids_different']}  ModDate: {ra['modDate']}")
    print(f"  /ModDate( no space in raw: {ra['moddate_no_space']}")
    print(f"  Account: {account_a in ra['text']}  Period end in text OK: {form_date_a in ra['text']}")
    print(f"  OST code in text: {ost_a in ra['text']}")

    # ── Version B ─────────────────────────────────────────────
    print(f"\n--- Version B (form={form_date_b}, op={op_date_b}) ---")
    print(f"  Amounts: in={inc_b}, exp={exp_b}, out={out_b}, tx={tx1_b}, {tx2_b}")
    apply_patches(
        BASE_PDF,
        OUTPUT_B,
        incoming=inc_b,
        expenses=exp_b,
        outgoing=out_b,
        tx1=tx1_b,
        tx2=tx2_b,
        op_date=op_date_b,
        form_date=form_date_b,
        new_account=account_b,
        new_op_c=op_c_b,
        new_ost=ost_b,
        id_flip_pos_from_end=1,
    )
    rb = verify_output(OUTPUT_B)
    print("\nVersion B verification:")
    _print_id_vs_etalon(BASE_PDF, rb)
    print(f"  Size: {rb['size']:,}  ID[0]!=ID[1]: {rb['ids_different']}  ModDate: {rb['modDate']}")
    print(f"  /ModDate( no space in raw: {rb['moddate_no_space']}")
    print(f"  Account: {account_b in rb['text']}  form_date: {form_date_b in rb['text']}  op_date: {op_date_b in rb['text']}")
    print(f"  OST code in text: {ost_b in rb['text']}")

    print("\n=== Summary ===")
    print(f"A: {OUTPUT_A.name}  account={account_a}")
    print(
        f"B: {OUTPUT_B.name}  account={account_b}  ID[1] A/B разные: {ra['doc_id2'] != rb['doc_id2']}"
    )


if __name__ == "__main__":
    main()
