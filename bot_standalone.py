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
    "📁 Чек — загрузить PDF или сгенерировать чек из базы.\n"
    "📋 Создать выписку — редактирование, по чеку или с нуля.\n"
    "📂 База — просмотр и добавление шаблонов.\n"
    "📝 Последние изменения — что нового."
)
MAIN_MENU_KB = [
    [{"text": "📁 Чек", "callback_data": "main_check"}],
    [{"text": "📋 Создать выписку", "callback_data": "main_stmt"}],
    [{"text": "📂 База", "callback_data": "main_db"}],
    [{"text": "📝 Последние изменения", "callback_data": "main_changelog"}],
]

CHANGELOG_TEXT = (
    "📝 Последние изменения\n\n"
    "• Масштабный рефакторинг навигации:\n"
    "  — Кнопки «Загрузить чек» и «Сгенерировать чек» объединены под «Чек».\n"
    "  — Раздел переименован: «Проверка базы» → «База».\n"
    "  — Убраны «Заявки» и кнопки «Тест»/«Заявка» после генерации.\n\n"
    "• Выбор банка — новый UI:\n"
    "  — Альфа Чек | ВТБ в два столбца, «Авто» снизу.\n"
    "  — У каждого банка своё суб-меню: только сумма / все поля / трансгран.\n\n"
    "• Автодетект суммы — режим «только сумма» теперь сам определяет сумму из чека;\n"
    "  пользователь вводит только новое значение. Комиссия сканируется автоматически.\n\n"
    "• Авто — любой банк: свободный формат замен (СТАРОЕ = НОВОЕ) без привязки к банку.\n\n"
    "• Выписки — новое меню Альфа / ВТБ с тремя вариантами каждого:\n"
    "  Редактирование своей / По чеку / Создание с нуля.\n\n"
    "• Исправлен баг: после генерации нажатие «Главная» больше не ломает навигацию.\n\n"
    "• База теперь показывает и шаблоны выписок."
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




def _increment_filename(stem: str) -> str:
    """Увеличить последнюю цифру в имени файла на 1. check3 → check4, abc → abc2."""
    m = re.search(r'(\d+)(\D*)$', stem)
    if m:
        num = int(m.group(1)) + 1
        return stem[:m.start(1)] + str(num) + m.group(2)
    return stem + "2"


def extract_commission_from_pdf(pdf_path: str) -> int | None:
    """Извлечь комиссию из PDF-чека. Вернуть сумму в рублях или None."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return None
    patterns = [
        r'[Кк]омиссия[^\d]*?([\d\s]+)[,\.](\d{2})',
        r'[Кк]омиссия[^\d]*?([\d\s]+)\s*(?:₽|руб|RUR|RUB)',
        r'Commission[^\d]*?([\d\s]+)[,\.](\d{2})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(1).replace(' ', '').replace('\xa0', '')
            try:
                val = int(raw)
                return val
            except ValueError:
                pass
    return None


def parse_auto_replacements(text: str) -> list[tuple[str, str]]:
    """Разобрать пары замен формата (старое = новое) или старое=новое."""
    pairs = re.findall(r'\(([^)]+?)\s*=\s*([^)]+?)\)', text)
    if pairs:
        return [(a.strip(), b.strip()) for a, b in pairs]
    # Fallback: строки вида ключ=значение
    result = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip()
            if k and v:
                result.append((k, v))
    return result


def _send_main_menu_button(token: str, chat_id: int, tg_req) -> None:
    """Отправить кнопку «Главное меню» после генерации."""
    tg_req(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "✅ Готово! Нажмите для возврата:",
        "reply_markup": json.dumps({
            "inline_keyboard": [[{"text": "🏠 Главное меню", "callback_data": "main_back"}]],
        }),
    })


def _do_amount_patch(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Выполнить патч суммы (и опционально комиссии) и отправить PDF."""
    inp = state["file_path"]
    bank = state["bank"]
    amount_from = state.get("current_amount") or 0
    amount_to = state["amount_to"]
    out_stem = _increment_filename(Path(state["file_name"]).stem)
    out_name = out_stem + ".pdf"

    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Обрабатываю чек..."})
    try:
        commission_from = state.get("current_commission") or 0
        commission_to = state.get("commission_to")
        data = bytearray(Path(inp).read_bytes())

        if bank == "vtb":
            try:
                out_bytes = bytes(patch_amount_only(data, Path(inp), amount_to))
            except Exception:
                ok_sum, err_sum, new_data = pdf_patch_amount(data, amount_from, amount_to, bank="vtb")
                if not ok_sum or new_data is None:
                    try:
                        os.unlink(inp)
                    except OSError:
                        pass
                    del USER_STATE[uid]
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Сумма не найдена в чеке. {err_sum or ''}"})
                    _send_main_menu_button(token, chat_id, tg_req)
                    return
                out_bytes = new_data
        else:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                out_path_tmp = tf.name
            ok, err = patch_pdf_file(inp, out_path_tmp, amount_from, amount_to, bank=bank)
            if not ok:
                try:
                    os.unlink(inp)
                    os.unlink(out_path_tmp)
                except OSError:
                    pass
                del USER_STATE[uid]
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {err}"})
                _send_main_menu_button(token, chat_id, tg_req)
                return
            out_bytes = Path(out_path_tmp).read_bytes()
            try:
                os.unlink(out_path_tmp)
            except OSError:
                pass

        # Комиссия
        if commission_to is not None and commission_from > 0:
            try:
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    comm_path = tf.name
                Path(comm_path).write_bytes(out_bytes)
                old_c = str(commission_from)
                new_c = str(commission_to)
                if patch_replacements(Path(comm_path), Path(comm_path), [(old_c, new_c)]):
                    out_bytes = Path(comm_path).read_bytes()
                try:
                    os.unlink(comm_path)
                except OSError:
                    pass
            except Exception:
                pass

        try:
            os.unlink(inp)
        except OSError:
            pass
        del USER_STATE[uid]
        am_from_disp = format_amount_display(amount_from) if amount_from else "?"
        caption = f"✅ Готово: {am_from_disp} ₽ → {format_amount_display(amount_to)} ₽"
        if commission_to is not None:
            caption += f"\nКомиссия: {format_amount_display(commission_from)} → {format_amount_display(commission_to)} ₽"
        tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, out_bytes)})
        _send_main_menu_button(token, chat_id, tg_req)
    except Exception as e:
        try:
            os.unlink(inp)
        except OSError:
            pass
        if uid in USER_STATE:
            del USER_STATE[uid]
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}\n\nПопробуй снова или отправь чек заново."})
        _send_main_menu_button(token, chat_id, tg_req)


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
        _send_main_menu_button(token, chat_id, tg_req)
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
        lines.append("Добавьте чеки в разделе «База» или замените буквы (ё→е, ‑→-).")
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
        _send_main_menu_button(token, chat_id, tg_req)
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
    _send_main_menu_button(token, chat_id, tg_req)


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


