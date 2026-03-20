#!/usr/bin/env python3
"""Telegram-бот без внешних зависимостей (только stdlib).
Отправьте чек → выберите банк → введите "с какой на какую" → получите PDF.
"""
# Обход: прокси и SSL (корпоративные сети / Python на macOS)
import os as _os
import ssl
import urllib.request as _ur
from urllib.parse import urlparse
_ssl_ctx = ssl._create_unverified_context()
_proxy_url = _os.environ.get("HTTPS_PROXY") or _os.environ.get("https_proxy") or _os.environ.get("HTTP_PROXY") or _os.environ.get("http_proxy")
_handlers = [_ur.HTTPSHandler(context=_ssl_ctx)]
if _proxy_url:
    _proxy = {"http": _proxy_url, "https": _proxy_url}
    _handlers.insert(0, _ur.ProxyHandler(_proxy))
    # Прокси с авторизацией user:pass@host:port
    try:
        p = urlparse(_proxy_url)
        if p.username and p.password:
            _pm = _ur.HTTPPasswordMgrWithDefaultRealm()
            _pm.add_password(None, f"{p.scheme}://{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}", p.username, p.password)
            _handlers.insert(1, _ur.ProxyBasicAuthHandler(_pm))
    except Exception:
        pass
_ur.install_opener(_ur.build_opener(*_handlers))

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

_BOT_DIR = Path(__file__).parent
_ADD_GLYPHS_SCRIPT = _BOT_DIR / "add_glyphs_to_13_03.py"

# Загрузка .env вручную (без python-dotenv)
try:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
except Exception:
    pass

from pdf_patcher import patch_pdf_file, patch_amount as pdf_patch_amount, format_amount_display
from alfa_transgran_patch import patch_transgran, extract_transgran_fields
from vtb_transgran_patch import patch_vtb_transgran, extract_fields as extract_vtb_transgran_fields, parse_rate, format_credited, format_amount_rub
from vtb_patch_from_config import patch_from_values, patch_amount_only
from vtb_cmap import get_unsupported_chars, format_unsupported_error, suggest_replacement, FALLBACK_TIPS
from vtb_sber_reference import scan_vtb_unsupported_chars
from vtb_test_generator import update_creation_date
from receipt_db import (
    receipt_supports_chars,
    get_missing_chars_in_receipt,
    chars_from_text_fields,
    find_donor,
    get_bank_report,
    get_bank_counts,
    load_index,
    add_receipt_to_index,
    build_and_save,
    get_operation_id_from_pdf,
    COMMON_AMOUNTS,
    VTB_SUBTYPES,
)

BASE = "https://api.telegram.org/bot"

VTB_UNSUPPORTED_NOTICE = (
    "⚠️ Нельзя использовать: ё, Ё, неразрывный дефис (‑). "
    "Замените на: е, Е, обычный дефис (-)."
)
USER_STATE: dict[int, dict] = {}

# Разрешённые user_id (через запятую в .env: ALLOWED_USER_IDS=123456,789012)
_ALLOWED_IDS: set[int] = set()
_raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
if _raw:
    for s in _raw.replace(" ", "").split(","):
        if s.isdigit():
            _ALLOWED_IDS.add(int(s))

ACCESS_DENIED_MSG = "🚫 Доступ запрещён. Бот доступен только ограниченному кругу пользователей."

MAIN_MENU_TEXT = (
    "👋 Главная\n\n"
    "📄 Загрузить чек — отправьте PDF и измените сумму/ФИО.\n"
    "✨ Сгенерировать — чек из базы без загрузки.\n"
    "📋 Создать выписку — редактирование или по чеку.\n"
    "📂 Проверка базы — просмотр и добавление чеков-доноров.\n"
    "📋 Заявки — просмотр и смена статусов."
)
MAIN_MENU_KB = [
    [{"text": "📄 Загрузить чек", "callback_data": "main_new"}],
    [{"text": "✨ Сгенерировать", "callback_data": "main_generate"}],
    [{"text": "📋 Создать выписку", "callback_data": "main_stmt"}],
    [{"text": "📂 Проверка базы", "callback_data": "main_db"}],
    [{"text": "📋 Заявки", "callback_data": "main_zayavki"}],
    [{"text": "📝 Последние изменения", "callback_data": "main_changelog"}],
]

CHANGELOG_TEXT = (
    "📝 Последние изменения\n\n"
    "• 🏦 Выписка Альфа-Банк — блоковое редактирование выписки. "
    "3 блока: Операции, Сводка (авто-расчёт), Реквизиты. "
    "Автозаполнение из чека, проверка доступных символов в шрифте. "
    "Побайтовый CID-патчинг с сохранением структуры и выравнивания.\n\n"
    "• 🌍 ВТБ Трансгран (UZS) — режим для трансграничных чеков ВТБ. "
    "Замена суммы, телефона, даты. Зачисление считается автоматически (сумма × курс). "
    "Сохраняет структуру PDF, правильное выравнивание, меняет Document ID.\n\n"
    "• 🌐 Альфа Трансгран — режим для трансграничных чеков Альфа-Банка. "
    "Замена суммы, комиссии, курса, имени, телефона и номера операции. "
    "Сохраняет структуру, меняет Document ID.\n\n"
    "• Система заявок — после создания чека можно оформить заявку. "
    "В разделе «Заявки» — список, нажмите на заявку и выберите статус: В работе или Оплачено.\n\n"
    "• Полная замена ВТБ — режим «Все поля»: замена суммы, даты, ФИО плательщика/получателя, телефона и банка. "
    "Замена ФИО и банка пока может работать некорректно.\n\n"
    "• Автозамена ё→е, ‑→- — при вводе ФИО/телефона/банка символы заменяются автоматически, если structurally всё ок.\n\n"
    "• Проверка базы — кнопка «Обновить индекс» для пересборки после ручного добавления чеков."
)

ZAYAVKI_DIR = Path(__file__).parent / "заявки"
STATUS_LABELS = {"новый": "🆕 Новый", "в работе": "🔄 В работе", "оплачено": "💰 Оплачено"}
STATUS_SHORT = {"n": "новый", "w": "в работе", "o": "оплачено"}
STATUS_TO_SHORT = {"новый": "n", "в работе": "w", "оплачено": "o"}


def save_zayavka(uid: int, username: str, amount_from: int, amount_to: int, bank: str, pdf_name: str, description: str) -> Path | None:
    """Сохранить заявку в JSON (файл по дате). Возвращает путь к файлу или None."""
    try:
        ZAYAVKI_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        filepath = ZAYAVKI_DIR / f"{day}.json"
        data = []
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = []
        entry = {
            "id": f"{int(time.time() * 1000)}_{uid}",
            "user_id": uid,
            "username": username or "",
            "timestamp": datetime.now().isoformat(),
            "amount_from": amount_from,
            "amount_to": amount_to,
            "bank": bank,
            "pdf_name": pdf_name,
            "описание": description,
            "статус": "новый",
        }
        data.append(entry)
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return filepath
    except Exception as e:
        print(f"  ⚠️ Ошибка сохранения заявки: {e}")
        return None


