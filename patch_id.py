#!/usr/bin/env python3
"""
Замена Document ID в PDF на случайный (32 hex символа).
Не меняет структуру файла — только hex-значения внутри /ID (длина та же).
"""
import argparse
import re
import secrets
import sys
from pathlib import Path


def patch_document_id(pdf_path: Path, new_id_hex: str | None = None) -> bool:
    """
    Заменить /ID [ <hex1> <hex2> ] на новый ID той же длины.
    new_id_hex: 32 символа 0-9a-f, или None для генерации случайного.
    """
    data = pdf_path.read_bytes()
    m = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]", data)
    if not m:
        return False

    old1, old2 = m.group(1), m.group(2)
    target_len = len(old1)
    if len(old2) != target_len:
        return False

    if new_id_hex is None:
        new_id_hex = secrets.token_hex(target_len // 2)
    new_id_hex = new_id_hex.lower()
    if len(new_id_hex) != target_len:
        raise ValueError(f"ID должен быть ровно {target_len} hex-символов, получено {len(new_id_hex)}")
    if not re.match(r"^[0-9a-f]+$", new_id_hex):
        raise ValueError("ID должен содержать только 0-9 и a-f")

    new_b = new_id_hex.encode("ascii")
    data = data.replace(b"<" + old1 + b">", b"<" + new_b + b">", 1)
    data = data.replace(b"<" + old2 + b">", b"<" + new_b + b">", 1)
    pdf_path.write_bytes(data)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Заменить Document ID в PDF на случайный (32 hex).")
    parser.add_argument("pdf", nargs="?", default="output.pdf", help="Путь к PDF (по умолчанию output.pdf)")
    parser.add_argument("--id", dest="custom_id", default=None, help="Свой 32-символьный hex (иначе генерируется случайный)")
    args = parser.parse_args()

    path = Path(args.pdf).expanduser().resolve()
    if not path.exists():
        print(f"[ERROR] Файл не найден: {path}", file=sys.stderr)
        return 1

    try:
        ok = patch_document_id(path, args.custom_id)
        if not ok:
            print("[ERROR] /ID не найден в PDF.", file=sys.stderr)
            return 1
        new_hex = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", path.read_bytes())
        if new_hex:
            print(f"[OK] Document ID заменён на: {new_hex.group(1).decode()}")
        return 0
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