# ── Выписка: пошаговый визард ──────────────────────────────────────────────

def _sw_send_step(token: str, chat_id: int, state: dict, tg_req) -> None:
    """Send the current wizard step to the user."""
    from statement_wizard import (
        get_current_field, format_step_message, get_step_keyboard,
        format_preview, get_preview_keyboard,
    )
    field = get_current_field(state)
    if field is None:
        txt = format_preview(state)
        kb = get_preview_keyboard("sw")
        tg_req(token, "sendMessage", {
            "chat_id": chat_id, "text": txt,
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return
    current_val = state.get("current_values", {}).get(field["key"])
    msg = format_step_message(field, current_val, changes=state.get("changes", {}))
    kb = get_step_keyboard(field, current_val, "sw")
    tg_req(token, "sendMessage", {
        "chat_id": chat_id, "text": msg,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps({"inline_keyboard": kb}),
    })


def _sw_handle_text(token: str, uid: int, chat_id: int, text: str, tg_req) -> None:
    """Handle text input in wizard mode: apply value to current field, advance."""
    from statement_wizard import (
        get_current_field, advance_field, validate_input, validate_account_number,
    )
    state = USER_STATE.get(uid)
    if not state:
        return
    field = get_current_field(state)
    if field is None:
        if "=" in text:
            parts = text.strip().split("\n")
            applied = []
            for line in parts:
                if "=" not in line:
                    continue
                old, _, new = line.partition("=")
                old, new = old.strip(), new.strip()
                if old and new:
                    state.setdefault("raw_replacements", []).append((old, new))
                    applied.append(f"{old} → {new}")
            if applied:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Добавлены замены:\n" + "\n".join(applied)})
        return
    value = text.strip()
    if not value:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Введите значение или нажмите кнопку."})
        return

    available = None
    pdf_path = state.get("file_path")
    if pdf_path:
        try:
            from vyписка_service import get_available_chars
            available = get_available_chars(Path(pdf_path))
        except (ImportError, Exception):
            pass
    if not available and state.get("bank") == "alfa":
        try:
            from alfa_statement_service import get_available_chars as alfa_chars
            available = alfa_chars()
        except (ImportError, Exception):
            pass

    ok, warning = validate_input(field, value, available)
    if not ok:
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": warning + "\nПопробуйте ещё раз или нажмите «Пропустить»."})
        return

    if field.get("suggest_fio") and state.get("bank") == "alfa" and "\n" not in value:
        parts = value.split()
        if len(parts) >= 3:
            value = f"{parts[0]} {parts[1]}\n{' '.join(parts[2:])}"
    state.setdefault("changes", {})[field["key"]] = value
    advance_field(state)
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_handle_callback(token: str, uid: int, q: dict, tg_req) -> None:
    """Handle callback queries for the statement wizard (sw_*)."""
    from statement_wizard import (
        get_current_field, advance_field, suggest_fio, suggest_address,
        jump_to_block, format_preview, get_preview_keyboard,
    )
    data = q["data"]
    chat_id = q["message"]["chat"]["id"]
    state = USER_STATE.get(uid)
    if not state:
        tg_req(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
        return

    if data == "sw_keep" or data == "sw_skip":
        advance_field(state)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_suggest_fio":
        field = get_current_field(state)
        if not field:
            return
        current = state.get("current_values", {}).get(field["key"], "")
        is_multiline = state.get("bank") == "alfa"
        suggestions = suggest_fio(current, multiline=is_multiline)
        if not suggestions:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Не удалось подобрать ФИО. Введите вручную."})
            return
        state["_suggestions"] = suggestions
        kb = []
        for i, s in enumerate(suggestions):
            display = s.replace("\n", " ")
            kb.append([{"text": f"✅ {display}", "callback_data": f"sw_pick_{i}"}])
        kb.append([{"text": "✏️ Ввести вручную", "callback_data": "sw_manual"}])
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"💡 Предложения ФИО:\n\nТекущее: {current.replace(chr(10), ' ')}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return

    if data == "sw_suggest_addr":
        field = get_current_field(state)
        if not field:
            return
        current_addr = state.get("current_values", {}).get(field["key"], "")
        bank_name = state.get("bank", "alfa")
        suggestions = suggest_address(current_addr, bank=bank_name)
        if not suggestions:
            tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "Не удалось подобрать адрес. Введите вручную."})
            return
        state["_suggestions"] = suggestions
        kb = []
        for i, s in enumerate(suggestions):
            display = s.replace("\n", ", ")[:60]
            kb.append([{"text": f"✅ {display}", "callback_data": f"sw_pick_{i}"}])
        kb.append([{"text": "✏️ Ввести вручную", "callback_data": "sw_manual"}])
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"💡 Предложения адреса:\n\nТекущий: {current_addr}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
        return

    if data.startswith("sw_pick_"):
        idx_str = data[8:]
        suggestions = state.get("_suggestions", [])
        try:
            idx = int(idx_str)
            picked = suggestions[idx] if idx < len(suggestions) else idx_str
        except (ValueError, IndexError):
            picked = idx_str
        field = get_current_field(state)
        if field:
            state.setdefault("changes", {})[field["key"]] = picked
        state.pop("_suggestions", None)
        advance_field(state)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_manual":
        tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "✏️ Введите значение:"})
        return

    if data.startswith("sw_edit_b"):
        block_num = int(data[-1])
        jump_to_block(state, block_num)
        _sw_send_step(token, chat_id, state, tg_req)
        return

    if data == "sw_generate":
        _sw_generate(token, uid, chat_id, state, tg_req)
        return