def load_all_zayavki() -> list[dict]:
    """Загрузить все заявки из JSON (все файлы заявки/*.json), сортировка по дате (новые сверху)."""
    result = []
    if not ZAYAVKI_DIR.exists():
        return result
    for fp in sorted(ZAYAVKI_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for e in data:
                e.setdefault("статус", "новый")
                result.append(e)
        except (json.JSONDecodeError, OSError):
            continue
    result.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return result


def update_zayavka_status(zayavka_id: str, new_status: str) -> bool:
    """Обновить статус заявки по id. Вернуть True если успешно."""
    if not ZAYAVKI_DIR.exists():
        return False
    for fp in ZAYAVKI_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for i, e in enumerate(data):
                if e.get("id") == zayavka_id:
                    data[i]["статус"] = new_status
                    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def get_zayavka_by_id(zayavka_id: str) -> dict | None:
    """Найти заявку по id."""
    for z in load_all_zayavki():
        if z.get("id") == zayavka_id:
            return z
    return None


def build_zayavki_list(limit: int = 15) -> tuple[str, str]:
    """Список заявок: каждая — кнопка для открытия."""
    try:
        items = load_all_zayavki()
    except Exception as e:
        print(f"  ⚠️ Ошибка загрузки заявок: {e}")
        return "📋 Заявки:\n\nОшибка загрузки.", json.dumps({"inline_keyboard": []})
    if not items:
        return "📋 Заявки:\n\nНет заявок. Нажмите на заявку для просмотра.", json.dumps({"inline_keyboard": []})
    lines = [f"📋 Заявки (всего {len(items)})\n\nНажмите на заявку:"]
    keyboard = []
    for i, z in enumerate(items[:limit]):
        try:
            st = z.get("статус", "новый")
            am_from = int(z.get("amount_from") or 0)
            am_to = int(z.get("amount_to") or 0)
            ts = (z.get("timestamp") or "")[:10]
            lbl = STATUS_LABELS.get(st, st)
            btn_text = f"{i+1}. {format_amount_display(am_from)}→{format_amount_display(am_to)} ₽ · {ts} {lbl}"[:64]
            zid = z.get("id") or ""
            if not zid:
                continue
            cb = f"v_{zid}"[:64]
            keyboard.append([{"text": btn_text, "callback_data": cb}])
        except Exception as e:
            print(f"  ⚠️ Ошибка заявки {i}: {e}")
            continue
    return "\n".join(lines), json.dumps({"inline_keyboard": keyboard})


def build_zayavka_detail(zayavka_id: str) -> tuple[str, str] | None:
    """Карточка заявки с кнопками [В работе] [Оплачено] [Назад]."""
    z = get_zayavka_by_id(zayavka_id)
    if not z:
        return None
    st = z.get("статус", "новый")
    am_from = int(z.get("amount_from") or 0)
    am_to = int(z.get("amount_to") or 0)
    ts = (z.get("timestamp") or "")[:16].replace("T", " ")
    desc = z.get("описание") or "(без описания)"
    bank = z.get("bank", "")
    bank_name = {"alfa": "Альфа", "vtb": "ВТБ", "auto": "Авто"}.get(bank, bank)
    lbl = STATUS_LABELS.get(st, st)
    txt = (
        f"📋 Заявка\n\n"
        f"💰 {format_amount_display(am_from)} → {format_amount_display(am_to)} ₽\n"
        f"🏦 {bank_name} · {ts}\n"
        f"📌 {lbl}\n\n"
        f"📝 {desc}"
    )
    cb_w = f"st_{zayavka_id}_w"[:64]
    cb_o = f"st_{zayavka_id}_o"[:64]
    kb = {
        "inline_keyboard": [
            [
                {"text": "🔄 В работе", "callback_data": cb_w},
                {"text": "💰 Оплачено", "callback_data": cb_o},
            ],
            [{"text": "⬅️ К списку", "callback_data": "main_zayavki"}],
        ]
    }
    return txt, json.dumps(kb)


def tg_request(token: str, method: str, data: dict | None = None, files: dict | None = None) -> dict:
    url = f"{BASE}{token}/{method}"
    max_retries = 3
    last_err = None
    for attempt in range(max_retries):
        try:
            return _tg_request_once(url, data, files)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = e
            if attempt < max_retries - 1:
                delay = (attempt + 1) * 3
                print(f"  ⚠️ Сеть: {e}. Повтор через {delay} сек...")
                time.sleep(delay)
            else:
                raise
    raise last_err


def _tg_request_once(url: str, data: dict | None, files: dict | None) -> dict:
    if files:
        boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
        body = []
        if data:
            for k, v in data.items():
                body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fname, fbytes) in files.items():
            body.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: application/pdf\r\n\r\n".encode())
            body.append(fbytes)
            body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())
        full_body = b"".join(body)
        req = urllib.request.Request(url, data=full_body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.loads(r.read().decode())


def tg_get_file(token: str, file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < 2:
                print(f"  ⚠️ Загрузка файла: {e}. Повтор...")
                time.sleep(3)
            else:
                raise


def tg_get_file_path(token: str, file_id: str) -> str:
    r = tg_request(token, "getFile", {"file_id": file_id})
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile failed"))
    return r["result"]["file_path"]


def parse_amounts(text: str) -> tuple[int, int] | None:
    nums = re.findall(r"\d+", text.strip())
    if len(nums) >= 2:
        return int(nums[0]), int("".join(nums[1:]))
    return None


def parse_amount_pairs(text: str) -> list[tuple[int, int]]:
    """Разбор '10 5000 50 1000' → [(10, 5000), (50, 1000)]."""
    nums = re.findall(r"\d+", text.strip())
    pairs = []
    i = 0
    while i + 1 < len(nums):
        pairs.append((int(nums[i]), int(nums[i + 1])))
        i += 2
    return pairs


def parse_custom_replacement(text: str) -> tuple[str, str] | None:
    """Разбор 'поле=значение' → (поле, значение)."""
    text = text.strip()
    if "=" in text:
        k, _, v = text.partition("=")
        return (k.strip().lower(), v.strip()) if k.strip() and v.strip() else None
    return None


def run_check_caps(payer: str, recipient: str) -> tuple[bool, str]:
    """Проверка ФИО: получатся ли заглавные буквы. Возвращает (ok, output)."""
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    try:
        proc = subprocess.run(
            [os.sys.executable, str(_ADD_GLYPHS_SCRIPT), "--check-caps", "--payer", payer or "", "--recipient", recipient or ""],
            cwd=str(_BOT_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        full = out or err or proc.stderr.decode(errors="replace") if proc.stderr else ""
        if proc.returncode != 0 and not full:
            full = f"Код выхода: {proc.returncode}"
        return proc.returncode == 0, full[:4000]  # лимит Telegram
    except subprocess.TimeoutExpired:
        return False, "Таймаут проверки (30 сек)"
    except Exception as e:
        return False, f"Ошибка: {e}"


def _decimal_safe_incs(hex_char: str) -> list[int]:
    """Инкременты 1..15, результат — цифра 0-9. Верификатор VTB требует decimal в pos=0."""
    base = int(hex_char.upper(), 16)
    return [i for i in range(1, 16) if (base + i) % 16 < 10]


def _change_one_char_in_id(data: bytearray) -> None:
    """Изменить ровно один символ в /ID. pos=0, результат 0-9 (как в add_glyphs — VTB верификатор)."""
    id_m = re.search(rb'/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\]', data)
    if not id_m:
        return
    hex1 = id_m.group(1).decode().upper()
    pos = 0
    incs = _decimal_safe_incs(hex1[pos])
    if not incs:
        return
    inc = incs[0]
    idx = "0123456789ABCDEF".find(hex1[pos])
    new_c = "0123456789ABCDEF"[(idx + inc) % 16]
    new1 = hex1[:pos] + new_c + hex1[pos + 1:]
    slot_len = id_m.end(1) - id_m.start(1)
    new_enc = new1.encode().ljust(slot_len)[:slot_len]
    data[id_m.start(1) : id_m.end(1)] = new_enc
    data[id_m.start(2) : id_m.end(2)] = new_enc


def run_sbp_generate_15_03(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация с шаблоном 15-03-26. Целостность: content-only если ФИО влезает, иначе add_glyphs + keep operation_id."""
    _sbp = _BOT_DIR / "база_чеков" / "vtb" / "СБП"
    _tpl = _sbp / "15-03-26_00-00.pdf"
    if not _tpl.exists():
        return False, None, "Шаблон 15-03-26_00-00.pdf не найден"
    req = {c for c in chars_from_text_fields(payer or "", recipient or "", "+7 (999) 000-00-00")}
    req.discard(" ")
    try:
        from receipt_db import get_receipt_chars, get_missing_chars_in_receipt, _normalize_char
    except ImportError:
        return False, None, ""
    req = {_normalize_char(c) for c in req}
    missing = get_missing_chars_in_receipt(_tpl, req) if req else set()
    if not date_str or date_str.strip().lower() in ("now", "сейчас"):
        dt = datetime.now()
        date_part = dt.strftime("%d.%m.%Y")
        time_part = dt.strftime("%H:%M")
    else:
        parts = date_str.strip().split(",")
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
    date_str_full = f"{date_part}, {time_part}"
    meta_date = datetime.strptime(date_str_full, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")
    phone = f"+7 ({__import__('random').randint(900, 999)}) {__import__('random').randint(100, 999)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}"
    if not missing:
        data = bytearray(_tpl.read_bytes())
        try:
            out = patch_from_values(
                data, _tpl,
                date_str=date_str_full, payer=payer or "Иван Иванович И.", recipient=recipient or "Иван Иванович И.",
                phone=phone, bank=bank or "Т-Банк", amount=amount, operation_id=None, keep_metadata=True,
                account_last4=account if account and re.match(r"^\d{4}$", account) else None,
            )
        except ValueError as e:
            return False, None, str(e)
        out_arr = bytearray(out)
        update_creation_date(out_arr, meta_date)
        _change_one_char_in_id(out_arr)
        return True, bytes(out_arr), ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(_BOT_DIR)) as tf:
        out_path = tf.name
    try:
        cmd = [
            os.sys.executable, str(_ADD_GLYPHS_SCRIPT),
            "--replace", "--hybrid-safe", "--target", str(_tpl), "--id-from", str(_tpl),
            "--keep-operation-id", "--payer", payer or "Иван Иванович И.", "--recipient", recipient or "Иван Иванович И.",
            "--bank", bank or "Т-Банк", "--amount", str(amount), "--date", date_part, "--time", time_part, "-o", out_path,
        ]
        if account and re.match(r"^\d{4}$", account):
            cmd.extend(["--account", account])
        proc = subprocess.run(cmd, cwd=str(_BOT_DIR), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, None, (proc.stderr or proc.stdout or "").strip()[:500]
        if Path(out_path).exists():
            return True, Path(out_path).read_bytes(), ""
        return False, None, "PDF не создан"
    except subprocess.TimeoutExpired:
        return False, None, "Таймаут"
    except Exception as e:
        return False, None, str(e)
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def run_sbp_generate_verified(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация чека БЕЗ модификации шрифта (content-only).
    Донор: check(3).pdf с полным алфавитом. operation_id — от донора.
    Цель: пройти проверку целостности.
    """
    _sbp = _BOT_DIR / "база_чеков" / "vtb" / "СБП"
    _donor = _sbp / "check (3).pdf"
    if not _donor.exists():
        return False, None, "Донор check (3).pdf не найден"
    try:
        from receipt_db import get_receipt_chars, _normalize_char
    except ImportError:
        return False, None, "receipt_db недоступен"
    req = {_normalize_char(c) for c in chars_from_text_fields(payer or "", recipient or "", f"+7 (999) 000-00-00")}
    donor_chars = get_receipt_chars(_donor)
    if req and not (req <= donor_chars):
        missing = req - donor_chars
        return False, None, f"В доноре нет букв: {''.join(sorted(missing))}"
    if not date_str or date_str.strip().lower() in ("now", "сейчас"):
        dt = datetime.now()
        date_part = dt.strftime("%d.%m.%Y")
        time_part = dt.strftime("%H:%M")
    else:
        parts = date_str.strip().split(",")
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
    date_str_full = f"{date_part}, {time_part}"
    meta_date = datetime.strptime(date_str_full, "%d.%m.%Y, %H:%M").strftime("D:%Y%m%d%H%M00+03'00'")
    phone = f"+7 ({__import__('random').randint(900, 999)}) {__import__('random').randint(100, 999)}-{__import__('random').randint(10, 99)}-{__import__('random').randint(10, 99)}"
    data = bytearray(_donor.read_bytes())
    try:
        out = patch_from_values(
            data,
            _donor,
            date_str=date_str_full,
            payer=payer or "Иван Иванович И.",
            recipient=recipient or "Иван Иванович И.",
            phone=phone,
            bank=bank or "Т-Банк",
            amount=amount,
            operation_id=None,
            keep_metadata=True,
            account_last4=account if account and re.match(r"^\d{4}$", account) else None,
        )
    except ValueError as e:
        return False, None, str(e)
    out_arr = bytearray(out)
    update_creation_date(out_arr, meta_date)
    _change_one_char_in_id(out_arr)
    return True, bytes(out_arr), ""


def run_sbp_generate(
    payer: str,
    recipient: str,
    amount: int,
    date_str: str,
    bank: str = "Т-Банк",
    account: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, bytes | None, str]:
    """Генерация чека СБП. Сначала шаблон 15-03-26 (целостность), затем check(3), затем add_glyphs."""
    ok, pdf_bytes, err = run_sbp_generate_15_03(payer, recipient, amount, date_str, bank, account)
    if ok and pdf_bytes:
        return True, pdf_bytes, ""
    ok, pdf_bytes, err = run_sbp_generate_verified(payer, recipient, amount, date_str, bank, account)
    if ok and pdf_bytes:
        return True, pdf_bytes, ""
    if not _ADD_GLYPHS_SCRIPT.exists():
        return False, None, err or f"Скрипт не найден: {_ADD_GLYPHS_SCRIPT}"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(_BOT_DIR)) as tf:
        out_path = tf.name
    try:
        if not date_str or date_str.strip().lower() in ("now", "сейчас"):
            dt = datetime.now()
            date_part = dt.strftime("%d.%m.%Y")
            time_part = dt.strftime("%H:%M")
        else:
            # "13.03.2026, 20:04" или "13.03.2026"
            parts = date_str.strip().split(",")
            date_part = parts[0].strip()
            time_part = parts[1].strip() if len(parts) > 1 else datetime.now().strftime("%H:%M")
        # 16-03-26 — актуальный шаблон ID (15-03-26 исчерпан: все 9 decimal-слотов израсходованы)
        # Каждый шаблон даёт ровно 9 уникальных decimal-ID (pos0, incs 1..15 дающих 0-9)
        _template_id = str(_BOT_DIR / "база_чеков" / "vtb" / "СБП" / "16-03-26_00-00.pdf")
        _template_exists = Path(_template_id).exists()
        cmd = [
            os.sys.executable, str(_ADD_GLYPHS_SCRIPT),
            "--replace", "--hybrid-safe", "--auto-base",
            "--payer", payer or "Иван Иванович И.",
            "--recipient", recipient or "Иван Иванович И.",
            "--bank", bank or "Т-Банк",
            "--amount", str(amount),
            "--date", date_part,
            "--time", time_part,
            "-o", out_path,
        ]
        if _template_exists:
            cmd.extend(["--id-from", _template_id])
        if account and re.match(r"^\d{4}$", account):
            cmd.extend(["--account", account])
        if operation_id:
            cmd.extend(["--operation-id", operation_id])
        proc = subprocess.run(cmd, cwd=str(_BOT_DIR), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            return False, None, err or f"Код выхода: {proc.returncode}"
        if not Path(out_path).exists():
            return False, None, "PDF не создан"
        pdf_bytes = Path(out_path).read_bytes()
        return True, pdf_bytes, ""
    except subprocess.TimeoutExpired:
        return False, None, "Таймаут генерации (60 сек)"
    except Exception as e:
        return False, None, str(e)
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def _vtb_full_validate_text(text: str, field_name: str) -> tuple[str | None, str | None]:
    """Валидация текста. Вернуть (None, None) если ок; (err_msg, suggested) при ошибке.
    suggested — вариант с заменой ё→е, ‑→- (если применим)."""
    bad = get_unsupported_chars(text)
    if not bad:
        return None, None
    suggested = suggest_replacement(text)
    return format_unsupported_error(bad, field_name), suggested


def _run_vtb_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч ВТБ и отправить PDF."""
    inp = state["file_path"]
    amount_from = state.get("vtb_amount_from") or state.get("vtb_amount")
    amount_to = state.get("vtb_amount")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    required_chars = chars_from_text_fields(
        state.get("vtb_payer") or "",
        state.get("vtb_recipient") or "",
        state.get("vtb_phone") or "",
        state.get("vtb_bank") or "",
    )
    if required_chars and not receipt_supports_chars(inp, required_chars):
        missing = get_missing_chars_in_receipt(inp, required_chars)
        miss_txt = f"Не хватает букв: «{'» «'.join(sorted(missing))}»" if missing else ""
        send(f"❌ Имя недоступно. В вашем чеке отсутствуют нужные буквы.\n{miss_txt}\nПопробуйте «Сгенерировать» — бот найдёт чек с подходящими символами.")
        return

    try:
        data = bytearray(Path(inp).read_bytes())
        # Для ВТБ сначала используем точное выравнивание суммы по wall из patch_amount_only.
        try:
            data = bytearray(patch_amount_only(data, Path(inp), amount_to))
        except Exception:
            ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
            if not ok_sum or new_data is None:
                send(f"❌ Сумма не найдена.\nПроверь: в чеке должна быть сумма {format_amount_display(amount_from)} ₽. {err_sum or ''}")
                return
            data = bytearray(new_data)
        # Остальные поля (дата, ФИО, телефон, банк) — amount=None, сумму уже поменяли
        out_bytes = patch_from_values(
            data,
            Path(inp),
            date_str=state.get("vtb_date", "now"),
            payer=state.get("vtb_payer"),
            recipient=state.get("vtb_recipient"),
            phone=state.get("vtb_phone"),
            bank=state.get("vtb_bank"),
            amount=None,
            account=None,
        )
        out_name = Path(state["file_name"]).stem + f"_{format_amount_display(state['vtb_amount']).replace(' ', '_')}.pdf"
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]
        am_from = state.get("vtb_amount_from") or state.get("vtb_amount")
        caption = f"✅ Готово: {format_amount_display(am_from)} ₽ → {format_amount_display(state['vtb_amount'])} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": state.get("vtb_amount_from") or state.get("vtb_amount", 0),
            "amount_to": state["vtb_amount"],
            "bank": "vtb",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Тест", "callback_data": "report_test"}],
                    [{"text": "Заявка", "callback_data": "report_zayavka"}],
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except ValueError as e:
        send(f"❌ Ошибка: {e}")
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _vtb_full_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Отправить следующий шаг. Для payer/recipient/phone/bank — с кнопкой «Оставить текущим»."""
    KEEP_PROMPTS = {
        "vtb_payer": ("3️⃣ Плательщик (ФИО):", "vtb_keep_payer"),
        "vtb_recipient": ("4️⃣ Получатель (ФИО):", "vtb_keep_recipient"),
        "vtb_phone": ("5️⃣ Телефон получателя:", "vtb_keep_phone"),
        "vtb_bank": ("6️⃣ Банк получателя:", "vtb_keep_bank"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _gen_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «Сгенерировать»."""
    KEEP_PROMPTS = {
        "gen_payer": ("1️⃣ Плательщик (ФИО):", "gen_keep_payer"),
        "gen_recipient": ("2️⃣ Получатель (ФИО):", "gen_keep_recipient"),
        "gen_phone": ("5️⃣ Телефон получателя:", "gen_keep_phone"),
        "gen_bank": ("6️⃣ Банк получателя:", "gen_keep_bank"),
        "gen_account": ("6️⃣ Номер счёта (4 цифры, напр. 9426):", "gen_keep_account"),
        "gen_operation_id": ("7️⃣ ID операции (B606...):", "gen_keep_opid"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        p, cb = KEEP_PROMPTS[aw]
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить пустым", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _do_gen_fio_check_and_continue(token: str, uid: int, chat_id: int, state: dict, send, next_step) -> None:
    """Проверить ФИО (--check-caps), показать результат, перейти к настройке суммы."""
    payer = state.get("gen_payer") or ""
    recipient = state.get("gen_recipient") or ""
    ok, output = run_check_caps(payer, recipient)
    txt = "📋 Результат проверки ФИО:\n\n" + (output or "(пусто)")
    send(txt[:4000])
    next_step("gen_amount", "📝 Настройка.\n\n1️⃣ Сумма (например 10000):")


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После ввода банка: для СБП — gen_account, для ВТБ на ВТБ — gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Оставить пустым — из шаблона:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")


def _run_sbp_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать чек СБП через add_glyphs_to_13_03.py."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    amount = state.get("gen_amount") or 0
    date_str = state.get("gen_date") or "now"
    ok, pdf_bytes, err = run_sbp_generate(
        payer=state.get("gen_payer") or "Иван Иванович И.",
        recipient=state.get("gen_recipient") or "Иван Иванович И.",
        amount=amount,
        date_str=date_str,
        bank=state.get("gen_bank") or "Т-Банк",
        account=state.get("gen_account"),
        operation_id=state.get("gen_operation_id"),
    )
    if not ok:
        send(f"❌ Ошибка генерации: {err}")
        return
    out_name = f"чек_{format_amount_display(amount).replace(' ', '_')}.pdf"
    del USER_STATE[uid]
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": f"✅ Сгенерировано: {format_amount_display(amount)} ₽"}, files={"document": (out_name, pdf_bytes)})
    USER_STATE[uid] = {"awaiting": "report_choice", "amount_from": 0, "amount_to": amount, "bank": "vtb", "pdf_name": out_name}
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "📋 Отчёт:",
        "reply_markup": json.dumps({
            "inline_keyboard": [
                [{"text": "Тест", "callback_data": "report_test"}],
                [{"text": "Заявка", "callback_data": "report_zayavka"}],
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ],
        }),
    })


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После gen_bank: для vtb_sbp → gen_account, иначе → gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Или «оставить»:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить из чека или ввести свой:")


def _run_gen_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Сгенерировать чек: найти донора, патч, отправить PDF."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    required_chars = chars_from_text_fields(
        state.get("gen_payer") or "",
        state.get("gen_recipient") or "",
        state.get("gen_phone") or "",
        state.get("gen_bank") or "",
    )
    bank_key = state.get("gen_vtb_subtype") or "vtb_sbp"
    donor_path, amount_from = find_donor(required_chars, bank_key)
    if not donor_path:
        missing, scanned, _ = get_bank_report(required_chars, bank_key)
        idx = load_index()
        lines = [
            "❌ Имя недоступно. В базе нет чека с нужными буквами.",
            f"📂 Просмотрено чеков: {scanned}",
            f"🔍 bank_key={bank_key}, в индексе: {list(idx.keys())}",
        ]
        if missing:
            missing_sorted = sorted(missing)
            lines.append(f"❌ Не хватает букв ({len(missing)}): «{'» «'.join(missing_sorted)}»")
        lines.append("Добавьте чеки в «Проверка базы» или замените буквы (ё→е, ‑→-).")
        send("\n".join(lines))
        del USER_STATE[uid]
        return

    amount_to = state["gen_amount"]
    user_from = state.get("gen_amount_from")
    if amount_from is not None:
        amounts_to_try = [amount_from]
    elif user_from is not None:
        amounts_to_try = [user_from] + [a for a in COMMON_AMOUNTS if a != user_from]
    else:
        amounts_to_try = COMMON_AMOUNTS

    try:
        data = bytearray(donor_path.read_bytes())
        try:
            data = bytearray(patch_amount_only(data, donor_path, amount_to))
        except Exception:
            ok_sum, err_sum, new_data = False, None, None
            for am in amounts_to_try:
                ok_sum, err_sum, new_data = pdf_patch_amount(data, am, amount_to, bank="vtb")
                if ok_sum and new_data is not None:
                    break
            if not ok_sum or new_data is None:
                send(f"❌ Сумма не найдена в доноре. {err_sum or ''}")
                del USER_STATE[uid]
                return
            data = bytearray(new_data)
        out_bytes = patch_from_values(
            data,
            donor_path,
            date_str=state.get("gen_date", "now"),
            payer=state.get("gen_payer"),
            recipient=state.get("gen_recipient"),
            phone=state.get("gen_phone"),
            bank=state.get("gen_bank"),
            amount=None,
            operation_id=state.get("gen_operation_id"),
            account=None,
        )
        out_name = donor_path.stem + f"_{format_amount_display(amount_to).replace(' ', '_')}.pdf"
        del USER_STATE[uid]
        caption = f"✅ Сгенерировано: {format_amount_display(amount_to)} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        op_id = state.get("gen_operation_id") or get_operation_id_from_pdf(out_bytes)
        if op_id:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"📋 ID операции (скопируйте при необходимости):\n{op_id}"})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": amount_from or 0,
            "amount_to": amount_to,
            "bank": "vtb",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Тест", "callback_data": "report_test"}],
                    [{"text": "Заявка", "callback_data": "report_zayavka"}],
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except Exception as e:
        send(f"❌ Ошибка: {e}")
        if uid in USER_STATE and state.get("gen_bank_type"):
            del USER_STATE[uid]


def _do_gen_fio_check_and_continue(token: str, uid: int, chat_id: int, state: dict, send, next_step) -> None:
    """Проверка ФИО (run_check_caps) и переход к настройке (gen_amount)."""
    payer = state.get("gen_payer") or ""
    recipient = state.get("gen_recipient") or ""
    send("⏳ Проверяю буквы...")
    ok, output = run_check_caps(payer, recipient)
    send("📋 Результат проверки ФИО:\n\n" + (output or "(пусто)"))
    next_step("gen_amount", "Настройка.\n\n1️⃣ Сумма (например 10000):")


def _gen_next_after_bank(state: dict, next_step) -> None:
    """После банка: для СБП — gen_account, для ВТБ на ВТБ — gen_operation_id."""
    if state.get("gen_vtb_subtype") == "vtb_sbp":
        next_step("gen_account", "6️⃣ Номер счёта (4 цифры, напр. 9426). Или оставить из шаблона:")
    else:
        next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")


def _run_sbp_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Генерация чека СБП через add_glyphs_to_13_03.py."""
    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})
    ok, pdf_bytes, err = run_sbp_generate(
        payer=state.get("gen_payer") or "Иван Иванович И.",
        recipient=state.get("gen_recipient") or "Иван Иванович И.",
        amount=state["gen_amount"],
        date_str=state.get("gen_date", "now"),
        bank=state.get("gen_bank") or "Т-Банк",
        account=state.get("gen_account"),
        operation_id=state.get("gen_operation_id"),
    )
    if not ok or not pdf_bytes:
        send(f"❌ Ошибка генерации: {err or 'PDF не создан'}")
        if uid in USER_STATE:
            del USER_STATE[uid]
        return
    out_name = f"чек_{format_amount_display(state['gen_amount']).replace(' ', '_')}.pdf"
    del USER_STATE[uid]
    caption = f"✅ Сгенерировано: {format_amount_display(state['gen_amount'])} ₽"
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
    USER_STATE[uid] = {
        "awaiting": "report_choice",
        "amount_from": 0,
        "amount_to": state["gen_amount"],
        "bank": "vtb",
        "pdf_name": out_name,
    }
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "📋 Отчёт:",
        "reply_markup": json.dumps({
            "inline_keyboard": [
                [{"text": "Тест", "callback_data": "report_test"}],
                [{"text": "Заявка", "callback_data": "report_zayavka"}],
                [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
            ],
        }),
    })


def _handle_gen_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода режима «Сгенерировать»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _gen_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting in ("gen_type", "gen_subtype", "gen_bank") and state.get("gen_bank_type") is None:
        send("Выберите тип перевода и банк кнопками выше.")
        return

    if awaiting == "gen_amount":
        nums = re.findall(r"\d+", text.strip())
        if not nums:
            send("❌ Введите сумму (например: 50000) или две: с какой на какую (10 1000)")
            return
        if len(nums) == 1:
            state["gen_amount"] = int(nums[0])
            state["gen_amount_from"] = None
        else:
            state["gen_amount_from"] = int(nums[0])
            state["gen_amount"] = int("".join(nums[1:]))
        if state["gen_amount"] <= 0:
            send("❌ Сумма должна быть больше 0.")
            return
        next_step("gen_date", "2️⃣ Дата (дд.мм.гггг чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "gen_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["gen_date"] = "now"
        else:
            state["gen_date"] = text.strip()
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            next_step("gen_phone", "5️⃣ Телефон получателя:")
        else:
            next_step("gen_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "gen_payer":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_payer"] = None
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["gen_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("gen_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["gen_payer"] = t
            next_step("gen_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "gen_recipient":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_recipient"] = None
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["gen_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                if state.get("gen_vtb_subtype") == "vtb_sbp":
                    _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
                else:
                    next_step("gen_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["gen_recipient"] = t
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _do_gen_fio_check_and_continue(token, uid, chat_id, state, send, next_step)
            else:
                next_step("gen_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "gen_operation_id":
        t = text.strip().replace(" ", "").replace("\n", "")
        if not t or t.lower() in ("пропустить", "-", "=", "оставить", "оставить пустым"):
            state["gen_operation_id"] = None
            send("⏳ Генерирую чек...")
            if state.get("gen_vtb_subtype") == "vtb_sbp":
                _run_sbp_generate(token, uid, chat_id, state, tg_req)
            else:
                _run_gen_patch(token, uid, chat_id, state, tg_req)
            return
        if not re.match(r"^[AB]606[\dA-Fa-f]{15,30}$", t):
            send("❌ Формат: B606... или A606... (цифры и A-F, 20-32 символа)")
            return
        state["gen_operation_id"] = t
        send("⏳ Генерирую чек...")
        if state.get("gen_vtb_subtype") == "vtb_sbp":
            _run_sbp_generate(token, uid, chat_id, state, tg_req)
        else:
            _run_gen_patch(token, uid, chat_id, state, tg_req)

    elif awaiting == "gen_phone":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_phone"] = None
            next_step("gen_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["gen_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("gen_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["gen_phone"] = t
            next_step("gen_bank", "6️⃣ Банк получателя:")

    elif awaiting == "gen_bank":
        t = text.strip().lower()
        if t in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_bank"] = None
            _gen_next_after_bank(state, next_step)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["gen_bank"] = suggested
                send(f"✅ Применена замена: {suggested}")
                _gen_next_after_bank(state, next_step)
            else:
                send(err)
                return
        else:
            state["gen_bank"] = t
            _gen_next_after_bank(state, next_step)

    elif awaiting == "gen_account":
        t = text.strip()
        if t.lower() in ("пропустить", "-", "=", "оставить пустым"):
            state["gen_account"] = None
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
            return
        if re.match(r"^\d{4}$", t):
            state["gen_account"] = t
            next_step("gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:")
        else:
            send("❌ Номер счёта — 4 цифры (например 9426)")


# ── Выписка Альфа-Банк ──────────────────────────────────────────────────

def _alfa_stmt_show_blocks(token: str, chat_id: int, state: dict, tg_req) -> None:
    """Show all 3 blocks and editing buttons."""
    from alfa_statement_service import format_block1, format_block2, format_block3
    changes = state.get("changes", {})
    txt = (
        "🏦 Выписка Альфа-Банк\n\n"
        + format_block1(changes) + "\n\n"
        + format_block2(changes) + "\n\n"
        + format_block3(changes)
    )
    kb = [
        [{"text": "💳 Блок 1 — Операции", "callback_data": "alfa_stmt_b1"}],
        [{"text": "📊 Блок 2 — Сводка", "callback_data": "alfa_stmt_b2"}],
        [{"text": "👤 Блок 3 — Реквизиты", "callback_data": "alfa_stmt_b3"}],
        [{"text": "📎 Заполнить из чека", "callback_data": "alfa_stmt_from_check"}],
        [{"text": "✅ Создать PDF", "callback_data": "alfa_stmt_generate"},
         {"text": "⬅️ Назад", "callback_data": "main_back"}],
    ]
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt,
           "reply_markup": json.dumps({"inline_keyboard": kb})})


def _alfa_stmt_show_block_edit(token: str, chat_id: int, msg_id: int, block_num: int, tg_req) -> None:
    """Show fields for a specific block, ready to edit."""
    from alfa_statement_service import (
        BLOCK1_DEFAULTS, BLOCK2_DEFAULTS, BLOCK3_DEFAULTS, BLOCK_LABELS)
    if block_num == 1:
        fields = BLOCK1_DEFAULTS
        title = "💳 Блок 1 — Операции"
        hint = (
            "Введите замены в формате:\n"
            "ключ=значение\n\n"
            "Ключи: код_расход, телефон, тел_конец, "
            "сумма_расход, код_приход, получатель, сумма_приход\n\n"
            "Пример:\nсумма_расход=15 000,00\nсумма_приход=15 000,00\nтелефон=+7 (999) 123-45-"
        )
    elif block_num == 2:
        fields = BLOCK2_DEFAULTS
        title = "📊 Блок 2 — Сводка"
        hint = (
            "Введите суммы (авто-расчёт):\n"
            "входящий=СУММА\nпоступления=СУММА\nрасходы=СУММА\n\n"
            "Исходящий/лимит/баланс считаются автоматически:\n"
            "исходящий = входящий + поступления - расходы\n\n"
            "Пример:\nвходящий=500\nпоступления=15000\nрасходы=10000\n\n"
            "Или вручную:\nисходящий=5500\nлимит=5500\nбаланс=5500"
        )
    else:
        fields = BLOCK3_DEFAULTS
        title = "👤 Блок 3 — Реквизиты"
        hint = (
            "Введите замены:\n"
            "Ключи: счёт, имя, отчество, индекс, город, дом\n\n"
            "Пример:\nимя=Иванов Иван\nотчество=Иванович\nиндекс=123456\nгород=Москва"
        )
    lines = [f"{title}\n\nТекущие значения:"]
    for k, v in fields.items():
        label = BLOCK_LABELS.get(k, k)
        lines.append(f"  {label}: {v}")
    lines.append(f"\n{hint}")
    kb = [[{"text": "⬅️ К блокам", "callback_data": "alfa_stmt_blocks"}]]
    tg_req(token, "editMessageText", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": "\n".join(lines),
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


_BLOCK2_KEY_MAP = {
    "входящий": "входящий_остаток", "входящий_остаток": "входящий_остаток",
    "поступления": "поступления",
    "расходы": "расходы",
    "исходящий": "исходящий_остаток", "исходящий_остаток": "исходящий_остаток",
    "лимит": "платежный_лимит", "платежный_лимит": "платежный_лимит",
    "баланс": "текущий_баланс", "текущий_баланс": "текущий_баланс",
}
_BLOCK3_KEY_MAP = {
    "счёт": "номер_счета", "счет": "номер_счета", "номер_счета": "номер_счета",
    "имя": "клиент_имя", "клиент_имя": "клиент_имя", "фио": "клиент_имя",
    "отчество": "клиент_отчество", "клиент_отчество": "клиент_отчество",
    "индекс": "индекс",
    "город": "город",
    "дом": "дом_кв", "дом_кв": "дом_кв", "квартира": "дом_кв",
}
_BLOCK1_KEY_MAP = {
    "код_расход": "код_операции_расход", "код_операции_расход": "код_операции_расход",
    "телефон": "телефон",
    "тел_конец": "телефон_окончание", "телефон_окончание": "телефон_окончание",
    "сумма_расход": "сумма_расход", "расход": "сумма_расход",
    "код_приход": "код_операции_приход", "код_операции_приход": "код_операции_приход",
    "получатель": "получатель_сокр", "получатель_сокр": "получатель_сокр",
    "сумма_приход": "сумма_приход", "приход": "сумма_приход",
}


def _handle_alfa_stmt_text(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Handle text input in alfa_stmt editing mode."""
    state = USER_STATE.get(uid)
    if not state:
        return
    block = state.get("editing_block")
    if not block:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Выберите блок для редактирования."})
        return

    from alfa_statement_service import (
        validate_text, parse_amount, recalc_balances, _fmt_amount,
        BLOCK2_DEFAULTS, format_block1, format_block2, format_block3,
    )

    changes = state.setdefault("changes", {})
    key_map = {1: _BLOCK1_KEY_MAP, 2: _BLOCK2_KEY_MAP, 3: _BLOCK3_KEY_MAP}[block]
    applied = []
    warnings = []

    for line in text.strip().split("\n"):
        line = line.strip()
        if "=" not in line:
            continue
        raw_key, _, raw_val = line.partition("=")
        raw_key = raw_key.strip().lower()
        raw_val = raw_val.strip()
        field_key = key_map.get(raw_key)
        if not field_key:
            warnings.append(f"Неизвестный ключ: {raw_key}")
            continue

        missing = validate_text(raw_val)
        if missing:
            warnings.append(f"⚠️ Недоступные символы для «{raw_key}»: {''.join(missing)}")
            continue

        if field_key in ("входящий_остаток", "поступления", "расходы",
                         "исходящий_остаток", "платежный_лимит", "текущий_баланс",
                         "сумма_расход", "сумма_приход"):
            parsed = parse_amount(raw_val)
            if parsed is not None:
                raw_val = _fmt_amount(parsed)

        changes[field_key] = raw_val
        applied.append(f"✅ {raw_key} = {raw_val}")

    # Auto-recalculate Block 2 if we have the three input values
    if block == 2:
        вх = parse_amount(changes.get("входящий_остаток", BLOCK2_DEFAULTS["входящий_остаток"]))
        пост = parse_amount(changes.get("поступления", BLOCK2_DEFAULTS["поступления"]))
        расх = parse_amount(changes.get("расходы", BLOCK2_DEFAULTS["расходы"]))
        has_manual = any(k in changes for k in ("исходящий_остаток", "платежный_лимит", "текущий_баланс"))
        if вх is not None and пост is not None and расх is not None and not has_manual:
            auto = recalc_balances(вх, пост, расх)
            changes.update(auto)
            applied.append(f"📊 Авто: исходящий={auto['исходящий_остаток']}, лимит={auto['платежный_лимит']}, баланс={auto['текущий_баланс']}")

    msg_parts = []
    if applied:
        msg_parts.append("\n".join(applied))
    if warnings:
        msg_parts.append("\n".join(warnings))
    if not applied and not warnings:
        msg_parts.append("❌ Не удалось распознать. Формат: ключ=значение")

    msg_parts.append("\nВведите ещё или вернитесь к блокам.")
    kb = [[{"text": "⬅️ К блокам", "callback_data": "alfa_stmt_blocks"}]]
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(msg_parts),
           "reply_markup": json.dumps({"inline_keyboard": kb})})


def _handle_alfa_stmt_upload(token: str, uid: int, chat_id: int, doc: dict, fname: str, tg_req) -> None:
    """Handle check PDF upload for auto-fill."""
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Извлекаю данные из чека..."})
    try:
        fp = tg_get_file_path(token, doc["file_id"])
        pdf_data = tg_get_file(token, fp)
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка загрузки: {e}"})
        return
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(pdf_data)
        check_path = tf.name
    try:
        from alfa_statement_service import (
            extract_from_check, validate_text, format_block1, format_block2, format_block3,
        )
        extracted = extract_from_check(Path(check_path))
    except Exception as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка извлечения: {e}"})
        try:
            os.unlink(check_path)
        except OSError:
            pass
        return
    try:
        os.unlink(check_path)
    except OSError:
        pass

    if not extracted:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⚠️ Не удалось извлечь данные из чека. Заполните вручную."})
        return

    state = USER_STATE.get(uid, {})
    changes = state.setdefault("changes", {})

    warn_lines = []
    for key, val in extracted.items():
        missing = validate_text(val)
        if missing:
            warn_lines.append(f"⚠️ «{key}»: недоступные символы {''.join(missing)}")
        else:
            changes[key] = val

    parts = ["📎 Извлечено из чека:\n"]
    from alfa_statement_service import BLOCK_LABELS
    for k, v in extracted.items():
        label = BLOCK_LABELS.get(k, k)
        parts.append(f"  {label}: {v}")
    if warn_lines:
        parts.append("\n" + "\n".join(warn_lines))
    parts.append("\nПросмотрите и отредактируйте блоки.")

    state["mode"] = "alfa_stmt"
    state.pop("editing_block", None)
    kb = [
        [{"text": "💳 Блок 1", "callback_data": "alfa_stmt_b1"},
         {"text": "📊 Блок 2", "callback_data": "alfa_stmt_b2"},
         {"text": "👤 Блок 3", "callback_data": "alfa_stmt_b3"}],
        [{"text": "✅ Создать PDF", "callback_data": "alfa_stmt_generate"},
         {"text": "⬅️ Назад", "callback_data": "main_back"}],
    ]
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(parts),
           "reply_markup": json.dumps({"inline_keyboard": kb})})


def _handle_alfa_stmt_callback(token: str, uid: int, q: dict, tg_req) -> None:
    """Handle callback queries for alfa_stmt flow."""
    data = q["data"]
    chat_id = q["message"]["chat"]["id"]
    msg_id = q["message"]["message_id"]

    if data == "alfa_stmt_start":
        USER_STATE[uid] = {"mode": "alfa_stmt", "changes": {}}
        _alfa_stmt_show_blocks(token, chat_id, USER_STATE[uid], tg_req)
        return

    if data == "alfa_stmt_blocks":
        state = USER_STATE.get(uid)
        if not state or not state.get("mode", "").startswith("alfa_stmt"):
            USER_STATE[uid] = {"mode": "alfa_stmt", "changes": {}}
        USER_STATE[uid].pop("editing_block", None)
        _alfa_stmt_show_blocks(token, chat_id, USER_STATE[uid], tg_req)
        return

    if data in ("alfa_stmt_b1", "alfa_stmt_b2", "alfa_stmt_b3"):
        block_num = int(data[-1])
        state = USER_STATE.setdefault(uid, {"mode": "alfa_stmt", "changes": {}})
        state["mode"] = "alfa_stmt"
        state["editing_block"] = block_num
        _alfa_stmt_show_block_edit(token, chat_id, msg_id, block_num, tg_req)
        return

    if data == "alfa_stmt_from_check":
        state = USER_STATE.setdefault(uid, {"mode": "alfa_stmt", "changes": {}})
        state["mode"] = "alfa_stmt_check"
        tg_req(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "📎 Отправьте PDF-файл чека.\n\nДанные будут извлечены и подставлены в блоки.",
            "reply_markup": json.dumps({"inline_keyboard": [
                [{"text": "⬅️ К блокам", "callback_data": "alfa_stmt_blocks"}]
            ]}),
        })
        return

    if data == "alfa_stmt_generate":
        state = USER_STATE.get(uid)
        if not state:
            tg_req(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
            return
        tg_req(token, "editMessageText", {
            "chat_id": chat_id, "message_id": msg_id,
            "text": "⏳ Генерирую выписку...",
        })
        try:
            from alfa_statement_service import patch_alfa_statement
            changes = state.get("changes", {})
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                out_path = tf.name
            ok, err = patch_alfa_statement(changes, Path(out_path))
            if not ok:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                del USER_STATE[uid]
                return
            with open(out_path, "rb") as f:
                pdf_bytes = f.read()
            try:
                os.unlink(out_path)
            except OSError:
                pass
            del USER_STATE[uid]
            summary = []
            for k, v in changes.items():
                from alfa_statement_service import BLOCK_LABELS
                label = BLOCK_LABELS.get(k, k)
                summary.append(f"  {label}: {v}")
            caption = "✅ Выписка Альфа-Банк готова"
            if summary:
                caption += "\n\nИзменения:\n" + "\n".join(summary[:10])
                if len(summary) > 10:
                    caption += f"\n  ... и ещё {len(summary) - 10}"
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption},
                   files={"document": ("выписка_альфа.pdf", pdf_bytes)})
        except Exception as e:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
            if uid in USER_STATE:
                del USER_STATE[uid]
        return


def _handle_stmt_text(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Обработка текста в режиме выписки."""
    state = USER_STATE[uid]
    mode = state.get("mode", "")
    step = state.get("step", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    if mode == "statement_edit":
        if step == "amounts":
            pairs = parse_amount_pairs(text)
            if not pairs:
                send("❌ Неверный формат. Введите: 10 10000 или 10 5000 50 1000")
                return
            try:
                from vyписка_service import calculate_balance_and_expenses
            except ImportError:
                send("❌ Модуль выписки недоступен")
                return
            amounts = [(p[0], p[1]) for p in pairs]
            USER_STATE[uid]["replacements"] = {"amounts": amounts}
            USER_STATE[uid]["step"] = "confirm"
            trans = list(state.get("transactions", []))
            amount_map = {int(p[0]): p[1] for p in amounts}
            trans_replaced = [float(amount_map.get(int(t), t)) for t in trans if t > 0]
            balance_end, expenses = calculate_balance_and_expenses(trans_replaced, 55242.65)
            kb = json.dumps({
                "inline_keyboard": [
                    [{"text": "⏭ Пропустить", "callback_data": "stmt_skip"}, {"text": "➡️ Далее", "callback_data": "stmt_next"}],
                    [{"text": "➕ Свои замены", "callback_data": "stmt_custom"}],
                ],
            })
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Замены: {amounts}\n\n📊 Расходы: {expenses:.2f} ₽\n📊 Баланс на конец: {balance_end:.2f} ₽\n\nПропустить / Далее или + свои замены", "reply_markup": kb})
            return
        if step == "custom":
            parsed = parse_custom_replacement(text)
            if not parsed:
                send("❌ Формат: поле=значение (ФИО=..., баланс_начало=..., телефон=...)")
                return
            key, value = parsed
            repl = USER_STATE[uid].setdefault("replacements", {})
            if key in ("fio", "фио"):
                try:
                    from vyписка_service import get_missing_chars
                    fp = Path(USER_STATE[uid]["file_path"])
                    missing = get_missing_chars(fp, value)
                    if missing:
                        kb = json.dumps({"inline_keyboard": [[{"text": "🔄 Повторить", "callback_data": "stmt_retry_fio"}, {"text": "⏭ Без замены ФИО", "callback_data": "stmt_skip_fio"}]]})
                        send(f"⚠️ Недоступные символы: {''.join(missing)}\nПовторите или пропустите.")
                        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Выберите:", "reply_markup": kb})
                        USER_STATE[uid]["pending_fio"] = value
                        return
                except ImportError:
                    pass
                repl["fio"] = value
            elif key in ("баланс_начало", "balance_start", "balance"):
                try:
                    repl["balance_start"] = float(value.replace(",", "."))
                except ValueError:
                    send("❌ Введите число для баланса.")
                    return
            elif key in ("телефон", "phone"):
                repl["phone"] = value
            elif key in ("номер_заявки", "application_id"):
                repl["application_id"] = value
            send(f"✅ Добавлено: {key}={value}\nВведите ещё или нажмите Далее.")

    elif mode == "statement_from_receipt" and step == "balance":
        try:
            balance_start = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            send("❌ Введите число (баланс на начало).")
            return
        try:
            from vyписка_service import BASE_STATEMENT, BASE_AMOUNT, BASE_OLD_FIO, patch_statement, calculate_balance_and_expenses
            amount = USER_STATE[uid]["amount"]
            balance_end, expenses = calculate_balance_and_expenses([float(amount)], balance_start)
            repl = {"amounts": [(BASE_AMOUNT, amount)], "balance_end": balance_end, "expenses": expenses, "fio": USER_STATE[uid].get("generated_fio", "Иванов Иван Иванович"), "old_fio": BASE_OLD_FIO}
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
                out_path = out_fp.name
            ok, err = patch_statement(BASE_STATEMENT, Path(out_path), repl)
            try:
                os.unlink(USER_STATE[uid]["file_path"])
            except OSError:
                pass
            del USER_STATE[uid]
            if not ok:
                send(f"❌ Ошибка: {err}")
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                return
            with open(out_path, "rb") as f:
                pdf_bytes = f.read()
            try:
                os.unlink(out_path)
            except OSError:
                pass
            tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": f"✅ Выписка готова: {amount} ₽"}, files={"document": (f"выписка_{amount}.pdf", pdf_bytes)})
        except ImportError as e:
            send(f"❌ Модуль выписки недоступен: {e}")
            if uid in USER_STATE:
                del USER_STATE[uid]


def _handle_vtb_full_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для ВТБ «Все поля»."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str):
        _vtb_full_send_next(state, aw, prompt, chat_id, token, tg_req)

    if awaiting == "vtb_amount":
        parsed = parse_amounts(text)
        if not parsed:
            send("❌ Введите две суммы: с какой на какую (например: 10 1000 или 50 50000)")
            return
        amount_from, amount_to = parsed
        if amount_from <= 0 or amount_to <= 0:
            send("❌ Суммы должны быть больше 0.")
            return
        state["vtb_amount_from"] = amount_from
        state["vtb_amount"] = amount_to
        next_step("vtb_date", "2️⃣ Дата (дд.мм.гггг, чч:мм). Enter или «сейчас» — текущая дата")

    elif awaiting == "vtb_date":
        text_stripped = text.strip().lower()
        if not text_stripped or text_stripped in ("сейчас", "enter", ""):
            state["vtb_date"] = "now"
        else:
            state["vtb_date"] = text.strip()
        next_step("vtb_payer", "3️⃣ Плательщик (ФИО):")

    elif awaiting == "vtb_payer":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_payer"] = None
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Плательщик")
        if err:
            if suggested:
                state["vtb_payer"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")
            else:
                send(err)
                return
        else:
            state["vtb_payer"] = t
            next_step("vtb_recipient", "4️⃣ Получатель (ФИО):")

    elif awaiting == "vtb_recipient":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_recipient"] = None
            next_step("vtb_phone", "5️⃣ Телефон получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Получатель")
        if err:
            if suggested:
                state["vtb_recipient"] = suggested
                send(f"✅ Применена замена (ё→е): {suggested}")
                next_step("vtb_phone", "5️⃣ Телефон получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_recipient"] = t
            next_step("vtb_phone", "5️⃣ Телефон получателя:")

    elif awaiting == "vtb_phone":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_phone"] = None
            next_step("vtb_bank", "6️⃣ Банк получателя:")
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Телефон")
        if err:
            if suggested:
                state["vtb_phone"] = suggested
                send(f"✅ Применена замена: {suggested}")
                next_step("vtb_bank", "6️⃣ Банк получателя:")
            else:
                send(err)
                return
        else:
            state["vtb_phone"] = t
            next_step("vtb_bank", "6️⃣ Банк получателя:")

    elif awaiting == "vtb_bank":
        t = text.strip().lower()
        if t in ("оставить", "текущим", "оставить текущим", "пропустить", "-", "="):
            state["vtb_bank"] = None
            send("⏳ Обрабатываю чек...")
            _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
            return
        t = text.strip()
        err, suggested = _vtb_full_validate_text(t, "Банк")
        if err:
            if suggested:
                state["vtb_bank"] = suggested
                send(f"✅ Применена замена: {suggested}\n⏳ Обрабатываю чек...")
                _run_vtb_full_patch(token, uid, chat_id, state, tg_req)
                return
            send(err)
            return
        state["vtb_bank"] = t
        send("⏳ Обрабатываю чек...")
        _run_vtb_full_patch(token, uid, chat_id, state, tg_req)


def _alfa_tg_send_next(state: dict, aw: str, prompt: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «Альфа Трансгран». Поля с кнопкой «Оставить текущим»."""
    KEEP_PROMPTS = {
        "at_amount": ("1️⃣ Сумма перевода (сейчас: {amount}):\nВведите новую сумму, напр. 3 036 RUR", "at_keep_amount"),
        "at_commission": ("2️⃣ Комиссия (сейчас: {commission}):\nВведите новую, напр. 50 RUR", "at_keep_commission"),
        "at_rate": ("3️⃣ Курс конвертации (сейчас: {rate}):\nВведите новый, напр. 1 RUR = 0.1130 TJS", "at_keep_rate"),
        "at_credited": ("4️⃣ Сумма зачисления (сейчас: {credited}):\nВведите новую, напр. 343,06 TJS", "at_keep_credited"),
        "at_phone": ("5️⃣ Телефон (сейчас: {phone}):", "at_keep_phone"),
        "at_name": ("6️⃣ Получатель (сейчас: {name}):", "at_keep_name"),
        "at_operation_id": ("7️⃣ Номер операции (сейчас: {operation_id}):", "at_keep_opid"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        tmpl, cb = KEEP_PROMPTS[aw]
        fields = state.get("at_fields", {})
        p = tmpl.format(**{k: fields.get(k, '—') for k in ['amount', 'commission', 'rate', 'credited', 'phone', 'name', 'operation_id']})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })
    else:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": prompt})


def _run_alfa_transgran_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч трансграничного чека Альфа-Банка и отправить PDF."""
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    try:
        pdf_data = Path(inp).read_bytes()
        fields = state.get("at_fields", {})

        kwargs = {}
        for key, label in [
            ("at_amount", "amount"),
            ("at_commission", "commission"),
            ("at_rate", "rate"),
            ("at_credited", "credited"),
            ("at_phone", "phone"),
            ("at_name", "name"),
            ("at_operation_id", "operation_id"),
        ]:
            new_val = state.get(key)
            if new_val:
                old_val = fields.get(label, "")
                if old_val:
                    kwargs[label] = f"{old_val}={new_val}"

        if not kwargs:
            send("❌ Нет замен — все поля оставлены текущими.")
            return

        ok, err, new_data = patch_transgran(pdf_data, **kwargs)
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]

        if not ok or new_data is None:
            send(f"❌ Ошибка: {err}")
            return

        out_name = Path(state["file_name"]).stem + "_transgran.pdf"
        changes = [f"• {k}" for k in kwargs]
        caption = f"✅ Трансгран готов\n" + "\n".join(changes)
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, new_data)})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": 0,
            "amount_to": 0,
            "bank": "alfa_transgran",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _handle_alfa_sbp_input(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Пошаговый ввод полей Альфа СБП (все поля)."""
    state = USER_STATE[uid]
    step = state.get("alfa_sbp_step", 0)
    fields = state.setdefault("alfa_sbp_fields", {})
    field_list = state.get("_alfa_sbp_field_list", [
        ("amount", "💰 Сумма перевода (число, например: 5000)"),
        ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
        ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
        ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
        ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
        ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
    ])

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    if step >= len(field_list):
        return

    field_key, _ = field_list[step]
    t = text.strip()

    if t != "-":
        if field_key == "amount":
            nums = re.findall(r"\d+", t)
            if not nums:
                send("❌ Введите число (например: 5000)")
                return
            fields["amount"] = int("".join(nums))
        elif field_key == "account":
            digits = re.findall(r"\d+", t)
            last4 = "".join(digits)
            if len(last4) < 4:
                send("❌ Нужно ровно 4 цифры (например: 1234)")
                return
            last4 = last4[-4:]
            fields["account_last4"] = last4
        else:
            fields[field_key] = t

    step += 1
    state["alfa_sbp_step"] = step

    if step < len(field_list):
        _, prompt_next = field_list[step]
        send(f"✅ Принято.\n\nШаг {step + 1}/{len(field_list)}: {prompt_next}\nОтправьте - чтобы пропустить.")
    else:
        summary_lines = []
        for fk, label in field_list:
            val = fields.get(fk) or fields.get("account_last4" if fk == "account" else fk)
            summary_lines.append(f"  {label.split('(')[0].strip()}: {val or '(без изменений)'}")
        summary = "\n".join(summary_lines)
        send(f"📋 Параметры:\n{summary}\n\n⏳ Генерирую PDF...")
        _run_alfa_sbp_full_patch(token, uid, chat_id, state, tg_req)


def _run_alfa_sbp_full_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Применяет все замены к Альфа СБП чеку (CID + zero-delta для счёта)."""
    import shutil

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    fields = state.get("alfa_sbp_fields", {})
    inp_path = Path(state["file_path"])

    try:
        data = inp_path.read_bytes()

        uni_to_cid = {}
        cid_to_uni = {}
        for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
            raw = data[m.end(): m.end() + int(m.group(2))]
            try:
                dec = zlib.decompress(raw)
            except zlib.error:
                continue
            if b'beginbfchar' in dec:
                for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                    cid = int(mm.group(1), 16)
                    uni = int(mm.group(2), 16)
                    cid_to_uni[cid] = chr(uni)
                    uni_to_cid[chr(uni)] = mm.group(1).decode().upper().zfill(4)
                break
            if b'beginbfrange' in dec:
                for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                    s = int(mm.group(1), 16)
                    e = int(mm.group(2), 16)
                    u = int(mm.group(3), 16)
                    for i in range(e - s + 1):
                        cid_to_uni[s + i] = chr(u + i)
                        uni_to_cid[chr(u + i)] = f'{s + i:04X}'
                break

        current_texts = []
        for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
            sl = int(m.group(2))
            ss = m.end()
            try:
                dec = zlib.decompress(data[ss:ss + sl])
            except zlib.error:
                continue
            if b'BT' not in dec:
                continue
            for tj in re.finditer(rb'<([0-9A-Fa-f]+)>\s*Tj', dec):
                hexstr = tj.group(1).decode()
                txt = ''
                for i in range(0, len(hexstr), 4):
                    cid = int(hexstr[i:i + 4], 16)
                    txt += cid_to_uni.get(cid, '?')
                current_texts.append(txt)
            break

        def find_text(pattern):
            for t in current_texts:
                if re.search(pattern, t.replace('\xa0', ' ')):
                    return t
            return None

        current_amount_text = find_text(r'\d+\s*RUR')
        current_datetime_text = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}')
        current_date_formed = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+мск')
        current_recipient = None
        current_phone = None
        current_bank = None
        current_account = None

        label_order = [t.replace('\xa0', ' ').strip() for t in current_texts]
        for i, tc in enumerate(label_order):
            if tc == 'Получатель' and i + 1 < len(label_order):
                current_recipient = current_texts[i + 1]
            elif 'телефона получателя' in tc and i + 1 < len(label_order):
                current_phone = current_texts[i + 1]
            elif tc == 'Банк получателя' and i + 1 < len(label_order):
                current_bank = current_texts[i + 1]
            elif 'Счёт списания' in tc and i + 1 < len(label_order):
                current_account = current_texts[i + 1]

        replacements = []

        if "amount" in fields and current_amount_text:
            old_amt = current_amount_text.replace('\xa0', ' ').strip()
            new_amt_num = fields["amount"]
            new_amt_str = f"{new_amt_num:,}".replace(",", "\xa0") + "\xa0RUR\xa0"
            replacements.append((old_amt, new_amt_str))

        if "date_time" in fields:
            new_dt = fields["date_time"]
            if current_datetime_text:
                old_dt = current_datetime_text.replace('\xa0', ' ').strip()
                new_dt_clean = new_dt.replace(' ', '\xa0')
                if not new_dt_clean.endswith('\xa0'):
                    new_dt_clean += '\xa0'
                replacements.append((old_dt, new_dt_clean))
            if current_date_formed:
                old_df = current_date_formed.replace('\xa0', ' ').strip()
                dt_parts = new_dt.split()
                if len(dt_parts) >= 2:
                    formed = dt_parts[0] + '\xa0' + dt_parts[1][:5] + '\xa0мск'
                    replacements.append((old_df, formed))

        if "recipient" in fields and current_recipient:
            old_r = current_recipient.replace('\xa0', ' ').strip()
            new_r = fields["recipient"].replace(' ', '\xa0')
            if old_r.endswith(' '):
                new_r += '\xa0'
            replacements.append((old_r, new_r))

        if "phone" in fields and current_phone:
            old_p = current_phone.replace('\xa0', ' ').strip()
            new_p = fields["phone"].replace(' ', '\xa0')
            replacements.append((old_p, new_p))

        if "bank" in fields and current_bank:
            old_b = current_bank.replace('\xa0', ' ').strip()
            new_b = fields["bank"].replace(' ', '\xa0')
            replacements.append((old_b, new_b))

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
            out_path = out_fp.name

        applied_text = False
        if replacements:
            try:
                from cid_patch_amount import patch_replacements
                text_reps = [(old.replace('\xa0', ' '), new.replace('\xa0', ' '))
                             for old, new in replacements if old.replace('\xa0', ' ') != new.replace('\xa0', ' ')]
                if text_reps:
                    applied_text = patch_replacements(inp_path, Path(out_path), text_reps)
            except Exception as e:
                send(f"⚠️ Ошибка текстовых замен: {e}")

        if not applied_text:
            shutil.copy2(str(inp_path), out_path)

        if "account_last4" in fields:
            try:
                from patch_account_last4 import patch_account_last4 as do_patch_account

                acct_20 = None
                if current_account:
                    digits_found = re.findall(r'\d+', current_account.replace('\xa0', ''))
                    full = ''.join(digits_found)
                    if len(full) == 20:
                        acct_20 = full

                if not acct_20:
                    acct_20 = "40817810980480002476"

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name

                ok = do_patch_account(
                    input_pdf=out_path,
                    output_pdf=tmp_path,
                    old_account=acct_20,
                    new_last4=fields["account_last4"],
                )
                if ok:
                    shutil.move(tmp_path, out_path)
                else:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    send("⚠️ Не удалось заменить счёт (возможно, номер не найден в PDF)")
            except Exception as e:
                send(f"⚠️ Ошибка замены счёта: {e}")

        try:
            os.unlink(state["file_path"])
        except OSError:
            pass
        del USER_STATE[uid]

        out_name = Path(state.get("file_name", "чек.pdf")).stem + "_patched.pdf"
        with open(out_path, "rb") as f:
            out_bytes = f.read()

        caption_parts = []
        if "amount" in fields:
            caption_parts.append(f"Сумма: {fields['amount']} RUR")
        if "account_last4" in fields:
            caption_parts.append(f"Счёт: ****{fields['account_last4']}")
        if "recipient" in fields:
            caption_parts.append(f"Получатель: {fields['recipient']}")
        caption = "✅ Готово! " + ", ".join(caption_parts) if caption_parts else "✅ Готово!"

        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})

        try:
            os.unlink(out_path)
        except OSError:
            pass

        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": 0,
            "amount_to": fields.get("amount", 0),
            "bank": "alfa_sbp_full",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        send(f"❌ Ошибка: {e}\n\nПопробуйте снова или отправьте чек заново.")
        try:
            os.unlink(state.get("file_path", ""))
        except OSError:
            pass
        if uid in USER_STATE:
            del USER_STATE[uid]


def _handle_alfa_transgran_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для Альфа Трансгран."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    def next_step(aw: str, prompt: str = ""):
        _alfa_tg_send_next(state, aw, prompt, chat_id, token, tg_req)

    skip_words = ("оставить", "текущим", "оставить текущим", "пропустить", "-", "=")

    FLOW = [
        ("at_amount",       "at_commission"),
        ("at_commission",   "at_rate"),
        ("at_rate",         "at_credited"),
        ("at_credited",     "at_phone"),
        ("at_phone",        "at_name"),
        ("at_name",         "at_operation_id"),
        ("at_operation_id", None),
    ]

    for step_aw, next_aw in FLOW:
        if awaiting != step_aw:
            continue
        t = text.strip()
        if t.lower() in skip_words:
            state[step_aw] = None
        else:
            state[step_aw] = t
        if next_aw:
            next_step(next_aw)
        else:
            send("⏳ Обрабатываю чек...")
            _run_alfa_transgran_patch(token, uid, chat_id, state, tg_req)
        return


def _vtb_tg_send_next(state: dict, aw: str, chat_id: int, token: str, tg_req) -> None:
    """Следующий шаг режима «ВТБ Трансгран». Поля с кнопкой «Оставить текущим»."""
    fields = state.get("vt_fields", {})
    KEEP_PROMPTS = {
        "vt_amount": ("1️⃣ Сумма операции (сейчас: {amount}):\nВведите новую сумму числом (напр. 10000)", "vt_keep_amount"),
        "vt_phone": ("2️⃣ Телефон получателя (сейчас: {phone}):\nВведите новый номер", "vt_keep_phone"),
        "vt_date": ("3️⃣ Дата и время (сейчас: {date}):\nВведите новые, напр. 18.03.2026, 02:44", "vt_keep_date"),
    }
    state["awaiting"] = aw
    if aw in KEEP_PROMPTS:
        tmpl, cb = KEEP_PROMPTS[aw]
        p = tmpl.format(**{k: fields.get(k, '—') for k in ['amount', 'phone', 'date']})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": p,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📌 Оставить текущим", "callback_data": cb}]]}),
        })


def _run_vtb_transgran_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч трансграничного чека ВТБ и отправить PDF."""
    inp = state["file_path"]

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    try:
        pdf_data = Path(inp).read_bytes()
        fields = state.get("vt_fields", {})

        kwargs: dict = {}
        new_amount = state.get("vt_amount")
        new_phone = state.get("vt_phone")
        new_date = state.get("vt_date")

        if new_amount:
            try:
                kwargs["amount"] = int(new_amount.replace(" ", "").replace("₽", "").strip())
            except ValueError:
                send(f"❌ Неверный формат суммы: {new_amount}")
                return

        if new_phone:
            kwargs["phone"] = new_phone.strip()

        if new_date:
            kwargs["date"] = new_date.strip()

        if not kwargs:
            send("❌ Нет замен — все поля оставлены текущими.")
            return

        if "amount" not in kwargs:
            old_amt = fields.get("amount", "")
            m = re.search(r'([\d\s]+)', old_amt.replace('\xa0', ' '))
            if m:
                kwargs["amount"] = int(m.group(1).replace(" ", ""))

        ok, info, new_data = patch_vtb_transgran(pdf_data, **kwargs)
        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]

        if not ok or new_data is None:
            send(f"❌ Ошибка: {info}")
            return

        out_name = Path(state["file_name"]).stem + "_transgran.pdf"

        rate_str = fields.get("rate", "")
        parsed = parse_rate(rate_str) if rate_str else None
        summary_lines = []
        if "amount" in kwargs:
            summary_lines.append(f"• Сумма: {format_amount_rub(kwargs['amount'])}")
            if parsed:
                from decimal import Decimal
                rate_val, currency = parsed
                credited_val = Decimal(kwargs["amount"]) * rate_val
                summary_lines.append(f"• Зачисление: {format_credited(credited_val, currency)} (авто)")
        if new_phone:
            summary_lines.append(f"• Телефон: {new_phone}")
        if new_date:
            summary_lines.append(f"• Дата: {new_date}")

        caption = "✅ ВТБ Трансгран готов\n" + "\n".join(summary_lines)
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, new_data)})
        USER_STATE[uid] = {
            "awaiting": "report_choice",
            "amount_from": 0,
            "amount_to": kwargs.get("amount", 0),
            "bank": "vtb_transgran",
            "pdf_name": out_name,
        }
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📋 Отчёт:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                ],
            }),
        })
    except Exception as e:
        send(f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново.")


def _handle_vtb_transgran_input(token: str, uid: int, chat_id: int, text: str, msg: dict, tg_req) -> None:
    """Обработка пошагового ввода для ВТБ Трансгран."""
    state = USER_STATE[uid]
    awaiting = state.get("awaiting", "")

    def send(txt: str):
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": txt})

    skip_words = ("оставить", "текущим", "оставить текущим", "пропустить", "-", "=")

    FLOW = [
        ("vt_amount", "vt_phone"),
        ("vt_phone",  "vt_date"),
        ("vt_date",   None),
    ]

    for step_aw, next_aw in FLOW:
        if awaiting != step_aw:
            continue
        t = text.strip()
        if t.lower() in skip_words:
            state[step_aw] = None
        else:
            state[step_aw] = t
        if next_aw:
            _vtb_tg_send_next(state, next_aw, chat_id, token, tg_req)
        else:
            send("⏳ Обрабатываю чек...")
            _run_vtb_transgran_patch(token, uid, chat_id, state, tg_req)
        return


def _do_stmt_apply(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Применить патч выписки и отправить PDF."""
    try:
        from vyписка_service import patch_statement, calculate_balance_and_expenses, BASE_OLD_FIO
    except ImportError as e:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Модуль выписки недоступен: {e}"})
        if uid in USER_STATE:
            del USER_STATE[uid]
        return
    fp = Path(state["file_path"])
    repl = state.get("replacements", {}).copy()
    trans = list(state.get("transactions", []))
    amount_map = {}
    for pair in repl.get("amounts", []):
        if len(pair) >= 2:
            amount_map[int(pair[0])] = pair[1]
    trans_replaced = [float(amount_map.get(int(t), amount_map.get(t, t))) for t in trans if t > 0]
    balance_start = repl.get("balance_start", 55242.65)
    balance_end, expenses = calculate_balance_and_expenses(trans_replaced, balance_start)
    repl["balance_end"] = balance_end
    repl["expenses"] = expenses
    repl.setdefault("old_fio", BASE_OLD_FIO)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
        out_path = out_fp.name
    ok, err = patch_statement(fp, Path(out_path), repl)
    try:
        os.unlink(state["file_path"])
    except OSError:
        pass
    del USER_STATE[uid]
    if not ok:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return
    out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
    with open(out_path, "rb") as f:
        pdf_bytes = f.read()
    try:
        os.unlink(out_path)
    except OSError:
        pass
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": "✅ Выписка готова"}, files={"document": (out_name, pdf_bytes)})


def run_bot(token: str) -> None:
    offset = 0
    print("Бот запущен (без зависимостей)...")

    while True:
        try:
            # timeout 10 — короткий long-poll, меньше обрывов на нестабильной сети
            r = tg_request(token, "getUpdates", {"offset": offset, "timeout": 10})
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")
            except Exception:
                pass
            err_msg = body or e.reason
            if e.code == 400:
                offset = 0
                print(f"⚠️ getUpdates 400: {err_msg[:120]}")
                if "webhook" in err_msg.lower():
                    print("   Подсказка: webhook мог остаться — перезапустите бота.")
            else:
                print(f"Ошибка getUpdates HTTP {e.code}: {err_msg[:80]}")
            time.sleep(5)
            continue
        except Exception as e:
            print(f"Ошибка getUpdates: {e}")
            time.sleep(5)
            continue

        if not r.get("ok"):
            print("Ответ API:", r)
            time.sleep(5)
            continue

        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:

                if "message" in upd:
                    msg = upd["message"]
                    uid = msg["from"]["id"]
                    chat_id = msg["chat"]["id"]
                    if _ALLOWED_IDS and uid not in _ALLOWED_IDS:
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": ACCESS_DENIED_MSG})
                        continue
                    text = msg.get("text", "").strip()

                    if text == "/start":
                        if uid in USER_STATE:
                            if "file_path" in USER_STATE[uid]:
                                try:
                                    os.unlink(USER_STATE[uid]["file_path"])
                                except OSError:
                                    pass
                            del USER_STATE[uid]
                        tg_request(token, "sendMessage", {
                            "chat_id": msg["chat"]["id"],
                            "text": MAIN_MENU_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                        })
                        continue

                    if text == "/main":
                        txt, km = build_zayavki_list()
                        kb = json.loads(km)
                        kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": txt, "reply_markup": json.dumps(kb)})
                        continue

                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "report_choice":
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "📋 Нажмите кнопку «Тест» или «Заявка» выше."})
                        continue
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "zayavka_description":
                        state = USER_STATE[uid]
                        description = text or "(без описания)"
                        username = msg.get("from", {}).get("username", "") or msg.get("from", {}).get("first_name", "")
                        fp = save_zayavka(
                            uid, username,
                            state["amount_from"], state["amount_to"], state["bank"], state["pdf_name"],
                            description,
                        )
                        del USER_STATE[uid]
                        if fp:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Заявка сохранена."})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⚠️ Ошибка сохранения. Заявка не записана."})
                        continue

                    if "document" in msg:
                        doc = msg["document"]
                        fname = doc.get("file_name", "")
                        if not fname.lower().endswith(".pdf"):
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Отправьте PDF-файл."})
                            continue
                        state = USER_STATE.get(uid, {})
                        mode = state.get("mode", "")
                        if mode == "alfa_stmt_check":
                            _handle_alfa_stmt_upload(token, uid, msg["chat"]["id"], doc, fname, tg_request)
                            continue
                        if mode == "statement_edit":
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю выписку..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            try:
                                from vyписка_service import scan_statement_amounts, scan_statement_transactions
                                amounts = scan_statement_amounts(Path(path))
                                transactions = scan_statement_transactions(Path(path))
                            except ImportError:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Модуль выписки недоступен."})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                continue
                            USER_STATE[uid] = {"mode": "statement_edit", "step": "amounts", "file_path": path, "file_name": fname, "replacements": {}, "transactions": transactions}
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "✅ Выписка получена.\n\n💰 Введите замены сумм: с какой на какую\nНапример: 10 10000 или 10 5000 50 1000"})
                            continue
                        if mode == "statement_from_receipt":
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            try:
                                from receipt_extractor import extract_from_receipt, generate_fio_from_first_letter
                                from vyписка_service import calculate_balance_and_expenses
                                extracted = extract_from_receipt(Path(path))
                                amount = extracted.get("amount")
                                if not amount:
                                    tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Не удалось извлечь сумму из чека."})
                                    try:
                                        os.unlink(path)
                                    except OSError:
                                        pass
                                    del USER_STATE[uid]
                                    continue
                                fio_r = extracted.get("fio_recipient", "") or extracted.get("fio_payer", "")
                                first_letter = fio_r[0] if fio_r else "И"
                                generated_fio = generate_fio_from_first_letter(first_letter)
                                USER_STATE[uid] = {"mode": "statement_from_receipt", "step": "balance", "file_path": path, "file_name": fname, "amount": amount, "generated_fio": generated_fio}
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"✅ Чек получен.\n\n📊 Сумма: {amount} ₽\n👤 ФИО: {generated_fio}\n\n💰 Введите баланс на начало периода (число):"})
                            except ImportError as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Модуль недоступен: {e}"})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                del USER_STATE[uid]
                            continue
                        aw = state.get("awaiting", "")
                        if aw.startswith("db_add_"):
                            bank = aw.replace("db_add_", "")
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю и добавляю в базу..."})
                            try:
                                fp = tg_get_file_path(token, doc["file_id"])
                                pdf_data = tg_get_file(token, fp)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                                continue
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                tf.write(pdf_data)
                                path = tf.name
                            if add_receipt_to_index(path, bank):
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                del USER_STATE[uid]
                                tg_request(token, "sendMessage", {
                                    "chat_id": msg["chat"]["id"],
                                    "text": f"✅ Чек добавлен в базу ({'СБП' if bank == 'vtb_sbp' else 'ВТБ на ВТБ' if bank == 'vtb_vtb_vtb' else 'Альфа'})",
                                    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                                })
                            else:
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Не удалось добавить (возможно пустой PDF или ошибка)"})
                            continue
                        if "gen_bank_type" in USER_STATE.get(uid, {}) or "gen_transfer_type" in USER_STATE.get(uid, {}):
                            tg_request(token, "sendMessage", {
                                "chat_id": msg["chat"]["id"],
                                "text": "В режиме генерации чек не нужен. Продолжайте ввод выше или нажмите /start.",
                            })
                            continue
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "⏳ Скачиваю чек..."})
                        try:
                            fp = tg_get_file_path(token, doc["file_id"])
                            pdf_data = tg_get_file(token, fp)
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка загрузки: {e}"})
                            continue
                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                            tf.write(pdf_data)
                            path = tf.name
                        old = USER_STATE.get(uid, {})
                        if "file_path" in old and old["file_path"] != path:
                            try:
                                os.unlink(old["file_path"])
                            except OSError:
                                pass
                        USER_STATE[uid] = {"file_path": path, "file_name": fname}
                        tg_request(token, "sendMessage", {
                            "chat_id": msg["chat"]["id"],
                            "text": "📎 Чек получен. Выберите банк:\n\n• Альфа-Банк / ВТБ / Авто — замена только суммы\n• Альфа СБП (все поля) — сумма, дата, получатель, телефон, банк, счёт",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏦 Альфа-Банк", "callback_data": "bank_alfa"},
                                        {"text": "🏛 ВТБ", "callback_data": "bank_vtb"},
                                    ],
                                    [{"text": "🏦 Альфа СБП (все поля)", "callback_data": "bank_alfa_sbp_full"}],
                                    [{"text": "🌐 Альфа Трансгран", "callback_data": "bank_alfa_transgran"}],
                                    [{"text": "🌍 ВТБ Трансгран (UZS)", "callback_data": "bank_vtb_transgran"}],
                                    [{"text": "🔍 Авто", "callback_data": "bank_auto"}],
                                    [{"text": "❌ Отмена", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue

                    # Альфа СБП все поля: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("mode") == "alfa_sbp_full":
                        _handle_alfa_sbp_input(token, uid, chat_id, text, tg_request)
                        continue

                    # Выписка Альфа-Банк: текст
                    if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("alfa_stmt"):
                        _handle_alfa_stmt_text(token, uid, chat_id, text, tg_request)
                        continue

                    # Режим выписки: текст
                    if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("statement_"):
                        _handle_stmt_text(token, uid, chat_id, text, tg_request)
                        continue

                    # Альфа Трансгран: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("at_"):
                        _handle_alfa_transgran_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # ВТБ Трансгран: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("vt_"):
                        _handle_vtb_transgran_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # ВТБ «Все поля»: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting", "").startswith("vtb_"):
                        _handle_vtb_full_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    # Режим «Сгенерировать»: по gen_bank_type (надёжнее, чем только awaiting)
                    if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                        _handle_gen_input(token, uid, chat_id, text, msg, tg_request)
                        continue

                    if uid in USER_STATE and "bank" in USER_STATE[uid]:
                        parsed = parse_amounts(text)
                        if not parsed:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Неверный формат. Введите две суммы, например: 10 5000"})
                            continue
                        amount_from, amount_to = parsed
                        if amount_from <= 0 or amount_to <= 0:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Суммы должны быть больше 0."})
                            continue
                        chat_id = msg["chat"]["id"]
                        state = USER_STATE[uid]
                        inp = state["file_path"]
                        bank = state["bank"]
                        out_name = Path(state["file_name"]).stem + f"_{format_amount_display(amount_to).replace(' ', '_')}.pdf"

                        # ВТБ «Только сумма»: patch_amount_only выравнивает по wall.
                        # pdf_patcher не пересчитывает Tm для kern -11.11111 → сумма смещается вправо.
                        if bank == "vtb" and state.get("vtb_mode") == "amount":
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
                            try:
                                print(f"  Патч ВТБ (только сумма) {amount_from}→{amount_to}...", end=" ", flush=True)
                                data = bytearray(Path(inp).read_bytes())
                                try:
                                    out_bytes = bytes(patch_amount_only(data, Path(inp), amount_to))
                                except Exception:
                                    ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
                                    if not ok_sum or new_data is None:
                                        del USER_STATE[uid]
                                        try:
                                            os.unlink(inp)
                                        except OSError:
                                            pass
                                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Сумма не найдена. В чеке должна быть {format_amount_display(amount_from)} ₽. {err_sum or ''}"})
                                        continue
                                    out_bytes = new_data
                                try:
                                    os.unlink(inp)
                                except OSError:
                                    pass
                                del USER_STATE[uid]
                                print("отправляю...", end=" ", flush=True)
                                caption = f"✅ Готово: {format_amount_display(amount_from)} ₽ → {format_amount_display(amount_to)} ₽"
                                tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
                                print("готово")
                                USER_STATE[uid] = {
                                    "awaiting": "report_choice",
                                    "amount_from": amount_from,
                                    "amount_to": amount_to,
                                    "bank": bank,
                                    "pdf_name": out_name,
                                }
                                tg_request(token, "sendMessage", {
                                    "chat_id": chat_id,
                                    "text": "📋 Отчёт:",
                                    "reply_markup": json.dumps({
                                        "inline_keyboard": [
                                            [{"text": "Тест", "callback_data": "report_test"}],
                                            [{"text": "Заявка", "callback_data": "report_zayavka"}],
                                            [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                        ],
                                    }),
                                })
                            except Exception as e:
                                print("ошибка:", e)
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
                            continue

                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
                        try:
                            print(f"  Патч {amount_from}→{amount_to} ({bank})...", end=" ", flush=True)
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                out_path = tf.name
                            ok, err = patch_pdf_file(inp, out_path, amount_from, amount_to, bank=bank)
                            try:
                                os.unlink(inp)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                            if not ok:
                                print("не найден")
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                continue
                            with open(out_path, "rb") as f:
                                pdf_bytes = f.read()
                            try:
                                os.unlink(out_path)
                            except OSError:
                                pass
                            print("отправляю...", end=" ", flush=True)
                            caption = f"✅ Готово: {format_amount_display(amount_from)} ₽ → {format_amount_display(amount_to)} ₽"
                            tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
                            print("готово")
                            USER_STATE[uid] = {
                                "awaiting": "report_choice",
                                "amount_from": amount_from,
                                "amount_to": amount_to,
                                "bank": bank,
                                "pdf_name": out_name,
                            }
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": "📋 Отчёт:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "Тест", "callback_data": "report_test"}],
                                        [{"text": "Заявка", "callback_data": "report_zayavka"}],
                                        [{"text": "🏠 Главное меню", "callback_data": "main_back"}],
                                    ],
                                }),
                            })
                        except Exception as e:
                            print("ошибка:", e)
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
                        continue

                    if uid in USER_STATE:
                        if "gen_bank_type" in USER_STATE[uid] or "gen_transfer_type" in USER_STATE[uid]:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Продолжайте ввод выше или нажмите кнопки."})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала выберите банк (кнопками выше)."})
                    else:
                        tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Сначала отправьте чек и выберите банк."})

                elif "callback_query" in upd:
                    q = upd["callback_query"]
                    uid = q["from"]["id"]
                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"]})
                    if _ALLOWED_IDS and uid not in _ALLOWED_IDS:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": ACCESS_DENIED_MSG})
                        continue
                    if q["data"] == "main_generate":
                        USER_STATE[uid] = {"awaiting": "gen_type", "gen_transfer_type": None, "gen_bank_type": None}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ Сгенерировать чек\n\n"
                                "Бот найдёт один чек из базы с нужными буквами.\n\n"
                                "Выберите тип перевода:"
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📱 Перевод по СБП", "callback_data": "gen_type_sbp"}],
                                    [{"text": "💳 По номеру карты (скоро)", "callback_data": "gen_type_card"}],
                                    [{"text": "🌐 Трансгран (скоро)", "callback_data": "gen_type_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_type_sbp":
                        USER_STATE[uid] = {"awaiting": "gen_subtype", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": None}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ Перевод по СБП\n\n"
                                "Выберите тип чека:"
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📱 СБП", "callback_data": "gen_subtype_sbp"}],
                                    [{"text": "🏛 ВТБ на ВТБ", "callback_data": "gen_subtype_vtb_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_subtype_sbp":
                        USER_STATE[uid] = {"awaiting": "gen_bank", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": "vtb_sbp"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ СБП\n\n"
                                f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                                "Выберите банк (пока только ВТБ):"
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "🏛 ВТБ", "callback_data": "gen_bank_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_subtype_vtb_vtb":
                        USER_STATE[uid] = {"awaiting": "gen_bank", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": "vtb_vtb_vtb"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ ВТБ на ВТБ\n\n"
                                f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                                "Выберите банк (пока только ВТБ):"
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "🏛 ВТБ", "callback_data": "gen_bank_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] in ("gen_type_card", "gen_type_transgran"):
                        if uid in USER_STATE and "gen_bank_type" in USER_STATE[uid]:
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "Пока доступен только перевод по СБП.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "main_db":
                        counts = get_bank_counts()
                        n_sbp, n_vtb, n_alfa = counts["vtb_sbp"], counts["vtb_vtb_vtb"], counts["alfa"]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                f"📂 Проверка базы\n\n"
                                f"СБП: {n_sbp} | ВТБ на ВТБ: {n_vtb} | Альфа: {n_alfa}\n\n"
                                "Чтобы добавить чек: выберите тип, затем загрузите PDF.\n"
                                "Если добавили файлы вручную — нажмите «Обновить индекс»."
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "📋 СБП", "callback_data": "db_list_vtb_sbp"}, {"text": "➕ СБП", "callback_data": "db_add_vtb_sbp"}],
                                    [{"text": "📋 ВТБ→ВТБ", "callback_data": "db_list_vtb_vtb"}, {"text": "➕ ВТБ→ВТБ", "callback_data": "db_add_vtb_vtb"}],
                                    [{"text": "📋 Альфа", "callback_data": "db_list_alfa"}, {"text": "➕ Альфа", "callback_data": "db_add_alfa"}],
                                    [{"text": "🔄 Обновить индекс", "callback_data": "db_rebuild"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "db_rebuild":
                        try:
                            idx = build_and_save()
                            n_sbp = len(idx.get("vtb_sbp", {}))
                            n_vtb = len(idx.get("vtb_vtb_vtb", {}))
                            n_alfa = len(idx.get("alfa", {}))
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"✅ Индекс обновлён\n\nСБП: {n_sbp} | ВТБ на ВТБ: {n_vtb} | Альфа: {n_alfa}",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ К базе", "callback_data": "main_db"}]]}),
                            })
                        except Exception as e:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"⚠️ Ошибка: {e}",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                            })
                        continue
                    if q["data"] in ("db_add_vtb_sbp", "db_add_vtb_vtb", "db_add_alfa"):
                        if "vtb_sbp" in q["data"]:
                            bank = "vtb_sbp"
                        elif "vtb_vtb" in q["data"]:
                            bank = "vtb_vtb_vtb"
                        else:
                            bank = "alfa"
                        USER_STATE[uid] = {"awaiting": f"db_add_{bank}"}
                        lbl = {"vtb_sbp": "СБП", "vtb_vtb_vtb": "ВТБ на ВТБ", "alfa": "Альфа"}.get(bank, bank)
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"➕ Отправьте PDF-чек для добавления в базу ({lbl})",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_vtb_sbp":
                        idx = load_index()
                        items = list(idx.get("vtb_sbp", {}).keys())
                        lines = ["📋 Чеки СБП:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_vtb_vtb":
                        idx = load_index()
                        items = list(idx.get("vtb_vtb_vtb", {}).keys())
                        lines = ["📋 Чеки ВТБ на ВТБ:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "db_list_alfa":
                        idx = load_index()
                        items = list(idx.get("alfa", {}).keys())
                        lines = ["📋 Чеки Альфа:"] + (items[:20] or ["(пусто)"])
                        if len(items) > 20:
                            lines.append(f"... и ещё {len(items) - 20}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "\n".join(lines),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_db"}]]}),
                        })
                        continue
                    if q["data"] == "gen_bank_vtb":
                        prev = USER_STATE.get(uid, {})
                        subtype = prev.get("gen_vtb_subtype", "vtb_sbp")
                        USER_STATE[uid] = {
                            "awaiting": "gen_payer" if subtype == "vtb_sbp" else "gen_amount",
                            "gen_bank_type": "vtb",
                            "gen_transfer_type": prev.get("gen_transfer_type", "sbp"),
                            "gen_vtb_subtype": subtype,
                        }
                        if subtype == "vtb_sbp":
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    "✨ Сгенерировать чек СБП (ВТБ)\n\n"
                                    "📋 Сначала проверка ФИО.\n\n"
                                    "1️⃣ Плательщик (например: Артем Никитич К.):"
                                ),
                            })
                        else:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": (
                                    "✨ Сгенерировать (ВТБ)\n\n"
                                    "1️⃣ Сумма: с какой на какую (например: 10 1000) или одна сумма (50000)"
                                ),
                            })
                        continue
                    if q["data"] in ("gen_keep_payer", "gen_keep_recipient", "gen_keep_phone", "gen_keep_bank", "gen_keep_account", "gen_keep_opid"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        subtype = state.get("gen_vtb_subtype", "vtb_sbp")
                        keep_map = {
                            "gen_keep_payer": ("gen_payer", "gen_recipient", "4️⃣ Получатель (ФИО):"),
                            "gen_keep_recipient": ("gen_recipient", "gen_phone" if subtype != "vtb_sbp" else None, "5️⃣ Телефон:" if subtype != "vtb_sbp" else ""),
                            "gen_keep_phone": ("gen_phone", "gen_bank", "6️⃣ Банк получателя:"),
                            "gen_keep_bank": ("gen_bank", "gen_account" if subtype == "vtb_sbp" else "gen_operation_id", "6️⃣ Счёт (4 цифры):" if subtype == "vtb_sbp" else "7️⃣ ID операции:"),
                            "gen_keep_account": ("gen_account", "gen_operation_id", "7️⃣ ID операции (B606...). Оставить или ввести:"),
                            "gen_keep_opid": ("gen_operation_id", None, ""),
                        }
                        field_key, next_aw, _ = keep_map[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Пусто"})
                        if next_aw:
                            _gen_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Генерирую чек..."})
                            if state.get("gen_vtb_subtype") == "vtb_sbp":
                                _run_sbp_generate(token, uid, cid, state, tg_request)
                            else:
                                _run_gen_patch(token, uid, cid, state, tg_request)
                        continue
                    if q["data"] == "main_new":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "📄 Новый чек\n\n"
                                "1. Отправьте PDF-чек\n"
                                "2. Выберите банк (Альфа-Банк или ВТБ)\n"
                                "3. Введите сумму: с какой на какую (например: 10 5000)"
                            ),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "main_stmt":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Создание выписки\n\nВыберите вариант:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "🏦 Выписка Альфа-Банк", "callback_data": "alfa_stmt_start"}],
                                    [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_edit"}],
                                    [{"text": "📄 Выписка по чеку", "callback_data": "stmt_receipt"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"].startswith("alfa_stmt"):
                        _handle_alfa_stmt_callback(token, uid, q, tg_request)
                        continue
                    if q["data"] == "stmt_edit":
                        USER_STATE[uid] = {"mode": "statement_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование своей выписки\n\nОтправьте PDF-файл выписки.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_receipt":
                        USER_STATE[uid] = {"mode": "statement_from_receipt", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка по чеку\n\nОтправьте PDF-файл чека.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_skip" or q["data"] == "stmt_next":
                        if uid not in USER_STATE or USER_STATE[uid].get("mode") != "statement_edit":
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        _do_stmt_apply(token, uid, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue
                    if q["data"] == "stmt_custom":
                        if uid in USER_STATE:
                            USER_STATE[uid]["step"] = "custom"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "➕ Введите замены: поле=значение\nФИО=..., баланс_начало=..., телефон=..., номер_заявки=...\nПосле ввода нажмите Далее.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "➡️ Далее", "callback_data": "stmt_next"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_retry_fio":
                        if uid in USER_STATE:
                            USER_STATE[uid]["step"] = "custom"
                        tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": "Введите ФИО заново (без недоступных символов):"})
                        continue
                    if q["data"] == "stmt_skip_fio":
                        if uid in USER_STATE:
                            USER_STATE[uid].pop("pending_fio", None)
                        tg_request(token, "sendMessage", {"chat_id": q["message"]["chat"]["id"], "text": "ФИО не заменяется. Введите другие замены или Далее."})
                        continue
                    if q["data"] == "main_back":
                        if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith(("statement_", "alfa_stmt")):
                            if "file_path" in USER_STATE[uid]:
                                try:
                                    os.unlink(USER_STATE[uid]["file_path"])
                                except OSError:
                                    pass
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": MAIN_MENU_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": MAIN_MENU_KB}),
                        })
                        continue
                    if q["data"] == "main_changelog":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": CHANGELOG_TEXT,
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_back"}]]}),
                        })
                        continue
                    if q["data"] == "main_zayavki":
                        try:
                            txt, km = build_zayavki_list()
                            kb = json.loads(km)
                            kb["inline_keyboard"].append([{"text": "🔄 Обновить", "callback_data": "main_zayavki"}, {"text": "⬅️ Назад", "callback_data": "main_back"}])
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": txt,
                                "reply_markup": json.dumps(kb),
                            })
                        except Exception as e:
                            print(f"  ⚠️ main_zayavki: {e}")
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "⚠️ Ошибка обновления. Попробуйте ещё раз.",
                            })
                        continue
                    if q["data"].startswith("v_"):
                        try:
                            zid = q["data"][2:] if len(q["data"]) > 2 else ""
                            if zid and (detail := build_zayavka_detail(zid)):
                                txt, km = detail
                                tg_request(token, "editMessageText", {
                                    "chat_id": q["message"]["chat"]["id"],
                                    "message_id": q["message"]["message_id"],
                                    "text": txt,
                                    "reply_markup": km,
                                })
                            else:
                                tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Заявка не найдена"})
                        except Exception as e:
                            print(f"  ⚠️ v_ callback: {e}")
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                        continue
                    if q["data"].startswith("st_"):
                        try:
                            parts = q["data"].split("_")
                            if len(parts) >= 3:
                                zid = "_".join(parts[1:-1])
                                short = parts[-1]
                                new_st = STATUS_SHORT.get(short)
                                if new_st and update_zayavka_status(zid, new_st):
                                    if detail := build_zayavka_detail(zid):
                                        txt, km = detail
                                        tg_request(token, "editMessageText", {
                                            "chat_id": q["message"]["chat"]["id"],
                                            "message_id": q["message"]["message_id"],
                                            "text": txt,
                                            "reply_markup": km,
                                        })
                                    else:
                                        txt, km = build_zayavki_list()
                                        kb = json.loads(km)
                                        kb["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "main_back"}])
                                        tg_request(token, "editMessageText", {
                                            "chat_id": q["message"]["chat"]["id"],
                                            "message_id": q["message"]["message_id"],
                                            "text": txt,
                                            "reply_markup": json.dumps(kb),
                                        })
                                else:
                                    tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Не удалось"})
                        except Exception as e:
                            print(f"  ⚠️ st_ callback: {e}")
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "⚠️ Ошибка"})
                        continue
                    if q["data"] == "cancel":
                        if uid in USER_STATE and "file_path" in USER_STATE[uid]:
                            try:
                                os.unlink(USER_STATE[uid]["file_path"])
                            except OSError:
                                pass
                            del USER_STATE[uid]
                        main_text = (
                            "👋 Главное меню.\n\n"
                            "📋 Отправьте PDF-чек или /start для начала."
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": main_text,
                            "reply_markup": json.dumps({"inline_keyboard": []}),
                        })
                        continue
                    if q["data"] == "report_test":
                        if uid in USER_STATE:
                            del USER_STATE[uid]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📋 Отчёт: Тест (ничего не сохранено)",
                            "reply_markup": json.dumps({"inline_keyboard": []}),
                        })
                        continue
                    if q["data"] == "report_zayavka":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Сессия истекла. Отправьте чек заново."})
                            continue
                        USER_STATE[uid]["awaiting"] = "zayavka_description"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📝 Опишите заявку:\nссылка на платеж / казино / обменник",
                            "reply_markup": json.dumps({"inline_keyboard": []}),
                        })
                        continue
                    if q["data"] == "vtb_mode_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["vtb_mode"] = "amount"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "💰 Введите сумму: с какой на какую менять\nНапример: 10 5000 или 50 10000",
                        })
                        continue
                    if q["data"] == "vtb_mode_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["vtb_mode"] = "full"
                        USER_STATE[uid]["awaiting"] = "vtb_amount"
                        txt = (
                            "📋 Режим «Все поля»\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            "1️⃣ Сумма: с какой на какую менять (например: 10 1000 или 50 50000)"
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": txt,
                        })
                        continue
                    if q["data"] in ("vtb_keep_payer", "vtb_keep_recipient", "vtb_keep_phone", "vtb_keep_bank"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid, mid = q["message"]["chat"]["id"], q["message"]["message_id"]
                        keep_map = {
                            "vtb_keep_payer": ("vtb_payer", "vtb_recipient", "4️⃣ Получатель (ФИО):"),
                            "vtb_keep_recipient": ("vtb_recipient", "vtb_phone", "5️⃣ Телефон получателя:"),
                            "vtb_keep_phone": ("vtb_phone", "vtb_bank", "6️⃣ Банк получателя:"),
                            "vtb_keep_bank": ("vtb_bank", None, ""),
                        }
                        field_key, next_aw, _ = keep_map[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _vtb_full_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_vtb_full_patch(token, uid, cid, state, tg_request)
                        continue
                    # ВТБ Трансгран: callback handlers
                    if q["data"] == "bank_vtb_transgran":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        state = USER_STATE[uid]
                        state["bank"] = "vtb_transgran"
                        try:
                            pdf_data = Path(state["file_path"]).read_bytes()
                            fields = extract_vtb_transgran_fields(pdf_data)
                        except Exception:
                            fields = {}
                        state["vt_fields"] = fields
                        field_lines = []
                        for label, key in [("Сумма операции", "amount"), ("Курс обмена", "rate"),
                                           ("Зачисление", "credited"), ("Телефон", "phone"),
                                           ("Дата", "date"), ("Получатель", "name"),
                                           ("Банк", "bank"), ("Страна", "country")]:
                            field_lines.append(f"  {label}: {fields.get(key, '—')}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🌍 ВТБ Трансгран (UZS)\n\n"
                                "Текущие поля чека:\n" + "\n".join(field_lines) + "\n\n"
                                "Зачисление считается автоматически (сумма × курс).\n"
                                "Имя получателя не меняется.\n\n"
                                "Введите новые значения пошагово."
                            ),
                        })
                        _vtb_tg_send_next(state, "vt_amount", q["message"]["chat"]["id"], token, tg_request)
                        continue
                    if q["data"] in ("vt_keep_amount", "vt_keep_phone", "vt_keep_date"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        keep_flow = {
                            "vt_keep_amount": ("vt_amount", "vt_phone"),
                            "vt_keep_phone":  ("vt_phone",  "vt_date"),
                            "vt_keep_date":   ("vt_date",   None),
                        }
                        field_key, next_aw = keep_flow[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _vtb_tg_send_next(state, next_aw, cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_vtb_transgran_patch(token, uid, cid, state, tg_request)
                        continue
                    # Альфа СБП все поля: callback
                    if q["data"] == "bank_alfa_sbp_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["mode"] = "alfa_sbp_full"
                        USER_STATE[uid]["alfa_sbp_step"] = 0
                        USER_STATE[uid]["alfa_sbp_fields"] = {}
                        _ALFA_SBP_FIELDS = [
                            ("amount", "💰 Сумма перевода (число, например: 5000)"),
                            ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
                            ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
                            ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
                            ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
                            ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
                        ]
                        USER_STATE[uid]["_alfa_sbp_field_list"] = _ALFA_SBP_FIELDS
                        _, prompt = _ALFA_SBP_FIELDS[0]
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🏦 Альфа-Банк СБП — замена всех полей\n\n"
                                "Введите новые значения пошагово.\n"
                                "Отправьте - чтобы оставить поле без изменений.\n\n"
                                f"Шаг 1/{len(_ALFA_SBP_FIELDS)}: {prompt}"
                            ),
                        })
                        continue

                    # Альфа Трансгран: callback handlers
                    if q["data"] == "bank_alfa_transgran":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        state = USER_STATE[uid]
                        state["bank"] = "alfa_transgran"
                        try:
                            pdf_data = Path(state["file_path"]).read_bytes()
                            fields = extract_transgran_fields(pdf_data)
                        except Exception:
                            fields = {}
                        state["at_fields"] = fields
                        field_lines = []
                        for label, key in [("Сумма перевода", "amount"), ("Комиссия", "commission"),
                                           ("Курс", "rate"), ("Зачисление", "credited"),
                                           ("Телефон", "phone"), ("Получатель", "name"),
                                           ("Номер операции", "operation_id")]:
                            field_lines.append(f"  {label}: {fields.get(key, '—')}")
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🌐 Альфа Трансгран\n\n"
                                "Текущие поля чека:\n" + "\n".join(field_lines) + "\n\n"
                                "Введите новые значения пошагово.\n"
                                "Для каждого поля можно «Оставить текущим»."
                            ),
                        })
                        _alfa_tg_send_next(state, "at_amount", "", q["message"]["chat"]["id"], token, tg_request)
                        continue
                    if q["data"] in ("at_keep_amount", "at_keep_commission", "at_keep_rate", "at_keep_credited", "at_keep_phone", "at_keep_name", "at_keep_opid"):
                        if uid not in USER_STATE:
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        keep_flow = {
                            "at_keep_amount": ("at_amount", "at_commission"),
                            "at_keep_commission": ("at_commission", "at_rate"),
                            "at_keep_rate": ("at_rate", "at_credited"),
                            "at_keep_credited": ("at_credited", "at_phone"),
                            "at_keep_phone": ("at_phone", "at_name"),
                            "at_keep_name": ("at_name", "at_operation_id"),
                            "at_keep_opid": ("at_operation_id", None),
                        }
                        field_key, next_aw = keep_flow[q["data"]]
                        state[field_key] = None
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅ Оставлено текущим"})
                        if next_aw:
                            _alfa_tg_send_next(state, next_aw, "", cid, token, tg_request)
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Обрабатываю чек..."})
                            _run_alfa_transgran_patch(token, uid, cid, state, tg_request)
                        continue
                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                        continue
                    bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_auto": "auto"}
                    bank = bank_map.get(q["data"], "auto")
                    bank_name = {"alfa": "Альфа-Банк", "vtb": "ВТБ", "auto": "Авто"}[bank]
                    USER_STATE[uid]["bank"] = bank
                    if bank == "vtb":
                        scan_tip = ""
                        try:
                            bad = scan_vtb_unsupported_chars(USER_STATE[uid]["file_path"])
                            if bad:
                                tips = [f"«{c}» → «{FALLBACK_TIPS.get(c, '?')}»" if c in FALLBACK_TIPS else f"«{c}»" for c in sorted(bad)]
                                scan_tip = f"\n\n⚠️ В чеке найдены буквы для замены: {', '.join(tips)}"
                        except Exception:
                            pass
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"✅ Банк: {bank_name}{scan_tip}\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "vtb_mode_amount"}],
                                    [{"text": "📋 Все поля (дата, ФИО, телефон, банк)", "callback_data": "vtb_mode_full"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                    else:
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"✅ Банк: {bank_name}\n\n💰 Введите сумму: с какой на какую менять\nНапример: 10 5000 или 50 10000",
                        })
                    continue
            except Exception as _upd_err:
                import traceback
                print(f"❌ Ошибка обработки апдейта {upd.get('update_id')}: {_upd_err}", flush=True)
                traceback.print_exc()


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN (в .env или export)")
        return
    if _proxy_url:
        _masked = _proxy_url.split("@")[-1] if "@" in _proxy_url else _proxy_url[:50]
        print("🔒 Прокси:", _masked)
    # Проверка токена и сброс webhook (иначе getUpdates даёт 400)
    try:
        r = tg_request(token, "getMe")
        if r.get("ok"):
            print("✅ Бот:", r["result"].get("username", "?"))
            if _ALLOWED_IDS:
                print("🔐 Доступ: только", len(_ALLOWED_IDS), "пользовател(ей)")
            dw = tg_request(token, "deleteWebhook", {"drop_pending_updates": True})
            if not dw.get("ok"):
                print("⚠️ deleteWebhook:", dw.get("description", dw))
            run_bot(token)
        else:
            print("❌ Токен неверный:", r.get("description"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Токен неверный (401). Сделай в @BotFather:")
            print("   /mybots → выбери бота → API Token → Revoke current token")
            print("   Обнови .env с новым токеном")
        else:
            print("❌ Ошибка HTTP:", e.code, e.reason)


if __name__ == "__main__":
    main()
