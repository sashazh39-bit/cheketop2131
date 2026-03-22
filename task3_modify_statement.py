#!/usr/bin/env python3
"""
Task 3: Modify AM_1774134591446.pdf with custom values, keeping original document ID.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SOURCE = Path("/Users/aleksandrzerebatav/Downloads/AM_1774134591446.pdf")
OUTPUT = Path(__file__).parent / "test_statement_no_id_change.pdf"


def main():
    print("=== Task 3: Modify statement (keep document ID) ===\n")

    if not SOURCE.exists():
        print(f"[ERROR] Source not found: {SOURCE}", file=sys.stderr)
        sys.exit(1)

    from cid_patch_amount import patch_replacements

    # Read original document ID
    raw = SOURCE.read_bytes()
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', raw)
    if id_m:
        orig_id1 = id_m.group(1).decode()
        orig_id2 = id_m.group(2).decode()
        print(f"Original Document ID: {orig_id1} / {orig_id2}")

    # Changes to apply
    # Current values from AM_1774134591446.pdf:
    #   Account: 40817810980480002476
    #   Formation date: 25.02.2026
    #   Входящий остаток: 1 852,90 RUR
    #   Поступления: 0,00 RUR
    #   Расходы: 40,00 RUR
    #   Исходящий остаток: 1 812,90 RUR
    #   Платежный лимит: 1 812,90 RUR
    #   Текущий баланс: 1 812,90 RUR
    #   Op1: OST1_5KSH0001I0M, -30,00 RUR
    #   Op2: C822502260006543, -10,00 RUR

    # New values (custom)
    replacements = [
        # Amounts (longer patterns first to avoid partial overlap)
        ("1 852,90 RUR",  "9 478,45 RUR"),   # Входящий остаток
        ("1 812,90 RUR",  "8 978,45 RUR"),   # Исходящий, Платежный, Текущий (same value)
        ("-30,00 RUR",    "-200,00 RUR"),     # Op1 amount
        ("-10,00 RUR",    "-300,00 RUR"),     # Op2 amount
        ("40,00 RUR",     "500,00 RUR"),      # Расходы total
        # Op2 code appears in description too, update description
        ("C822502260006543",
         "C822502260099999"),
        # Op2 description
        ("Перевод за рубеж по номеру телефона C822502260006543",
         "Перевод за рубеж по номеру телефона C822502260099999"),
        # Op1 description (update commission text)
        ("Комиссия за перевод по номеру телефона. Получатель 992920001499, \nТаджикистан. C822502260006543",
         "Комиссия за перевод по номеру телефона. Получатель 992920001499, \nТаджикистан. C822502260099999"),
    ]

    print("Applying replacements:")
    for old, new in replacements:
        print(f"  {old[:60]!r} -> {new[:60]!r}")

    ok = patch_replacements(SOURCE, OUTPUT, replacements)
    if not ok:
        print("[WARN] patch_replacements returned False (some may not have applied)")

    # Verify document ID unchanged
    raw_out = OUTPUT.read_bytes()
    id_m_out = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', raw_out)
    if id_m_out:
        new_id1 = id_m_out.group(1).decode()
        new_id2 = id_m_out.group(2).decode()
        print(f"\nDocument ID after: {new_id1} / {new_id2}")
        if new_id1 == orig_id1 and new_id2 == orig_id2:
            print("[OK] Document ID UNCHANGED")
        else:
            print("[ERROR] Document ID changed!")

    # Verify text
    import fitz
    doc = fitz.open(str(OUTPUT))
    text = doc[0].get_text()
    doc.close()
    print(f"\nFile size: {OUTPUT.stat().st_size:,} bytes (original: {SOURCE.stat().st_size:,} bytes)")
    print(f"\nExtracted text:\n{text}")


if __name__ == "__main__":
    main()