def _parse_float_safe(val_str) -> float:
    s = str(val_str).replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        if s.rindex(",") < s.rindex("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _apply_alfa_block3_calc(state, changes, current, out_path,
                            sum_operations_expense, sum_operations_income_alfa,
                            calc_alfa_block3, format_amount_rur, patch_replacements):
    """Apply Block 3 auto-calculation for Alfa statements."""
    from statement_wizard import _parse_amount
    expenses_str = changes.get("расходы")
    income_str = changes.get("поступления")
    if expenses_str:
        expenses = _parse_amount(expenses_str)
    else:
        expenses = sum_operations_expense(state)
    if income_str:
        income = _parse_amount(income_str)
    else:
        income = sum_operations_income_alfa(state)
    bal = _parse_float_safe(changes.get("текущий_баланс", current.get("текущий_баланс", "0")))
    calc = calc_alfa_block3(bal, expenses, income)
    bal_pairs = []
    seen_bal: set[str] = set()
    for calc_key in ("входящий_остаток", "расходы", "поступления", "исходящий_остаток", "платежный_лимит", "текущий_баланс"):
        old_v = current.get(calc_key)
        new_v = format_amount_rur(calc[calc_key])
        if old_v and old_v != new_v:
            pair_key = old_v + " RUR"
            if pair_key not in seen_bal:
                seen_bal.add(pair_key)
                bal_pairs.append((pair_key, new_v + " RUR"))
    if bal_pairs:
        patch_replacements(Path(out_path), Path(out_path), bal_pairs)


def _mutate_doc_id(pdf_bytes: bytes) -> bytes:
    """Mutate PDF /ID to MD5 of content, so each output has a unique document ID."""
    import hashlib
    data = bytearray(pdf_bytes)
    id_m = re.search(rb'/ID\s*\[\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\]', bytes(data))
    if not id_m:
        return bytes(data)
    h = hashlib.md5(bytes(data)).hexdigest().upper()
    new_id = h.encode()
    old1, old2 = id_m.group(1), id_m.group(2)
    full_old = id_m.group(0)
    full_new = full_old.replace(old1, new_id[:len(old1)].ljust(len(old1), b'0'), 1)
    full_new = full_new.replace(old2, new_id[:len(old2)].ljust(len(old2), b'0'), 1)
    data[id_m.start():id_m.end()] = full_new
    return bytes(data)


def _sw_generate(token: str, uid: int, chat_id: int, state: dict, tg_req) -> None:
    """Generate statement PDF from wizard state."""
    from statement_wizard import (
        build_replacement_pairs, sum_operations_expense, sum_operations_income,
        sum_operations_income_alfa,
        calc_alfa_block3, calc_vtb_block3, format_amount_rur, format_amount_rub,
    )
    bank = state.get("bank", "alfa")
    mode = state.get("mode", "")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "⏳ Генерирую выписку..."})

    changes = state.get("changes", {})
    current = state.get("current_values", {})
    raw_extra = state.get("raw_replacements", [])

    if bank == "alfa":
        if mode == "sw_alfa_edit":
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            if not replacement_pairs:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "ℹ️ Нет изменений — файл не модифицирован."})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                _send_main_menu_button(token, chat_id, tg_req)
                return
            in_path = Path(state["file_path"])
            try:
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                patch_replacements(in_path, Path(out_path), replacement_pairs)
                _apply_alfa_block3_calc(state, changes, current, out_path,
                                        sum_operations_expense, sum_operations_income_alfa,
                                        calc_alfa_block3, format_amount_rur, patch_replacements)
                try:
                    from alfa_statement_service import adjust_amount_tm_positions
                    adjust_amount_tm_positions(Path(out_path))
                except Exception:
                    pass
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка патчинга: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            try:
                os.unlink(state["file_path"])
            except OSError:
                pass
            out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
        else:
            try:
                from alfa_statement_service import BASE_PDF
                from cid_patch_amount import patch_replacements
                if not BASE_PDF.exists():
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Шаблон Альфа не найден"})
                    if uid in USER_STATE:
                        del USER_STATE[uid]
                    return
                replacement_pairs = build_replacement_pairs(state) + raw_extra
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if replacement_pairs:
                    patch_replacements(BASE_PDF, Path(out_path), replacement_pairs)
                else:
                    import shutil
                    shutil.copy2(BASE_PDF, out_path)

                _apply_alfa_block3_calc(state, changes, current, out_path,
                                        sum_operations_expense, sum_operations_income_alfa,
                                        calc_alfa_block3, format_amount_rur, patch_replacements)
                try:
                    from alfa_statement_service import adjust_amount_tm_positions
                    adjust_amount_tm_positions(Path(out_path))
                except Exception:
                    pass

                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            out_name = "выписка_альфа.pdf"
    else:
        if mode == "sw_vtb_edit":
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            in_path = Path(state["file_path"])
            try:
                from cid_patch_amount import patch_replacements
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if replacement_pairs:
                    patch_replacements(in_path, Path(out_path), replacement_pairs)
                else:
                    import shutil
                    shutil.copy2(in_path, out_path)
                expenses = sum_operations_expense(state)
                income = sum_operations_income(state)
                bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
                try:
                    bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
                except ValueError:
                    bal = 0.0
                calc = calc_vtb_block3(bal, expenses, income)
                bal_pairs = []
                for calc_key, fmt_key in [("баланс_начало", "баланс_начало"), ("расходные_операции", "расходные_операции"), ("баланс_конец", "баланс_конец")]:
                    old_v = current.get(fmt_key)
                    new_v = format_amount_rub(calc[calc_key])
                    if old_v and old_v != new_v:
                        bal_pairs.append((old_v, new_v))
                if bal_pairs:
                    patch_replacements(Path(out_path), Path(out_path), bal_pairs)
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            try:
                os.unlink(state["file_path"])
            except OSError:
                pass
            out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
        else:
            replacement_pairs = build_replacement_pairs(state) + raw_extra
            try:
                from vyписка_service import BASE_STATEMENT
                from cid_patch_amount import patch_replacements
                if not BASE_STATEMENT.exists():
                    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Шаблон ВТБ не найден (база_выписок/vtb_template.pdf)"})
                    if uid in USER_STATE:
                        del USER_STATE[uid]
                    return
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    out_path = tf.name
                if replacement_pairs:
                    patch_replacements(BASE_STATEMENT, Path(out_path), replacement_pairs)
                else:
                    import shutil
                    shutil.copy2(BASE_STATEMENT, out_path)
                expenses = sum_operations_expense(state)
                income = sum_operations_income(state)
                bal_str = changes.get("баланс_начало", current.get("баланс_начало", "0"))
                try:
                    bal = float(str(bal_str).replace(" ", "").replace("\xa0", "").replace(",", "."))
                except ValueError:
                    bal = 0.0
                calc = calc_vtb_block3(bal, expenses, income)
                bal_pairs = []
                for calc_key in ("баланс_начало", "расходные_операции", "баланс_конец"):
                    old_v = current.get(calc_key)
                    new_v = format_amount_rub(calc[calc_key])
                    if old_v and old_v != new_v:
                        bal_pairs.append((old_v, new_v))
                if bal_pairs:
                    patch_replacements(Path(out_path), Path(out_path), bal_pairs)
                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
            except Exception as e:
                tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                if uid in USER_STATE:
                    del USER_STATE[uid]
                return
            out_name = "выписка_втб.pdf"

    pdf_bytes = _mutate_doc_id(pdf_bytes)

    if uid in USER_STATE:
        del USER_STATE[uid]
    caption = f"✅ Выписка {'Альфа-Банк' if bank == 'alfa' else 'ВТБ'} готова"
    n_changes = len(changes)
    if n_changes:
        caption += f" ({n_changes} изм.)"
    tg_req(token, "sendDocument", {"chat_id": chat_id, "caption": caption},
           files={"document": (out_name, pdf_bytes)})
    _send_main_menu_button(token, chat_id, tg_req)


