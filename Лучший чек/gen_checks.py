#!/usr/bin/env python3
"""Генерация чеков «Лучший чек»: check 1, check 2, ... 

Цель: заглавные буквы (Ф, Ч, Ю) визуально и при копировании, размер ~эталон, структура приближена к эталону.

Использование:
  python3 gen_checks.py                    # генерирует check 1, 2, 3
  python3 gen_checks.py --only 1           # только check 1
  python3 gen_checks.py --etalon /path/to/13.pdf
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
OUT_DIR = Path(__file__).parent
ETALON_PATHS = [
    Path.home() / "Downloads" / "13-03-26_00-00 13.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 13.pdf",
]
ETALON_17_PATHS = [
    Path.home() / "Downloads" / "13-03-26_00-00 17.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 17.pdf",
]
ETALON_16_PATHS = [
    Path.home() / "Downloads" / "13-03-26_00-00 16.pdf",
    BASE / "база_чеков" / "vtb" / "СБП" / "13-03-26_00-00 16.pdf",
]
DONOR = BASE / "база_чеков" / "vtb" / "СБП" / "check (3).pdf"


def _find_etalon(etalon_arg: Path | None) -> Path | None:
    for p in ([etalon_arg] if etalon_arg else []) + list(ETALON_PATHS):
        if p and p.exists():
            return p.resolve()
    return None


def _find_17() -> Path | None:
    for p in ETALON_17_PATHS:
        if p.exists():
            return p.resolve()
    return None


def _find_16() -> Path | None:
    for p in ETALON_16_PATHS:
        if p.exists():
            return p.resolve()
    return None


def _run(cmd: list[str], cwd: Path | None = None) -> bool:
    r = subprocess.run(cmd, cwd=cwd or BASE, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"[ERROR] {' '.join(cmd)}\n{r.stderr or r.stdout}", file=sys.stderr)
        return False
    return True


def gen_check_1(etalon: Path) -> bool:
    """add_glyphs --replace: перезапись 3 CID без добавления — структура ~эталон."""
    out = OUT_DIR / "check 1.pdf"
    cmd = [
        sys.executable,
        str(BASE / "add_glyphs_to_13_03.py"),
        "--replace",
        "--target", str(etalon),
        "-o", str(out),
    ]
    if not _run(cmd):
        return False
    print(f"  check 1: {out.name} ({out.stat().st_size} bytes)")
    return True


def gen_check_2(etalon: Path) -> bool:
    """patch_13_03_from_check3: font+ToUnicode+/W из check(3), перекодирование content."""
    out = OUT_DIR / "check 2.pdf"
    cmd = [
        sys.executable,
        str(BASE / "patch_13_03_from_check3.py"),
        "--target", str(etalon),
        "-o", str(out),
    ]
    if not _run(cmd):
        return False
    print(f"  check 2: {out.name} ({out.stat().st_size} bytes)")
    return True


def gen_check_3(etalon: Path) -> bool:
    """add_glyphs (ADD) + patch obj12 из эталона + add_tounicode_cyrillic — без patch_w."""
    out = OUT_DIR / "check 3.pdf"
    step1 = BASE / ".temp_check3_step1.pdf"
    step2 = BASE / ".temp_check3_step2.pdf"
    # 1. add_glyphs (без --replace)
    if not _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--target", str(etalon), "-o", str(step1),
    ]):
        return False
    # 2. patch obj12 из эталона
    if not _run([
        sys.executable, str(BASE / "patch_obj12_from_etalon.py"),
        str(step1), "--etalon", str(etalon), "-o", str(step2),
    ]):
        return False
    # 3. add_tounicode_cyrillic
    if not _run([
        sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
        str(step2), "-o", str(out),
    ]):
        return False
    step1.unlink(missing_ok=True)
    step2.unlink(missing_ok=True)
    print(f"  check 3: {out.name} ({out.stat().st_size} bytes)")
    return True


def gen_check_3a(etalon: Path) -> bool:
    """REPLACE приоритет (размер ~эталон), fallback ADD. Генерирует 3 варианта."""
    ok = 0
    # Вариант 2 (сначала): ADD — нужен для transplant в compact/deepcopy
    out_add = OUT_DIR / "check 3a_add.pdf"
    step1 = BASE / ".temp_3a_add_s1.pdf"
    step2 = BASE / ".temp_3a_add_s2.pdf"
    if _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--target", str(etalon), "-o", str(step1),
    ]) and _run([
        sys.executable, str(BASE / "patch_obj12_from_etalon.py"),
        str(step1), "--etalon", str(etalon), "-o", str(step2),
    ]) and _run([
        sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
        str(step2), "-o", str(out_add),
    ]):
        step1.unlink(missing_ok=True)
        step2.unlink(missing_ok=True)
        ok += 1
        print(f"  check 3a_add: {out_add.name} ({out_add.stat().st_size} bytes) [ADD]")

    # Вариант 1: REPLACE compact (deepcopy) + transplant — размер ~9 KB, глифы Ф,Ч,Ю из add
    # --preserve-w: не меняем /W → /W=эталон → бот пропускает
    out_compact = OUT_DIR / "check 3a_compact.pdf"
    step_r = BASE / ".temp_3a_replace.pdf"
    if _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--replace", "--method", "deepcopy", "--preserve-w", "--target", str(etalon), "-o", str(step_r),
    ]):
        if _run([
            sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
            str(step_r), "--replace", "-o", str(out_compact),
        ]):
            step_r.unlink(missing_ok=True)
            _run([sys.executable, str(BASE / "fix_copy_tounicode.py"), str(out_compact), "-o", str(out_compact)])
            if out_add.exists() and _run([
                sys.executable, str(BASE / "transplant_glyphs.py"),
                "--base", str(out_compact), "--glyphs", str(out_add), "-o", str(out_compact),
            ]):
                pass
            ok += 1
            print(f"  check 3a_compact: {out_compact.name} ({out_compact.stat().st_size} bytes) [REPLACE+transplant]")
    step_r.unlink(missing_ok=True)

    # Вариант 3: REPLACE deepcopy + transplant — размер ~9 KB
    out_deepcopy = OUT_DIR / "check 3a_deepcopy.pdf"
    step_d = BASE / ".temp_3a_deepcopy.pdf"
    if _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--replace", "--method", "deepcopy", "--preserve-w", "--target", str(etalon), "-o", str(step_d),
    ]):
        if _run([
            sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
            str(step_d), "--replace", "-o", str(out_deepcopy),
        ]):
            step_d.unlink(missing_ok=True)
            _run([sys.executable, str(BASE / "fix_copy_tounicode.py"), str(out_deepcopy), "-o", str(out_deepcopy)])
            if out_add.exists() and _run([
                sys.executable, str(BASE / "transplant_glyphs.py"),
                "--base", str(out_deepcopy), "--glyphs", str(out_add), "-o", str(out_deepcopy),
            ]):
                pass
            ok += 1
            print(f"  check 3a_deepcopy: {out_deepcopy.name} ({out_deepcopy.stat().st_size} bytes) [REPLACE deepcopy+transplant]")
    step_d.unlink(missing_ok=True)

    # Вариант 3b: REPLACE decompose + transplant — альтернативный способ копирования глифов
    out_decompose = OUT_DIR / "check 3a_decompose.pdf"
    step_dec = BASE / ".temp_3a_decompose.pdf"
    if _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--replace", "--method", "decompose", "--preserve-w", "--target", str(etalon), "-o", str(step_dec),
    ]):
        if _run([
            sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
            str(step_dec), "--replace", "-o", str(out_decompose),
        ]):
            step_dec.unlink(missing_ok=True)
            _run([sys.executable, str(BASE / "fix_copy_tounicode.py"), str(out_decompose), "-o", str(out_decompose)])
            if out_add.exists() and _run([
                sys.executable, str(BASE / "transplant_glyphs.py"),
                "--base", str(out_decompose), "--glyphs", str(out_add), "-o", str(out_decompose),
            ]):
                pass
            ok += 1
            print(f"  check 3a_decompose: {out_decompose.name} ({out_decompose.stat().st_size} bytes) [REPLACE decompose+transplant]")
    step_dec.unlink(missing_ok=True)

    # Вариант 4: patch_13_03_from_check3 (полная подмена шрифта) — база 16.pdf если есть
    out_font = OUT_DIR / "check 3a_font.pdf"
    base_font = _find_16() or etalon
    if _run([
        sys.executable, str(BASE / "patch_13_03_from_check3.py"),
        "--target", str(base_font), "-o", str(out_font),
    ]):
        ok += 1
        print(f"  check 3a_font: {out_font.name} ({out_font.stat().st_size} bytes) [font swap, base={base_font.name}]")

    # Вариант 5: ADD на базе 17.pdf (BaseFont AASONC для проверки)
    base_17 = _find_17()
    out_17 = OUT_DIR / "check 3a_17.pdf"  # всегда определён для 3a_17_meta
    if base_17:
        out_17 = OUT_DIR / "check 3a_17.pdf"
        step1 = BASE / ".temp_3a_17_s1.pdf"
        step2 = BASE / ".temp_3a_17_s2.pdf"
        if _run([
            sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
            "--target", str(base_17), "-o", str(step1),
        ]) and _run([
            sys.executable, str(BASE / "patch_obj12_from_etalon.py"),
            str(step1), "--etalon", str(base_17), "-o", str(step2),
        ]) and _run([
            sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
            str(step2), "-o", str(out_17),
        ]):
            step1.unlink(missing_ok=True)
            step2.unlink(missing_ok=True)
            ok += 1
            print(f"  check 3a_17: {out_17.name} ({out_17.stat().st_size} bytes) [ADD, base 17.pdf]")

    # Вариант 5b: hybrid-safe REPLACE (Ф/Ч/Ю все через REPLACE, Ю на слоте 0221=Е)
    # CIDToGIDMap = эталон → проходит бот. /W = эталон. Без «ИсхоЮящий».
    out_hybrid = OUT_DIR / "check 3a_hybrid.pdf"
    step_h = BASE / ".temp_3a_hybrid.pdf"
    if _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--replace", "--hybrid-safe", "--method", "deepcopy",
        "--target", str(etalon), "-o", str(step_h),
    ]):
        if _run([
            sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
            str(step_h), "--replace", "-o", str(out_hybrid),
        ]):
            step_h.unlink(missing_ok=True)
            _run([sys.executable, str(BASE / "fix_copy_tounicode.py"), str(out_hybrid), "-o", str(out_hybrid)])
            ok += 1
            print(f"  check 3a_hybrid: {out_hybrid.name} ({out_hybrid.stat().st_size} bytes) [REPLACE-only, CIDToGIDMap=эталон]")
    step_h.unlink(missing_ok=True)

    # Гибриды: внурянка/структура ↔ визуал
    import shutil
    # 3a_transplant: REPLACE-структура + глифы из ADD (визуал из add в compact)
    out_transplant = OUT_DIR / "check 3a_transplant.pdf"
    if out_compact.exists() and out_add.exists() and _run([
        sys.executable, str(BASE / "transplant_glyphs.py"),
        "--base", str(out_compact), "--glyphs", str(out_add), "-o", str(out_transplant),
    ]):
        ok += 1
        print(f"  check 3a_transplant: {out_transplant.name} ({out_transplant.stat().st_size} bytes) [compact+glyphs from add]")

    # 3a_add_meta: ADD (визуал) + метаданные из compact (ID, CreationDate, Producer)
    out_add_meta = OUT_DIR / "check 3a_add_meta.pdf"
    if out_add.exists() and (out_compact.exists() or out_deepcopy.exists()):
        meta_src = out_compact if out_compact.exists() else OUT_DIR / "check 3a_deepcopy.pdf"
        if meta_src.exists() and _run([
            sys.executable, str(BASE / "copy_metadata.py"),
            "--from", str(meta_src), "--to", str(out_add), "-o", str(out_add_meta),
        ]):
            ok += 1
            print(f"  check 3a_add_meta: {out_add_meta.name} ({out_add_meta.stat().st_size} bytes) [ADD + meta from compact]")

    # 3a_17_meta: ADD на 17.pdf + метаданные из compact
    out_17_meta = OUT_DIR / "check 3a_17_meta.pdf"
    if out_17.exists() and (out_compact.exists() or out_deepcopy.exists()):
        meta_src = out_compact if out_compact.exists() else OUT_DIR / "check 3a_deepcopy.pdf"
        if meta_src.exists() and _run([
            sys.executable, str(BASE / "copy_metadata.py"),
            "--from", str(meta_src), "--to", str(out_17), "-o", str(out_17_meta),
        ]):
            ok += 1
            print(f"  check 3a_17_meta: {out_17_meta.name} ({out_17_meta.stat().st_size} bytes) [ADD 17 + meta from compact]")

    # check 3a.pdf: compact или deepcopy — размер ~9 KB для прохождения проверки TG (лимит 8.8–9.2 KB)
    out_main = OUT_DIR / "check 3a.pdf"
    if out_compact.exists():
        shutil.copy2(out_compact, out_main)
        print(f"  check 3a: {out_main.name} ({out_compact.stat().st_size} B, для TG)")
    elif out_deepcopy.exists():
        shutil.copy2(out_deepcopy, out_main)
        print(f"  check 3a: {out_main.name} ({out_deepcopy.stat().st_size} B)")
    elif out_add.exists():
        shutil.copy2(out_add, out_main)
        print(f"  check 3a: {out_main.name} (копия 3a_add, визуал ок)")
    elif out_compact.exists():
        shutil.copy2(out_compact, out_main)
        print(f"  check 3a: {out_main.name} (копия 3a_compact)")
    else:
        return False
    return ok > 0


def gen_check_3b(etalon: Path) -> bool:
    """Структура check 1: check 1 + add_tounicode --replace. Визуал+копирование как у check 3."""
    out = OUT_DIR / "check 3b.pdf"
    if not gen_check_1(etalon):  # создаёт check 1
        return False
    if not _run([
        sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
        str(OUT_DIR / "check 1.pdf"), "--replace", "-o", str(out),
    ]):
        return False
    print(f"  check 3b: {out.name} ({out.stat().st_size} bytes) [структура check 1]")
    return True


def gen_check_4(etalon: Path) -> bool:
    """obj12+obj16 из эталона + add_tounicode — копирование + попытка структуры эталона."""
    out = OUT_DIR / "check 4.pdf"
    step1 = BASE / ".temp_check4_step1.pdf"
    step2 = BASE / ".temp_check4_step2.pdf"
    if not _run([
        sys.executable, str(BASE / "add_glyphs_to_13_03.py"),
        "--target", str(etalon), "-o", str(step1),
    ]):
        return False
    if not _run([
        sys.executable, str(BASE / "patch_obj12_obj16_from_etalon.py"),
        str(step1), "--etalon", str(etalon), "-o", str(step2),
    ]):
        return False
    if not _run([
        sys.executable, str(BASE / "add_tounicode_cyrillic.py"),
        str(step2), "-o", str(out),
    ]):
        return False
    step1.unlink(missing_ok=True)
    step2.unlink(missing_ok=True)
    print(f"  check 4: {out.name} ({out.stat().st_size} bytes)")
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Генерация check 1, 2, 3... в Лучший чек")
    ap.add_argument("--etalon", type=Path, default=None, help="Эталон 13.pdf")
    ap.add_argument("--only", default=None, help="Только check N (1, 2, 3, 4, 3a, 3b). 3a даёт: 3a.pdf, 3a_compact, 3a_add, 3a_deepcopy, 3a_font, 3a_17)")
    args = ap.parse_args()

    etalon = _find_etalon(args.etalon)
    if not etalon:
        print("[ERROR] Эталон 13.pdf не найден. Укажите --etalon путь", file=sys.stderr)
        return 1
    if not DONOR.exists():
        print(f"[ERROR] Донор не найден: {DONOR}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    etalon_size = etalon.stat().st_size
    print(f"Эталон: {etalon.name} ({etalon_size} bytes)")
    print(f"Донор:  {DONOR.name}")
    print("---")

    gens = [
        (1, gen_check_1),
        (2, gen_check_2),
        (3, gen_check_3),
        ("3a", gen_check_3a),
        ("3b", gen_check_3b),
        (4, gen_check_4),
    ]
    if args.only:
        only_val = int(args.only) if args.only.isdigit() else args.only
        gens = [(n, fn) for n, fn in gens if n == only_val]
        if not gens:
            print(f"[ERROR] Нет check {args.only}", file=sys.stderr)
            return 1

    ok = 0
    for n, fn in gens:
        if fn(etalon):
            ok += 1

    print("---")
    print(f"Готово: {ok}/{len(gens)}")
    return 0 if ok == len(gens) else 1


if __name__ == "__main__":
    sys.exit(main())
