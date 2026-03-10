#!/usr/bin/env python3
"""Генерация чека ВТБ из конфига vtb_config.json.

Меняй значения в vtb_config.json — выравнивание сохранится.

Использование:
  python3 vtb_patch_from_config.py [input.pdf]
  python3 vtb_patch_from_config.py --config vtb_config.json input.pdf
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from vtb_test_generator import (
    PTS_CALIBRATION,
    build_tj,
    build_date_tj,
    tm_x_touch_wall,
)
from vtb_sber_reference import get_vtb_per_field_params
from vtb_cmap import text_to_cids, format_amount


def build_amount_tj(amount: int) -> bytes:
    """Сумма → TJ (kern -11.11111). 10000 → '10 000 ₽'."""
    s = format_amount(amount)
    cids = text_to_cids(s)
    if not cids:
        raise ValueError(f"Не удалось закодировать сумму: {s}")
    return b"[" + build_tj(cids, kern="-11.11111") + b"]"


def build_text_tj(text: str, kern: str = "-16.66667", wrap: bool = True) -> bytes:
    """Текст (ФИО) → TJ. wrap=True для recipient (в [ ]), False для payer (без [ ])."""
    cids = text_to_cids(text)
    if not cids:
        raise ValueError(f"Символ не найден в CMap: {text}")
    tj = build_tj(cids, kern=kern)
    return b"[" + tj + b"]" if wrap else tj


def patch_from_values(
    data: bytearray,
    pdf_path: Path,
    *,
    date_str: str | datetime | None = None,
    payer: str | None = None,
    recipient: str | None = None,
    phone: str | None = None,
    bank: str | None = None,
    amount: int | None = None,
    keep_metadata: bool = False,
) -> bytes:
    """Патч с кастомными значениями. Использует OLD из 09-03-26_03-47."""
    params = get_vtb_per_field_params(pdf_path)
    wall = params["wall"]
    pts = PTS_CALIBRATION

    # Парсим дату
    if date_str == "now" or date_str is None:
        dt = datetime.now()
    elif isinstance(date_str, datetime):
        dt = date_str
    else:
        try:
            dt = datetime.strptime(date_str.strip(), "%d.%m.%Y, %H:%M")
        except ValueError:
            dt = datetime.now()

    new_date_tj = build_date_tj(dt)
    new_payer_tj = build_text_tj(payer, wrap=False) if payer else None
    new_recipient_tj = build_text_tj(recipient, wrap=True) if recipient else None
    new_recipient_21 = build_text_tj(recipient, kern="-21.42857", wrap=True) if recipient else None
    new_phone_tj = build_text_tj(phone, wrap=True) if phone else None
    new_bank_tj = build_text_tj(bank, wrap=True) if bank else None
    new_amount_tj = build_amount_tj(amount) if amount else None

    # OLD patterns (из 09-03-26_03-47)
    OLD_DATE = (
        b'[(\x00\x13)-16.66667 (\x00\x1c)-16.66667 (\x00\x11)-16.66667 (\x00\x13)-16.66667 (\x00\x16)-16.66667 (\x00\x11)-16.66667 '
        b'(\x00\x15)-16.66667 (\x00\x13)-16.66667 (\x00\x15)-16.66667 (\x00\x19)-16.66667 (\x00\x0f)-16.66667 (\x00\x03)-16.66667 '
        b'(\x00\x13)-16.66667 (\x00\x17)-16.66667 (\x00\x1d)-16.66667 (\x00\x17)-16.66667 (\x00\x1a)]'
    )
    OLD_PAYER = (
        b'(\x02\x1c)-16.66667 (\x02G)-16.66667 (\x02A)-16.66667 (\x02F)-16.66667 (\x02M)-16.66667 (\x02<)-16.66667 '
        b'(\x02I)-16.66667 (\x02@)-16.66667 (\x02L)-16.66667 (\x00\x03)-16.66667 '
        b'(\x02!)-16.66667 (\x02>)-16.66667 (\x02?)-16.66667 (\x02A)-16.66667 (\x02I)-16.66667 (\x02X)-16.66667 '
        b'(\x02A)-16.66667 (\x02>)-16.66667 (\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02")-16.66667 (\x00\x11)'
    )
    OLD_RECIPIENT_16 = (
        b'[(\x02!)-16.66667 (\x02P)-16.66667 (\x02D)-16.66667 (\x02H)-16.66667 (\x00\x03)-16.66667 (\x02\x1c)-16.66667 '
        b'(\x02I)-16.66667 (\x02N)-16.66667 (\x02J)-16.66667 (\x02I)-16.66667 (\x02J)-16.66667 (\x02>)-16.66667 '
        b'(\x02D)-16.66667 (\x02S)-16.66667 (\x00\x03)-16.66667 (\x02\x1d)-16.66667 (\x00\x11)]'
    )
    OLD_RECIPIENT_21 = (
        b'[(\x02!)-21.42857 (\x02P)-21.42857 (\x02D)-21.42857 (\x02H)-21.42857 (\x00\x03)-21.42857 (\x02\x1c)-21.42857 '
        b'(\x02I)-21.42857 (\x02N)-21.42857 (\x02J)-21.42857 (\x02I)-21.42857 (\x02J)-21.42857 (\x02>)-21.42857 '
        b'(\x02D)-21.42857 (\x02S)-21.42857 (\x00\x03)-21.42857 (\x02\x1d)-21.42857 (\x00\x11)]'
    )
    OLD_AMOUNT = b'[(\x00\x14)-11.11111 (\x00\x03)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x13)-11.11111 (\x00\x03)-11.11111 (\x04@)]'
    OLD_TM_AMOUNT = b"1 0 0 1 211.95001 72.37499 Tm"
    OLD_PHONE = (
        b'[(\x00\x0e)-16.66667 (\x00\x1a)-16.66667 (\x00\x03)-16.66667 (\x00\x0b)-16.66667 (\x00\x1c)-16.66667 (\x00\x13)-16.66667 '
        b'(\x00\x19)-16.66667 (\x00\x0c)-16.66667 (\x00\x03)-16.66667 (\x00\x15)-16.66667 (\x00\x16)-16.66667 (\x00\x19)-16.66667 '
        b'(\x00\x10)-16.66667 (\x00\x1b)-16.66667 (\x00\x19)-16.66667 (\x00\x10)-16.66667 (\x00\x14)-16.66667 (\x00\x16)]'
    )
    OLD_BANK = b'[(\x02-)-16.66667 (\x02=)-16.66667 (\x02A)-16.66667 (\x02L)-16.66667 (\x02=)-16.66667 (\x02<)-16.66667 (\x02I)-16.66667 (\x02F)]'
    OLD_TM_PHONE = b"1 0 0 1 174.825 179.25 Tm"
    OLD_TM_BANK = b"1 0 0 1 216.33751 155.25 Tm"

    def n_glyphs(tj: bytes) -> int:
        """Количество глифов в TJ: кернов + 1."""
        return sum(tj.count(k) for k in (b"-16.66667", b"-11.11111", b"-21.42857", b"-8.33333")) + 1

    for m in re.finditer(rb"<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n", data, re.DOTALL):
        stream_len = int(m.group(2))
        stream_start = m.end()
        len_num_start = m.start(2)
        if stream_start + stream_len > len(data):
            continue
        try:
            dec = __import__("zlib").decompress(bytes(data[stream_start : stream_start + stream_len]))
        except Exception:
            continue
        if b"BT" not in dec:
            continue

        new_dec = dec

        if new_date_tj and OLD_DATE in new_dec:
            new_dec = new_dec.replace(OLD_DATE, new_date_tj)
            n = n_glyphs(new_date_tj)
            new_x = tm_x_touch_wall(wall, n, pts["date"])
            tm_date_m = re.search(rb"(1 0 0 1 )([\d.]+)( 275\.25 Tm)", new_dec)
            if tm_date_m and float(tm_date_m.group(2)) > 100:
                new_dec = new_dec.replace(tm_date_m.group(0), tm_date_m.group(1) + f"{new_x:.5f}".encode() + tm_date_m.group(3))

        if new_payer_tj and OLD_PAYER in new_dec:
            new_dec = new_dec.replace(OLD_PAYER, new_payer_tj)
            n = n_glyphs(new_payer_tj)
            new_x = tm_x_touch_wall(wall, n, pts["payer"])
            for old_tm in (b"1 0 0 1 149.8125 227.25 Tm", b"1 0 0 1 133.19 227.25 Tm", b"1 0 0 1 148.40 227.25 Tm", b"1 0 0 1 147.21 227.25 Tm"):
                if old_tm in new_dec:
                    new_dec = new_dec.replace(old_tm, f"1 0 0 1 {new_x:.5f} 227.25 Tm".encode())
                    break

        if new_recipient_tj:
            if OLD_RECIPIENT_16 in new_dec:
                new_dec = new_dec.replace(OLD_RECIPIENT_16, new_recipient_tj)
                n = n_glyphs(new_recipient_tj)
                new_x = tm_x_touch_wall(wall, n, pts["recipient"])
                tm_m = re.search(rb"(1 0 0 1 )([\d.]+)( 203\.25 Tm)", new_dec)
                if tm_m and float(tm_m.group(2)) > 100:
                    new_dec = new_dec.replace(tm_m.group(0), tm_m.group(1) + f"{new_x:.5f}".encode() + tm_m.group(3))
            if new_recipient_21 and OLD_RECIPIENT_21 in new_dec:
                new_dec = new_dec.replace(OLD_RECIPIENT_21, new_recipient_21)
                pts_h = pts["recipient"] * (10.5 / 9)
                shift = (17 - n_glyphs(new_recipient_21)) * pts_h / 2
                tm_m = re.search(rb"(1 0 0 1 )([\d.]+)( 327\.11249 Tm)", new_dec)
                if tm_m and 90 < float(tm_m.group(2)) < 120:
                    old_x = float(tm_m.group(2))
                    new_dec = new_dec.replace(tm_m.group(0), tm_m.group(1) + f"{old_x + shift:.5f}".encode() + tm_m.group(3))

        if new_amount_tj:
            if OLD_AMOUNT in new_dec:
                new_dec = new_dec.replace(OLD_AMOUNT, new_amount_tj)
            if new_amount_tj in new_dec and OLD_TM_AMOUNT in new_dec:
                n = n_glyphs(new_amount_tj)
                new_x = tm_x_touch_wall(wall, n, pts["amount"])
                new_dec = new_dec.replace(OLD_TM_AMOUNT, f"1 0 0 1 {new_x:.5f} 72.37499 Tm".encode())

        if new_phone_tj and OLD_PHONE in new_dec:
            new_dec = new_dec.replace(OLD_PHONE, new_phone_tj)
            n = n_glyphs(new_phone_tj)
            new_x = tm_x_touch_wall(wall, n, pts.get("phone", 4.57))
            new_dec = new_dec.replace(OLD_TM_PHONE, f"1 0 0 1 {new_x:.5f} 179.25 Tm".encode())

        if new_bank_tj and OLD_BANK in new_dec:
            new_dec = new_dec.replace(OLD_BANK, new_bank_tj)
            n = n_glyphs(new_bank_tj)
            new_x = tm_x_touch_wall(wall, n, pts.get("bank", 5.09))
            new_dec = new_dec.replace(OLD_TM_BANK, f"1 0 0 1 {new_x:.5f} 155.25 Tm".encode())

        if new_dec != dec:
            new_raw = __import__("zlib").compress(new_dec, 6)
            delta = len(new_raw) - stream_len
            old_len_str = str(stream_len).encode()
            new_len_str = str(len(new_raw)).encode()
            data = bytearray(data[:stream_start] + new_raw + data[stream_start + stream_len :])
            num_end = len_num_start + len(old_len_str)
            data[len_num_start:num_end] = new_len_str.ljust(len(old_len_str))[: len(old_len_str)]
            xref_m = re.search(rb"xref\r?\n(\d+)\s+(\d+)\r?\n((?:\d{10}\s+\d{5}\s+[nf]\s*\r?\n)+)", data)
            if xref_m:
                entries = bytearray(xref_m.group(3))
                for em in re.finditer(rb"(\d{10})(\s+\d{5}\s+[nf]\s*\r?\n)", entries):
                    offset = int(em.group(1))
                    if offset > stream_start:
                        entries[em.start(1) : em.start(1) + 10] = f"{offset + delta:010d}".encode()
                data[xref_m.start(3) : xref_m.end(3)] = bytes(entries)
            startxref_m = re.search(rb"startxref\r?\n(\d+)\r?\n", data)
            if startxref_m and delta != 0:
                pos = startxref_m.start(1)
                old_pos = int(startxref_m.group(1))
                data[pos : pos + len(str(old_pos))] = str(old_pos + delta).encode()
            break

    if not keep_metadata:
        from vtb_test_generator import update_id, update_creation_date
        update_creation_date(data, f"D:{datetime.now().strftime('%Y%m%d%H%M%S')}+03'00'")
        update_id(data)
    return bytes(data)


def main():
    base = Path(__file__).parent
    config_path = base / "vtb_config.json"
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            config_path = Path(sys.argv[i + 1])
            break

    if not config_path.exists():
        print(f"[ERROR] Конфиг не найден: {config_path}")
        print("Создай vtb_config.json (см. vtb_config.json)")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    inp = Path(sys.argv[-1]) if sys.argv and not sys.argv[-1].startswith("-") else Path("/Users/aleksandrzerebatav/Downloads/09-03-26_03-47.pdf")
    if not inp.exists():
        inp = base / "Тест ВТБ" / "09-03-26_03-47_1.pdf"
    if not inp.exists():
        print(f"[ERROR] Файл не найден: {inp}")
        sys.exit(1)

    out_dir = base / cfg.get("output_folder", "10.03")
    out_dir.mkdir(parents=True, exist_ok=True)

    data = bytearray(inp.read_bytes())
    try:
        data = patch_from_values(
            data,
            inp,
            date_str=cfg.get("date", "now"),
            payer=cfg.get("payer"),
            recipient=cfg.get("recipient"),
            phone=cfg.get("phone"),
            bank=cfg.get("bank"),
            amount=cfg.get("amount"),
            keep_metadata=not cfg.get("update_id", True),
        )
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    out_name = cfg.get("output_name") or f"{inp.stem}_config"
    if not out_name.endswith(".pdf"):
        out_name += ".pdf"
    out_path = out_dir / out_name
    out_path.write_bytes(data)
    print(f"[OK] {out_path}")
    print(f"  date={cfg.get('date')}, payer={cfg.get('payer')}, recipient={cfg.get('recipient')}, amount={cfg.get('amount')}")


if __name__ == "__main__":
    main()