def _sw_start_alfa_edit_after_upload(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads Alfa statement PDF: scan and start wizard."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    block1 = scan_alfa_block1(Path(path))
    block2_ops = scan_alfa_block2(Path(path))
    block3 = scan_alfa_block3(Path(path))
    state = init_wizard_state("alfa", "sw_alfa_edit", block1, block2_ops, file_path=path, file_name=fname)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Выписка загружена.\n\n📋 Найдено полей: {len(block1)}\n📋 Операций: {len(block2_ops)}\n\nНачинаем пошаговое редактирование:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_alfa_from_check(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads check PDF for Alfa statement: extract and start wizard."""
    from statement_wizard import init_wizard_state
    from receipt_extractor import extract_from_receipt
    from alfa_statement_service import BASE_PDF
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    extracted = extract_from_receipt(Path(path))
    try:
        os.unlink(path)
    except OSError:
        pass
    block1 = scan_alfa_block1(BASE_PDF)
    block2_ops = scan_alfa_block2(BASE_PDF)
    block3 = scan_alfa_block3(BASE_PDF)
    if extracted.get("date"):
        for op in block2_ops:
            op["дата"] = extracted["date"]
    if extracted.get("operation_id"):
        for op in block2_ops:
            op["номер_операции"] = extracted["operation_id"]
    if extracted.get("phone_recipient"):
        for op in block2_ops:
            op["телефон"] = extracted["phone_recipient"]
    if extracted.get("amount"):
        for op in block2_ops:
            op["сумма"] = str(extracted["amount"])
    state = init_wizard_state("alfa", "sw_alfa_check", block1, block2_ops)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    parts = ["📎 Данные извлечены из чека:\n"]
    for k, v in extracted.items():
        if v:
            parts.append(f"  {k}: {v}")
    parts.append("\nНачинаем пошаговое редактирование выписки:")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(parts)})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_alfa_new(token: str, uid: int, chat_id: int, tg_req) -> None:
    """Start Alfa statement from scratch by scanning the Alfa template."""
    from statement_wizard import init_wizard_state
    from alfa_statement_service import BASE_PDF
    from vyписка_service import scan_alfa_block1, scan_alfa_block2, scan_alfa_block3
    block1 = scan_alfa_block1(BASE_PDF)
    block2_ops = scan_alfa_block2(BASE_PDF)
    block3 = scan_alfa_block3(BASE_PDF)
    state = init_wizard_state("alfa", "sw_alfa_new", block1, block2_ops)
    state["current_values"].update(block3)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "🔧 Создание выписки Альфа с нуля\n\nЗаполните все поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_edit_after_upload(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads VTB statement PDF: scan and start wizard."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_vtb_block1, scan_vtb_block2
    block1 = scan_vtb_block1(Path(path))
    block2_ops = scan_vtb_block2(Path(path))
    state = init_wizard_state("vtb", "sw_vtb_edit", block1, block2_ops, file_path=path, file_name=fname)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": f"✅ Выписка ВТБ загружена.\n\n📋 Найдено полей: {len(block1)}\n📋 Операций: {len(block2_ops)}\n\nНачинаем редактирование:"})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_from_check(token: str, uid: int, chat_id: int, path: str, fname: str, tg_req) -> None:
    """After user uploads check PDF for VTB statement: extract and start wizard."""
    from statement_wizard import init_wizard_state
    from receipt_extractor import extract_from_receipt
    from vyписка_service import scan_vtb_block1, scan_vtb_block2, BASE_STATEMENT
    extracted = extract_from_receipt(Path(path))
    try:
        os.unlink(path)
    except OSError:
        pass
    block1 = scan_vtb_block1(BASE_STATEMENT)
    block2_ops = scan_vtb_block2(BASE_STATEMENT)
    if extracted.get("date"):
        for op in block2_ops:
            op["дата"] = extracted["date"]
    if extracted.get("amount"):
        for op in block2_ops:
            op["сумма"] = str(extracted["amount"])
            op["сумма_зачисление"] = str(extracted["amount"])
    if extracted.get("fio_recipient"):
        desc_parts = []
        desc_parts.append("Переводы через СБП.")
        desc_parts.append(f"Перевод денежных средств. {extracted['fio_recipient']}.")
        for op in block2_ops:
            op["описание"] = " ".join(desc_parts)
    state = init_wizard_state("vtb", "sw_vtb_check", block1, block2_ops)
    USER_STATE[uid] = state
    parts = ["📎 Данные из чека для выписки ВТБ:\n"]
    for k, v in extracted.items():
        if v:
            parts.append(f"  {k}: {v}")
    parts.append("\nПошаговое редактирование:")
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "\n".join(parts)})
    _sw_send_step(token, chat_id, state, tg_req)


def _sw_start_vtb_new(token: str, uid: int, chat_id: int, tg_req) -> None:
    """Start VTB statement from scratch by scanning the VTB template."""
    from statement_wizard import init_wizard_state
    from vyписка_service import scan_vtb_block1, scan_vtb_block2, BASE_STATEMENT
    block1 = scan_vtb_block1(BASE_STATEMENT)
    block2_ops = scan_vtb_block2(BASE_STATEMENT)
    state = init_wizard_state("vtb", "sw_vtb_new", block1, block2_ops)
    USER_STATE[uid] = state
    tg_req(token, "sendMessage", {"chat_id": chat_id, "text": "🔧 Создание выписки ВТБ с нуля\n\nЗаполните все поля пошагово:"})
    _sw_send_step(token, chat_id, state, tg_req)


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
        "at_commission": ("2️⃣ Комиссия (сейчас: {commission}):\nВведите новую, напр. 50 RUR\n(комиссия не включается в зачисление)", "at_keep_commission"),
        "at_rate": ("3️⃣ Курс конвертации (сейчас: {rate}):\nВведите новый, напр. 1 RUR = 0.1130 TJS\n✅ Зачисление будет рассчитано автоматически (сумма × курс)", "at_keep_rate"),
        "at_phone": ("4️⃣ Телефон (сейчас: {phone}):", "at_keep_phone"),
        "at_name": ("5️⃣ Получатель (сейчас: {name}):", "at_keep_name"),
        "at_operation_id": ("6️⃣ Номер операции (сейчас: {operation_id}):", "at_keep_opid"),
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
        _send_main_menu_button(token, chat_id, tg_req)
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
            # Check for unsupported chars, but continue anyway with warning
            from vtb_cmap import get_unsupported_chars as _get_bad
            bad = _get_bad(t)
            if bad:
                bad_str = ", ".join(f"«{c}»" for c in sorted(bad))
                send(f"⚠️ Символы недоступны: {bad_str}. Поле будет пропущено. Продолжаем.")
                # Don't save the value, continue to next step
            else:
                fields[field_key] = t

    step += 1
    state["alfa_sbp_step"] = step

    if step < len(field_list):
        _, prompt_next = field_list[step]
        curr_key = field_list[step][0]
        curr_val = state.get("alfa_sbp_current", {}).get(curr_key)
        keep_hint = f"\n📌 Текущее: {curr_val}" if curr_val else ""
        kb = [[{"text": f"⏭ Пропустить", "callback_data": f"alfa_sbp_skip_{step}"}]]
        if curr_val:
            kb[0].insert(0, {"text": f"📌 Оставить: {str(curr_val)[:20]}", "callback_data": f"alfa_sbp_keep_{step}"})
        tg_req(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Принято.\n\nШаг {step + 1}/{len(field_list)}: {prompt_next}{keep_hint}",
            "reply_markup": json.dumps({"inline_keyboard": kb}),
        })
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

        _send_main_menu_button(token, chat_id, tg_req)
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

    # Примечание: at_credited рассчитывается автоматически (сумма × курс) — шаг пропускается
    FLOW = [
        ("at_amount",       "at_commission"),
        ("at_commission",   "at_rate"),
        ("at_rate",         "at_phone"),   # at_credited вычисляется автоматически
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
        # После ввода курса — автоматически вычислить зачисление
        if step_aw == "at_rate" and state.get("at_rate"):
            fields = state.get("at_fields", {})
            try:
                rate_str = state["at_rate"]
                amount_str = state.get("at_amount") or fields.get("amount", "")
                rate_val, currency = parse_rate(rate_str)
                from decimal import Decimal
                amount_num = Decimal(re.sub(r"[^\d.,]", "", amount_str.replace(",", ".")))
                credited_val = amount_num * rate_val
                state["at_credited"] = format_credited(credited_val, currency)
                send(f"✅ Зачисление авто: {state['at_credited']} (сумма × курс, без комиссии)")
            except Exception:
                state["at_credited"] = None
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
        _send_main_menu_button(token, chat_id, tg_req)
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


                    if "document" in msg:
                        doc = msg["document"]
                        fname = doc.get("file_name", "")
                        if not fname.lower().endswith(".pdf"):
                            tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": "❌ Отправьте PDF-файл."})
                            continue
                        state = USER_STATE.get(uid, {})
                        mode = state.get("mode", "")
                        if mode in ("sw_alfa_edit", "sw_vtb_edit") and state.get("step") == "upload":
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
                                if mode == "sw_alfa_edit":
                                    _sw_start_alfa_edit_after_upload(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                else:
                                    _sw_start_vtb_edit_after_upload(token, uid, msg["chat"]["id"], path, fname, tg_request)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка сканирования: {e}"})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                            continue
                        if mode in ("sw_alfa_check", "sw_vtb_check") and state.get("step") == "upload":
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
                                if mode == "sw_alfa_check":
                                    _sw_start_alfa_from_check(token, uid, msg["chat"]["id"], path, fname, tg_request)
                                else:
                                    _sw_start_vtb_from_check(token, uid, msg["chat"]["id"], path, fname, tg_request)
                            except Exception as e:
                                tg_request(token, "sendMessage", {"chat_id": msg["chat"]["id"], "text": f"❌ Ошибка: {e}"})
                                try:
                                    os.unlink(path)
                                except OSError:
                                    pass
                                if uid in USER_STATE:
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
                            "text": "📎 Чек получен. Выберите банк:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏦 Альфа Чек", "callback_data": "bank_alfa_sub"},
                                        {"text": "🏛 ВТБ", "callback_data": "bank_vtb_sub"},
                                    ],
                                    [{"text": "🔍 Авто — любой банк", "callback_data": "bank_auto_free"}],
                                    [{"text": "❌ Отмена", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue

                    # Альфа СБП все поля: пошаговый ввод
                    if uid in USER_STATE and USER_STATE[uid].get("mode") == "alfa_sbp_full":
                        _handle_alfa_sbp_input(token, uid, chat_id, text, tg_request)
                        continue

                    # Пошаговый визард выписки (sw_alfa_*, sw_vtb_*)
                    if uid in USER_STATE and USER_STATE[uid].get("mode", "").startswith("sw_"):
                        _sw_handle_text(token, uid, chat_id, text, tg_request)
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

                    # Авто-режим: пользователь вводит пары замен (старое = новое)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "auto_replacements":
                        state = USER_STATE[uid]
                        pairs = parse_auto_replacements(text)
                        if not pairs:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Не найдено пар замен.\nФормат: (старое = новое)\nНапример: (10 RUR = 10 000 RUR)"})
                            continue
                        inp = state["file_path"]
                        out_stem = _increment_filename(Path(state["file_name"]).stem)
                        out_name = out_stem + ".pdf"
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"⏳ Применяю {len(pairs)} замен(ы)..."})
                        try:
                            from cid_patch_amount import patch_replacements
                            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                                out_path = tf.name
                            ok = patch_replacements(Path(inp), Path(out_path), pairs)
                            try:
                                os.unlink(inp)
                            except OSError:
                                pass
                            del USER_STATE[uid]
                            if ok:
                                with open(out_path, "rb") as f:
                                    pdf_bytes = f.read()
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                summary = "\n".join(f"  {a} → {b}" for a, b in pairs[:5])
                                if len(pairs) > 5:
                                    summary += f"\n  ... и ещё {len(pairs)-5}"
                                caption = f"✅ Авто-замены:\n{summary}\n\n⚠️ Режим Авто не гарантирует прохождение проверки."
                                tg_request(token, "sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (out_name, pdf_bytes)})
                            else:
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Не удалось применить замены. Проверьте что значения совпадают с текстом в PDF."})
                        except Exception as e:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": f"❌ Ошибка: {e}"})
                        _send_main_menu_button(token, chat_id, tg_request)
                        continue

                    # Новый ввод суммы: только новое значение (auto-detect текущего)
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_new_only":
                        state = USER_STATE[uid]
                        nums = re.findall(r"\d+", text.strip())
                        if not nums:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите новую сумму числом (например: 5000)"})
                            continue
                        amount_to = int("".join(nums))
                        if amount_to <= 0:
                            tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Сумма должна быть больше 0."})
                            continue
                        state["amount_to"] = amount_to
                        commission = state.get("current_commission")
                        if commission and commission > 0:
                            state["awaiting"] = "amount_commission"
                            tg_request(token, "sendMessage", {
                                "chat_id": chat_id,
                                "text": f"💳 Обнаружена комиссия: {format_amount_display(commission)} ₽\nВведите новую комиссию или — чтобы пропустить:",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "— Пропустить", "callback_data": "amount_skip_commission"}]]}),
                            })
                        else:
                            _do_amount_patch(token, uid, chat_id, state, tg_request)
                        continue

                    # Ввод новой комиссии
                    if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_commission":
                        state = USER_STATE[uid]
                        t = text.strip()
                        if t in ("-", "—", "пропустить", "skip"):
                            state["commission_to"] = None
                        else:
                            nums = re.findall(r"\d+", t)
                            state["commission_to"] = int("".join(nums)) if nums else None
                        _do_amount_patch(token, uid, chat_id, state, tg_request)
                        continue

                    if uid in USER_STATE and "bank" in USER_STATE[uid]:
                        # Fallback: legacy "FROM TO" format (shouldn't normally reach here)
                        tg_request(token, "sendMessage", {"chat_id": chat_id, "text": "❌ Введите новую сумму (одно число, например: 5000)."})
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
                    if q["data"] == "main_check":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📁 Чек\n\nЗагрузите PDF или сгенерируйте из базы:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "📄 Загрузить чек", "callback_data": "main_new"},
                                        {"text": "✨ Сгенерировать чек", "callback_data": "main_generate"},
                                    ],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
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
                                    [{"text": "⬅️ Назад", "callback_data": "main_check"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_type_sbp":
                        USER_STATE[uid] = {"awaiting": "gen_subtype", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": None}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✨ Перевод по СБП\n\nВыберите тип чека:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏛 ВТБ СБП", "callback_data": "gen_subtype_sbp"},
                                        {"text": "🏦 Альфа СБП", "callback_data": "gen_subtype_alfa_sbp"},
                                    ],
                                    [{"text": "🏛 ВТБ на ВТБ", "callback_data": "gen_subtype_vtb_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_generate"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "gen_subtype_alfa_sbp":
                        # Проверяем базу Альфа
                        alfa_base = _BOT_DIR / "база_чеков" / "alfa"
                        alfa_files = list(alfa_base.glob("*.pdf")) if alfa_base.exists() else []
                        if not alfa_files:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "⚠️ База Альфа пуста.\n\nДобавьте донорские чеки Альфа-Банк через раздел «База».",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "📂 Открыть Базу", "callback_data": "main_db"}],
                                    [{"text": "⬅️ Назад", "callback_data": "gen_type_sbp"}],
                                ]}),
                            })
                        else:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"🏦 Альфа СБП\n\nВ базе {len(alfa_files)} чек(ов).\n⚠️ Функция генерации Альфа СБП в разработке.",
                                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "gen_type_sbp"}]]}),
                            })
                        continue
                    if q["data"] == "gen_subtype_sbp":
                        USER_STATE[uid] = {"awaiting": "gen_bank", "gen_transfer_type": "sbp", "gen_bank_type": None, "gen_vtb_subtype": "vtb_sbp"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "✨ ВТБ СБП\n\n"
                                f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                                "Выберите банк:"
                            ),
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "🏛 ВТБ", "callback_data": "gen_bank_vtb"}],
                                    [{"text": "⬅️ Назад", "callback_data": "gen_type_sbp"}],
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
                        # Подсчёт шаблонов выписок
                        stmt_base = _BOT_DIR / "база_выписок"
                        n_alfa_stmt = len(list(stmt_base.glob("*.pdf"))) if stmt_base.exists() else 0
                        vtb_tpl = stmt_base / "vtb_template.pdf"
                        n_vtb_stmt = 1 if vtb_tpl.exists() else 0
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                f"📂 База\n\n"
                                f"Чеки:\n"
                                f"  СБП: {n_sbp} | ВТБ→ВТБ: {n_vtb} | Альфа: {n_alfa}\n\n"
                                f"Шаблоны выписок:\n"
                                f"  Альфа: {n_alfa_stmt} | ВТБ: {n_vtb_stmt}\n\n"
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
                                "📄 Загрузить чек\n\n"
                                "Отправьте PDF-чек — бот определит сумму автоматически.\n\n"
                                "После загрузки выберите банк и режим замены."
                            ),
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "main_check"}]]}),
                        })
                        continue
                    if q["data"] == "main_stmt":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📋 Создать выписку\n\nВыберите банк:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [
                                        {"text": "🏦 Альфа Банк", "callback_data": "stmt_alfa_menu"},
                                        {"text": "🏛 ВТБ", "callback_data": "stmt_vtb_menu"},
                                    ],
                                    [{"text": "⬅️ Назад", "callback_data": "main_back"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "stmt_alfa_menu":
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Выписка Альфа-Банк\n\nВыберите вариант:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_alfa_edit"}],
                                    [{"text": "📄 Выписка по чеку", "callback_data": "stmt_alfa_from_check"}],
                                    [{"text": "🔧 Создание с нуля", "callback_data": "stmt_alfa_new"}],
                                    [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "stmt_vtb_menu":
                        vtb_tpl = _BOT_DIR / "база_выписок" / "vtb_template.pdf"
                        if not vtb_tpl.exists():
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 Выписка ВТБ\n\n⚠️ Шаблон выписки ВТБ не найден.\n\nДобавьте файл vtb_template.pdf в папку база_выписок/ и обновите индекс в разделе «База».",
                                "reply_markup": json.dumps({"inline_keyboard": [
                                    [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                ]}),
                            })
                        else:
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 Выписка ВТБ\n\nВыберите вариант:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "✏️ Редактирование своей выписки", "callback_data": "stmt_vtb_edit"}],
                                        [{"text": "📄 Выписка по чеку", "callback_data": "stmt_vtb_from_check"}],
                                        [{"text": "🔧 Создание с нуля", "callback_data": "stmt_vtb_new"}],
                                        [{"text": "⬅️ Назад", "callback_data": "main_stmt"}],
                                    ],
                                }),
                            })
                        continue
                    if q["data"] == "stmt_vtb_edit":
                        USER_STATE[uid] = {"mode": "sw_vtb_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование выписки ВТБ\n\nОтправьте PDF-файл вашей выписки ВТБ.\n\nБот отсканирует все поля и предложит пошаговое редактирование.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_vtb_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_vtb_from_check":
                        USER_STATE[uid] = {"mode": "sw_vtb_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка ВТБ по чеку\n\nОтправьте PDF-файл чека — данные будут перенесены в выписку ВТБ.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_vtb_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_vtb_new":
                        _sw_start_vtb_new(token, uid, q["message"]["chat"]["id"], tg_request)
                        continue
                    if q["data"] == "stmt_alfa_edit":
                        USER_STATE[uid] = {"mode": "sw_alfa_edit", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "✏️ Редактирование выписки Альфа\n\nОтправьте PDF-файл вашей выписки.\n\nБот отсканирует все 3 блока и предложит пошаговое редактирование каждого поля.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_alfa_from_check":
                        USER_STATE[uid] = {"mode": "sw_alfa_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Выписка по чеку (Альфа)\n\nОтправьте PDF-файл чека — данные будут автоматически перенесены в выписку Альфа-Банка.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"] == "stmt_alfa_new":
                        _sw_start_alfa_new(token, uid, q["message"]["chat"]["id"], tg_request)
                        continue
                    if q["data"] in ("stmt_edit", "stmt_receipt"):
                        USER_STATE[uid] = {"mode": "sw_alfa_edit" if q["data"] == "stmt_edit" else "sw_alfa_check", "step": "upload"}
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "📄 Отправьте PDF-файл.",
                            "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "stmt_alfa_menu"}]]}),
                        })
                        continue
                    if q["data"].startswith("sw_"):
                        _sw_handle_callback(token, uid, q, tg_request)
                        continue
                    if q["data"] == "main_back":
                        if uid in USER_STATE:
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
                    if q["data"] == "vtb_mode_amount":
                        # redirect to new flow
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏛 ВТБ — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "vtb_mode_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["vtb_mode"] = "full"
                        USER_STATE[uid]["awaiting"] = "vtb_amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        amt_hint = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n" if current_amt else ""
                        txt = (
                            "🏛 ВТБ — все поля\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            f"{amt_hint}"
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
                    # Альфа СБП: пропустить/оставить текущий шаг
                    if q["data"].startswith("alfa_sbp_skip_") or q["data"].startswith("alfa_sbp_keep_"):
                        if uid not in USER_STATE or USER_STATE[uid].get("mode") != "alfa_sbp_full":
                            tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "❌ Сессия истекла"})
                            continue
                        state = USER_STATE[uid]
                        cid = q["message"]["chat"]["id"]
                        is_keep = q["data"].startswith("alfa_sbp_keep_")
                        step_idx = int(q["data"].split("_")[-1])
                        field_list = state.get("_alfa_sbp_field_list", [])
                        if step_idx < len(field_list):
                            field_key = field_list[step_idx][0]
                            if is_keep:
                                curr_val = state.get("alfa_sbp_current", {}).get(field_key)
                                if curr_val:
                                    fields = state.setdefault("alfa_sbp_fields", {})
                                    if field_key == "account":
                                        fields["account_last4"] = str(curr_val)[-4:]
                                    else:
                                        fields[field_key] = curr_val
                        # Advance step
                        state["alfa_sbp_step"] = step_idx + 1
                        tg_request(token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "✅"})
                        if step_idx + 1 < len(field_list):
                            _, prompt_next = field_list[step_idx + 1]
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": f"Шаг {step_idx+2}/{len(field_list)}: {prompt_next}\nОтправьте - чтобы пропустить."})
                        else:
                            tg_request(token, "sendMessage", {"chat_id": cid, "text": "⏳ Генерирую PDF..."})
                            _run_alfa_sbp_full_patch(token, uid, cid, state, tg_request)
                        continue
                    # Альфа СБП все поля: старый callback (для совместимости)
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
                                "🏦 Альфа — все поля\n\n"
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
                        # at_credited теперь вычисляется автоматически — пропускаем этот шаг
                        keep_flow = {
                            "at_keep_amount": ("at_amount", "at_commission"),
                            "at_keep_commission": ("at_commission", "at_rate"),
                            "at_keep_rate": ("at_rate", "at_phone"),  # пропускаем at_credited
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
                    # Новые обработчики банка
                    if q["data"] == "bank_alfa_sub":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": "🏦 Альфа Чек\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "bank_alfa_amount"}],
                                    [{"text": "📋 Все поля", "callback_data": "bank_alfa_full"}],
                                    [{"text": "🌐 Трансгран", "callback_data": "bank_alfa_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "bank_vtb_sub":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
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
                            "text": f"🏛 ВТБ{scan_tip}\n\nВыберите режим:",
                            "reply_markup": json.dumps({
                                "inline_keyboard": [
                                    [{"text": "💰 Только сумма", "callback_data": "bank_vtb_amount"}],
                                    [{"text": "📋 Все поля", "callback_data": "bank_vtb_full"}],
                                    [{"text": "🌍 Трансгран", "callback_data": "bank_vtb_transgran"}],
                                    [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                ],
                            }),
                        })
                        continue
                    if q["data"] == "bank_auto_free":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "auto"
                        USER_STATE[uid]["awaiting"] = "auto_replacements"
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": (
                                "🔍 Авто — любой банк\n\n"
                                "Введите замены в формате:\n"
                                "(старое = новое)\n\n"
                                "Пример:\n(10 RUR = 10 000 RUR)\n(01.01.2025 = 20.03.2026)\n\n"
                                "⚠️ Авто-режим не гарантирует прохождение проверки."
                            ),
                        })
                        continue
                    if q["data"] == "bank_alfa_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "alfa"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        current_comm = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = current_comm
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏦 Альфа — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "bank_vtb_amount":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "amount"
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        current_comm = extract_commission_from_pdf(inp)
                        USER_STATE[uid]["current_amount"] = current_amt
                        USER_STATE[uid]["current_commission"] = current_comm
                        USER_STATE[uid]["awaiting"] = "amount_new_only"
                        amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": f"🏛 ВТБ — только сумма\n\n{amt_txt}Введите новую сумму:",
                        })
                        continue
                    if q["data"] == "bank_alfa_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        # Same as old bank_alfa_sbp_full
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
                                "🏦 Альфа — все поля\n\n"
                                "Введите новые значения пошагово.\n"
                                "Отправьте - чтобы оставить поле без изменений.\n\n"
                                f"Шаг 1/{len(_ALFA_SBP_FIELDS)}: {prompt}"
                            ),
                        })
                        continue
                    if q["data"] == "bank_vtb_full":
                        if uid not in USER_STATE:
                            tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                            continue
                        USER_STATE[uid]["bank"] = "vtb"
                        USER_STATE[uid]["vtb_mode"] = "full"
                        USER_STATE[uid]["awaiting"] = "vtb_amount"
                        # Extract current values
                        inp = USER_STATE[uid]["file_path"]
                        try:
                            from receipt_db import get_receipt_amount
                            current_amt = get_receipt_amount(Path(inp))
                        except Exception:
                            current_amt = None
                        amt_hint = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n" if current_amt else ""
                        txt = (
                            "🏛 ВТБ — все поля\n\n"
                            f"{VTB_UNSUPPORTED_NOTICE}\n\n"
                            f"{amt_hint}"
                            "1️⃣ Сумма: с какой на какую (например: 10 1000)"
                        )
                        tg_request(token, "editMessageText", {
                            "chat_id": q["message"]["chat"]["id"],
                            "message_id": q["message"]["message_id"],
                            "text": txt,
                        })
                        continue
                    if q["data"] == "amount_skip_commission":
                        if uid in USER_STATE and USER_STATE[uid].get("awaiting") == "amount_commission":
                            USER_STATE[uid]["commission_to"] = None
                            _do_amount_patch(token, uid, q["message"]["chat"]["id"], USER_STATE[uid], tg_request)
                        continue

                    if uid not in USER_STATE:
                        tg_request(token, "editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "❌ Чек не найден. Отправьте PDF заново."})
                        continue
                    # Legacy bank callbacks (for backwards compat)
                    bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_auto": "auto"}
                    if q["data"] in bank_map:
                        bank = bank_map[q["data"]]
                        USER_STATE[uid]["bank"] = bank
                        if bank == "vtb":
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": "🏛 ВТБ\n\nВыберите режим:",
                                "reply_markup": json.dumps({
                                    "inline_keyboard": [
                                        [{"text": "💰 Только сумма", "callback_data": "bank_vtb_amount"}],
                                        [{"text": "📋 Все поля", "callback_data": "bank_vtb_full"}],
                                        [{"text": "⬅️ Назад", "callback_data": "cancel"}],
                                    ],
                                }),
                            })
                        else:
                            # Old alfa/auto: trigger amount_new_only flow
                            inp = USER_STATE[uid]["file_path"]
                            try:
                                from receipt_db import get_receipt_amount
                                current_amt = get_receipt_amount(Path(inp))
                            except Exception:
                                current_amt = None
                            USER_STATE[uid]["current_amount"] = current_amt
                            USER_STATE[uid]["current_commission"] = extract_commission_from_pdf(inp)
                            USER_STATE[uid]["awaiting"] = "amount_new_only"
                            amt_txt = f"Текущая сумма: {format_amount_display(current_amt)} ₽\n\n" if current_amt else ""
                            tg_request(token, "editMessageText", {
                                "chat_id": q["message"]["chat"]["id"],
                                "message_id": q["message"]["message_id"],
                                "text": f"Введите новую сумму:\n\n{amt_txt}",
                            })
                        continue
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
