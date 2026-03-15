#!/usr/bin/env python3
"""Генерация чека по Варианту B: compact_base + патч значений.

Сначала: python3 transform_to_compact.py "база_чеков/vtb/СБП/check (3).pdf" compact_base.pdf
Затем: python3 gen_receipt.py output.pdf --payer "..." --recipient "..." --amount 5000
"""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vtb_patch_from_config import patch_from_values
from vtb_test_generator import update_creation_date


def main() -> int:
    base = Path(__file__).parent / "compact_base.pdf"
    if not base.exists():
        print("[ERROR] Сначала запустите: python3 transform_to_compact.py 'база_чеков/vtb/СБП/check (3).pdf' compact_base.pdf")
        return 1

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("output", default="receipt_variant_b.pdf", nargs="?")
    ap.add_argument("--payer", "-p", default="Максим Андреевич Б.")
    ap.add_argument("--recipient", "-r", default="Александр Евгеньевич Ж.")
    ap.add_argument("--phone", default="+7 (992) 494-94-95")
    ap.add_argument("--amount", "-a", type=int, default=8700)
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    out_path = Path(args.output).resolve()
    if not out_path.suffix:
        out_path = out_path.with_suffix(".pdf")

    date_str = datetime.now().strftime("%d.%m.%Y, %H:%M") if not args.date or args.date == "now" else args.date
    meta_date = datetime.strptime(date_str, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")

    data = bytearray(base.read_bytes())
    out = patch_from_values(
        data,
        base,
        date_str=date_str,
        payer=args.payer,
        recipient=args.recipient,
        phone=args.phone,
        amount=args.amount,
        keep_metadata=True,
    )
    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)

    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', out_arr)
    if id_m:
        hex1 = id_m.group(1).decode()
        c = hex1[-1]
        idx = "0123456789ABCDEF".find(c.upper())
        new_c = "0123456789ABCDEF"[(idx + 1) % 16]
        new1 = hex1[:-1] + new_c
        out_arr[id_m.start(1):id_m.end(1)] = new1.encode()
        out_arr[id_m.start(2):id_m.end(2)] = new1.encode()

    out_path.write_bytes(out_arr)
    print(f"✅ Вариант B: {out_path} ({len(out_arr)/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
