import os
import csv
import io
import json
import html
import re
import sqlite3
import zipfile
import tempfile
import threading
import traceback
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL", "https://tg-catalog-bot-10.onrender.com").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CSV_FILE = os.getenv("CSV_FILE", "Telegramååå¯¼å¥è¡¨_93ä¸ª_å¯ç´æ¥å¯¼å¥.csv").strip()
SERVICE_USERNAME = os.getenv("SERVICE_USERNAME", "niubanggu").strip().lstrip("@")
BOT_USERNAME = os.getenv("BOT_USERNAME", "shangpinmulu2026_bot").strip().lstrip("@")

CATEGORIES = {
    "c1": "åå¤©ä¸ç³»å",
    "c2": "çç«ç³»å",
    "c3": "åäº¬ç³»å",
    "c4": "è·è±ç³»å",
    "c5": "èèçç³»å",
    "c6": "ç¡ä¸¹ç³»å",
    "c7": "é»éå¶ç³»å",
    "c8": "èçç³»å",
    "c9": "å©ç¾¤ç³»å",
    "c10": "é»é¹¤æ¥¼ç³»å",
    "c11": "ä¸­åç³»å",
    "c12": "ç½ç®ç³»å",
}


def parse_admin_ids(raw: str):
    parts = re.split(r"[,;\s]+", raw.strip()) if raw.strip() else []
    ids = []
    for item in parts:
        if not item:
            continue
        if item.isdigit():
            ids.append(int(item))
    return ids


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")
DB_PATH = BASE_DIR / "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("ç¼ºå°ç¯å¢åé BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("ç¼ºå°ææç ADMIN_IDSãè¯·å¡«å Telegram æ°å­ç¨æ·IDï¼ä¸è¦å¡«å @ç¨æ·å")


# -------------------- æ°æ®åº --------------------

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def db_connect():
    if USE_POSTGRES:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as exc:
            raise RuntimeError("ä½¿ç¨ PostgreSQL æ¶å¿é¡»å®è£ psycopg2-binary") from exc
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    try:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT,
                    category TEXT,
                    price TEXT,
                    stock INTEGER DEFAULT 0,
                    description TEXT,
                    active INTEGER DEFAULT 1,
                    photo_id TEXT,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inquiries (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    username TEXT,
                    product TEXT,
                    message TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT
                )
            """)
            for col, definition in [
                ("code", "TEXT"), ("category", "TEXT"), ("price", "TEXT"),
                ("stock", "INTEGER DEFAULT 0"), ("description", "TEXT"),
                ("active", "INTEGER DEFAULT 1"), ("photo_id", "TEXT"),
                ("created_at", "TEXT"),
            ]:
                cur.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col} {definition}")
            for col, definition in [
                ("user_id", "TEXT"), ("username", "TEXT"), ("product", "TEXT"),
                ("message", "TEXT"), ("status", "TEXT DEFAULT 'pending'"),
                ("created_at", "TEXT"),
            ]:
                cur.execute(f"ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS {col} {definition}")
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT,
                    category TEXT,
                    price TEXT,
                    stock INTEGER DEFAULT 0,
                    description TEXT,
                    active INTEGER DEFAULT 1,
                    photo_id TEXT,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    username TEXT,
                    product TEXT,
                    message TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT
                )
            """)
            cur.execute("PRAGMA table_info(products)")
            existing = {row[1] for row in cur.fetchall()}
            for col, definition in [
                ("code", "TEXT"), ("category", "TEXT"), ("price", "TEXT"),
                ("stock", "INTEGER DEFAULT 0"), ("description", "TEXT"),
                ("active", "INTEGER DEFAULT 1"), ("photo_id", "TEXT"),
                ("created_at", "TEXT"),
            ]:
                if col not in existing:
                    cur.execute(f"ALTER TABLE products ADD COLUMN {col} {definition}")
            cur.execute("PRAGMA table_info(inquiries)")
            existing_i = {row[1] for row in cur.fetchall()}
            for col, definition in [
                ("user_id", "TEXT"), ("username", "TEXT"), ("product", "TEXT"),
                ("message", "TEXT"), ("status", "TEXT DEFAULT 'pending'"),
                ("created_at", "TEXT"),
            ]:
                if col not in existing_i:
                    cur.execute(f"ALTER TABLE inquiries ADD COLUMN {col} {definition}")
        conn.commit()
    finally:
        conn.close()


def fetchall(sql, params=()):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) if not isinstance(r, dict) else r for r in rows]
    finally:
        conn.close()


def fetchone(sql, params=()):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row) if not isinstance(row, dict) else row
    finally:
        conn.close()


def execute(sql, params=(), returning=False):
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        result = None
        if returning:
            row = cur.fetchone()
            if row:
                result = dict(row) if not isinstance(row, dict) else row
        elif not USE_POSTGRES:
            result = cur.lastrowid
        conn.commit()
        return result
    finally:
        conn.close()


def db_placeholder():
    return "%s" if USE_POSTGRES else "?"


def product_count():
    row = fetchone("SELECT COUNT(*) AS n FROM products")
    return int(row["n"] or 0)


def product_get(product_id):
    p = db_placeholder()
    return fetchone(f"SELECT * FROM products WHERE id={p}", (product_id,))


def product_create(name, code, category, price, stock, description, active=1, photo_id=None):
    created = now_text()
    if USE_POSTGRES:
        row = execute(
            """INSERT INTO products
               (name,code,category,price,stock,description,active,photo_id,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (name, code, category, price, stock, description, active, photo_id, created),
            returning=True,
        )
        return int(row["id"])
    return int(execute(
        """INSERT INTO products
           (name,code,category,price,stock,description,active,photo_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (name, code, category, price, stock, description, active, photo_id, created),
    ))


def product_update(product_id, name, code, category, price, stock, description):
    p = db_placeholder()
    execute(
        f"""UPDATE products SET name={p},code={p},category={p},price={p},stock={p},description={p}
            WHERE id={p}""",
        (name, code, category, price, stock, description, product_id),
    )


def product_set_photo(product_id, photo_id):
    p = db_placeholder()
    execute(f"UPDATE products SET photo_id={p} WHERE id={p}", (photo_id, product_id))


def product_set_stock(product_id, stock):
    p = db_placeholder()
    execute(f"UPDATE products SET stock={p} WHERE id={p}", (stock, product_id))


def bulk_update_stock_from_csv(path):
    """æååç¼å·æ¹éæ´æ°åºå­ãæ¯æ UTF-8/UTF-8-SIG/GB18030ã

    CSV è³å°éè¦ä¸¤åï¼code,stockã
    ä¹å¼å®¹å®æ´åå CSVï¼åªè¯»å code å stockï¼ä¸ä¿®æ¹å¶ä»å­æ®µã
    å¨é¨æ ¡éªéè¿åæä¸æ¬¡æ§æäº¤ï¼é¿åæ´æ°å°ä¸åå¤±è´¥ã
    """
    data = Path(path).read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise ValueError("æ æ³è¯å« CSV ç¼ç ï¼è¯·ä¿å­ä¸º UTF-8 CSV")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV æ²¡æè¡¨å¤´")

    fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
    if "code" not in fields or "stock" not in fields:
        raise ValueError("åºå­ CSV å¿é¡»åå«ä¸¤åï¼code,stock")

    rows = []
    seen = set()
    for line_no, raw in enumerate(reader, start=2):
        code = str(raw.get(fields["code"], "") or "").strip().upper()
        stock_text = str(raw.get(fields["stock"], "") or "").strip()
        # å¿½ç¥å®å¨ç©ºç½è¡
        if not code and not stock_text:
            continue
        if not code:
            raise ValueError(f"ç¬¬ {line_no} è¡ååç¼å·ä¸ºç©º")
        if code in seen:
            raise ValueError(f"ç¬¬ {line_no} è¡ååç¼å·éå¤ï¼{code}")
        seen.add(code)
        try:
            if not re.fullmatch(r"\d+", stock_text):
                raise ValueError
            stock = int(stock_text)
        except ValueError:
            raise ValueError(f"ç¬¬ {line_no} è¡åºå­æ æï¼{stock_text!r}ï¼å¿é¡»æ¯ 0 æä»¥ä¸æ´æ°")
        rows.append((code, stock))

    if not rows:
        raise ValueError("CSV æ²¡æå¯æ´æ°çåºå­æ°æ®")

    conn = db_connect()
    try:
        cur = conn.cursor()
        ph = "%s" if USE_POSTGRES else "?"
        missing = []
        product_ids = []
        for code, stock in rows:
            cur.execute(f"SELECT id FROM products WHERE UPPER(code)={ph} ORDER BY id LIMIT 1", (code,))
            row = cur.fetchone()
            if row is None:
                missing.append(code)
            else:
                pid = row["id"] if isinstance(row, dict) else row[0]
                product_ids.append((pid, stock, code))

        if missing:
            raise ValueError("æ¾ä¸å°ååç¼å·ï¼" + ", ".join(missing[:20]) + (" ç­" if len(missing) > 20 else ""))

        for pid, stock, code in product_ids:
            cur.execute(f"UPDATE products SET stock={ph} WHERE id={ph}", (stock, pid))

        conn.commit()
        return len(product_ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def product_set_active(product_id, active):
    p = db_placeholder()
    execute(f"UPDATE products SET active={p} WHERE id={p}", (active, product_id))


def product_delete(product_id):
    p = db_placeholder()
    execute(f"DELETE FROM products WHERE id={p}", (product_id,))


def normalize_category(value):
    """æ¯æ c1-c12 å CSV ä¸­çä¸­æåç±»åç§°ã"""
    raw = str(value or "").strip()
    if raw in CATEGORIES:
        return raw
    for code, name in CATEGORIES.items():
        if raw == name or raw == name.replace("ç³»å", ""):
            return code
    old_map = {
        "åç±»ä¸": "c1", "åç±»äº": "c2", "åç±»ä¸": "c3", "åç±»å": "c4",
        "åç±»äº": "c5", "åç±»å­": "c6", "åç±»ä¸": "c7", "åç±»å«": "c8",
        "åç±»ä¹": "c9", "åç±»å": "c10", "åç±»åä¸": "c11", "åç±»åäº": "c12",
    }
    return old_map.get(raw, "")


def product_upsert(row, photo_id=None):
    name = str(row.get("name", "")).strip()
    code = str(row.get("code", "")).strip()
    category = normalize_category(row.get("category", ""))
    price = str(row.get("price", "")).strip()
    description = str(row.get("description", "") or "").strip()
    try:
        stock = int(float(str(row.get("stock", 0)).strip() or 0))
    except ValueError:
        stock = 0
    if not name:
        raise ValueError("CSV ä¸­å­å¨ç©ºåååç§°")
    if category not in CATEGORIES:
        raise ValueError(f"åç±» {category} æ æï¼åºä¸º c1-c12")
    existing = None
    if code:
        p = db_placeholder()
        existing = fetchone(f"SELECT * FROM products WHERE code={p} ORDER BY id LIMIT 1", (code,))
    if existing is None:
        pid = product_create(name, code, category, price, stock, description, 1, photo_id)
    else:
        pid = int(existing["id"])
        p = db_placeholder()
        if photo_id is None:
            execute(
                f"""UPDATE products SET name={p},category={p},price={p},stock={p},description={p},active=1
                    WHERE id={p}""",
                (name, category, price, stock, description, pid),
            )
        else:
            execute(
                f"""UPDATE products SET name={p},category={p},price={p},stock={p},description={p},active=1,photo_id={p}
                    WHERE id={p}""",
                (name, category, price, stock, description, photo_id, pid),
            )
    return pid


# -------------------- CSV --------------------

def read_csv_rows(path):
    encodings = ["utf-8-sig", "utf-8", "gb18030"]
    last_error = None
    text = None
    for enc in encodings:
        try:
            text = Path(path).read_text(encoding=enc)
            break
        except Exception as exc:
            last_error = exc
    if text is None:
        raise ValueError(f"æ æ³è¯»å CSVï¼{last_error}")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV æ²¡æè¡¨å¤´")
    fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
    required = ["name", "code", "category", "price", "stock"]
    missing = [x for x in required if x not in fields]
    if missing:
        raise ValueError("CSV ç¼ºå°å­æ®µï¼" + ", ".join(missing))
    rows = []
    for raw in reader:
        row = {}
        for key, original in fields.items():
            row[key] = raw.get(original, "")
        rows.append(row)
    return rows


def import_csv_file(path):
    rows = read_csv_rows(path)
    for row in rows:
        product_upsert(row)
    return len(rows)


# -------------------- Telegram è¾å© --------------------

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def cat_name(code):
    return CATEGORIES.get(code, code or "æªåç±»")


def product_text(product, admin=False):
    status = "ð¢ ä¸æ¶" if int(product.get("active", 0) or 0) else "ð´ ä¸æ¶"
    photo = "æå¾ç" if product.get("photo_id") else "æ å¾ç"
    text = (
        f"ð¦ {product.get('name', '')}\n"
        f"ç¼å·ï¼{product.get('code') or '-'}\n"
        f"åç±»ï¼{cat_name(product.get('category'))}\n"
        f"ä»·æ ¼ï¼{product.get('price') or 'é¢è®®'}\n"
        f"åºå­ï¼{product.get('stock', 0)}\n"
        f"ç¶æï¼{status}\n"
        f"å¾çï¼{photo}\n"
    )
    if product.get("description"):
        text += f"æè¿°ï¼{product['description']}\n"
    if admin:
        text += f"IDï¼{product.get('id')}"
    return text


def main_menu(user_id):
    rows = [
        [InlineKeyboardButton("ð ååç®å½", callback_data="catalog")],
        [InlineKeyboardButton("ð æç´¢åå", callback_data="search")],
        [InlineKeyboardButton("ð å¨çº¿ååç®å½", web_app=WebAppInfo(url=WEB_URL))],
        [InlineKeyboardButton("ð¬ å®¢æç´è¾¾", url=f"https://t.me/{SERVICE_USERNAME}")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("âï¸ ç®¡çåå°", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def catalog_keyboard():
    rows = []
    keys = list(CATEGORIES.items())
    for i in range(0, len(keys), 2):
        pair = keys[i:i + 2]
        rows.append([InlineKeyboardButton(v, callback_data=f"cat:{k}") for k, v in pair])
    rows.append([InlineKeyboardButton("ð¦ å¨é¨åå", callback_data="all_products")])
    rows.append([InlineKeyboardButton("â¬ï¸ è¿å", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def products_keyboard(products, prefix="prod:", back="catalog"):
    rows = []
    for p in products:
        label = f"{p['name']} [{p.get('code') or '-'}]"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"{prefix}{p['id']}")])
    rows.append([InlineKeyboardButton("â¬ï¸ è¿å", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ð¦ ååç®¡ç", callback_data="admin_products:0"), InlineKeyboardButton("â æ·»å åå", callback_data="admin_add")],
        [InlineKeyboardButton("ð¨ è¯¢ä»·è®°å½", callback_data="admin_inquiries:0"), InlineKeyboardButton("ð ç³»ç»ç¶æ", callback_data="admin_status")],
        [InlineKeyboardButton("ð¦ ä¸é®ä¸ä¼ åå+å¾ç", callback_data="admin_zip"), InlineKeyboardButton("ð¥ åªå¯¼å¥CSV", callback_data="admin_csv")],
        [InlineKeyboardButton("ð ä¸é®æ¹éæ¹åºå­", callback_data="bulk_stock" )],
        [InlineKeyboardButton("ð¢ å¨é¨ä¸æ¶", callback_data="bulk_on"), InlineKeyboardButton("ð´ å¨é¨ä¸æ¶", callback_data="bulk_off")],
        [InlineKeyboardButton("â¬ï¸ è¿åä¸»èå", callback_data="home")],
    ])


def product_share_url(product):
    pid = int(product["id"])
    share_url = f"https://t.me/{BOT_USERNAME}?start=product_{pid}"
    share_text = f"ð¦ {product.get('name', '')}\nç¼å·ï¼{product.get('code') or '-'}\nç¹å»æ¥çååè¯¦æ"
    return (
        "https://t.me/share/url?url="
        + quote(share_url, safe="")
        + "&text="
        + quote(share_text, safe="")
    )


def admin_product_keyboard(product):
    pid = product["id"]
    active = int(product.get("active", 0) or 0)
    toggle_text = "ð´ ä¸æ¶" if active else "ð¢ ä¸æ¶"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("âï¸ ç¼è¾åå", callback_data=f"edit:{pid}"), InlineKeyboardButton("ð¦ ä¿®æ¹åºå­", callback_data=f"stock:{pid}")],
        [InlineKeyboardButton("ð¼ï¸ æ´æ¢å¾ç", callback_data=f"photo:{pid}"), InlineKeyboardButton(toggle_text, callback_data=f"toggle:{pid}")],
        [InlineKeyboardButton("ð¤ åäº«åå", url=product_share_url(product))],
        [InlineKeyboardButton("ðï¸ å é¤åå", callback_data=f"delete:{pid}")],
        [InlineKeyboardButton("â¬ï¸ ååç®¡ç", callback_data="admin_products:0")],
    ])


def clear_state(context):
    for key in [
        "state", "pending_product_id", "edit_product_id", "stock_product_id",
        "photo_product_id", "inquiry_product_id", "zip_import", "bulk_stock_import",
    ]:
        context.user_data.pop(key, None)


async def safe_delete_message(message):
    try:
        await message.delete()
    except Exception:
        pass


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            pass


async def send_product_to_user(target, product):
    """åéååè¯¦æï¼target å¯ä»¥æ¯ Update æ Messageã"""
    text = product_text(product)
    pid = int(product["id"])
    chat = target.effective_chat if hasattr(target, "effective_chat") else getattr(target, "chat", None)
    if chat is None:
        raise ValueError("æ æ³ç¡®å®åååéç®æ èå¤©")

    share_link = product_share_url(product)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("ð¬ æè¦è¯¢ä»·", callback_data=f"inq:{pid}")],
        [InlineKeyboardButton("ð¤ åäº«åå", url=share_link)],
        [
            InlineKeyboardButton("â¬ï¸ è¿ååç±»", callback_data=f"cat:{product.get('category')}"),
            InlineKeyboardButton("ð  é¦é¡µ", callback_data="home"),
        ],
    ])

    if product.get("photo_id"):
        try:
            await chat.send_photo(
                photo=product["photo_id"],
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass
    await chat.send_message(text=text, reply_markup=keyboard)


# -------------------- éç¥ --------------------

async def notify_admins(context, text):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass


def telegram_api(method, params):
    data = json.dumps(params).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def notify_admins_sync(text):
    for admin_id in ADMIN_IDS:
        try:
            telegram_api("sendMessage", {"chat_id": admin_id, "text": text})
        except Exception:
            pass


# -------------------- ç¨æ·å½ä»¤ --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)

    # åååäº«æ·±é¾æ¥ï¼/start product_123
    args = context.args or []
    if args and args[0].startswith("product_"):
        try:
            pid = int(args[0].split("_", 1)[1])
        except (ValueError, IndexError):
            pid = 0

        if pid:
            product = product_get(pid)
            if product and int(product.get("active", 0) or 0):
                await send_product_to_user(update, product)
                return

            await update.message.reply_text(
                "ååä¸å­å¨æå·²ä¸æ¶ã",
                reply_markup=main_menu(update.effective_user.id),
            )
            return

    await update.message.reply_text(
        "ð æ¬¢è¿ä½¿ç¨ååç®å½\n\nè¯·éæ©åè½ï¼",
        reply_markup=main_menu(update.effective_user.id),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    await update.message.reply_text("å·²åæ¶å½åæä½ã", reply_markup=main_menu(update.effective_user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("æ æéã")
        return
    clear_state(context)
    await update.message.reply_text("âï¸ ç®¡çåå°", reply_markup=admin_menu_keyboard())


# -------------------- Callback --------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    if data == "home":
        clear_state(context)
        await safe_edit(query, "ð æ¬¢è¿ä½¿ç¨ååç®å½\n\nè¯·éæ©åè½ï¼", main_menu(user_id))
        return

    if data == "catalog":
        clear_state(context)
        await safe_edit(query, "ð åååç±»\n\nè¯·éæ©åç±»ï¼", catalog_keyboard())
        return

    if data == "search":
        clear_state(context)
        context.user_data["state"] = "search"
        await safe_edit(query, "ð è¯·è¾å¥åååç§°ãç¼å·æå³é®è¯ï¼\n\nåé /cancel å¯åæ¶")
        return

    if data.startswith("cat:"):
        category = data.split(":", 1)[1]
        products = fetchall(
            "SELECT * FROM products WHERE category=? AND active=1 ORDER BY id DESC" if not USE_POSTGRES
            else "SELECT * FROM products WHERE category=%s AND active=1 ORDER BY id DESC",
            (category,),
        )
        await safe_edit(query, f"ð {cat_name(category)}\n\nå± {len(products)} ä¸ªåå", products_keyboard(products, "prod:", "catalog"))
        return

    if data == "all_products":
        products = fetchall("SELECT * FROM products WHERE active=1 ORDER BY id DESC")
        await safe_edit(query, f"ð¦ å¨é¨åå\n\nå± {len(products)} ä¸ªåå", products_keyboard(products, "prod:", "catalog"))
        return

    if data.startswith("prod:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product or not int(product.get("active", 0) or 0):
            await safe_edit(query, "ååä¸å­å¨æå·²ä¸æ¶ã", InlineKeyboardMarkup([[InlineKeyboardButton("â¬ï¸ è¿å", callback_data="catalog")]]))
            return
        await send_product_to_user(update, product)
        return

    if data == "admin":
        if not is_admin(user_id):
            return
        clear_state(context)
        await safe_edit(query, "âï¸ ç®¡çåå°", admin_menu_keyboard())
        return

    if not is_admin(user_id):
        await safe_edit(query, "æ æéã")
        return

    if data.startswith("admin_products:"):
        page = max(0, int(data.split(":", 1)[1]))
        await show_admin_products(query, page)
        return

    if data.startswith("admin_prod:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product:
            await safe_edit(query, "ååä¸å­å¨ã", InlineKeyboardMarkup([[InlineKeyboardButton("â¬ï¸ ååç®¡ç", callback_data="admin_products:0")]]))
            return
        await safe_edit(query, product_text(product, True), admin_product_keyboard(product))
        return

    if data == "admin_add":
        clear_state(context)
        context.user_data["state"] = "add_product"
        await safe_edit(query, "â æ·»å åå\n\nè¯·åéï¼\nåç§°|ç¼å·|åç±»(c1-c12)|ä»·æ ¼|åºå­|æè¿°\n\nä¾å¦ï¼\nååA|P001|c1|100|20|æµè¯åå")
        return

    if data.startswith("edit:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "ååä¸å­å¨ã")
            return
        clear_state(context)
        context.user_data["state"] = "edit_product"
        context.user_data["edit_product_id"] = pid
        await safe_edit(query, "âï¸ ç¼è¾åå\n\nè¯·éæ°åéå®æ´ä¿¡æ¯ï¼\nåç§°|ç¼å·|åç±»(c1-c12)|ä»·æ ¼|åºå­|æè¿°")
        return

    if data.startswith("stock:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "ååä¸å­å¨ã")
            return
        clear_state(context)
        context.user_data["state"] = "stock"
        context.user_data["stock_product_id"] = pid
        await safe_edit(query, "ð¦ ä¿®æ¹åºå­\n\nè¯·è¾å¥æ°çåºå­æ°éï¼ä¾å¦ï¼100")
        return

    if data.startswith("photo:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "ååä¸å­å¨ã")
            return
        clear_state(context)
        context.user_data["state"] = "replace_photo"
        context.user_data["photo_product_id"] = pid
        await safe_edit(query, "ð¼ï¸ æ´æ¢å¾ç\n\nè¯·åéååå¾çã\nå¦æä¸éè¦æ´æ¢ï¼è¯·åéï¼è·³è¿")
        return

    if data.startswith("toggle:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if product:
            new_active = 0 if int(product.get("active", 0) or 0) else 1
            product_set_active(pid, new_active)
            product = product_get(pid)
            await safe_edit(query, product_text(product, True), admin_product_keyboard(product))
        return

    if data.startswith("delete:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product:
            await safe_edit(query, "ååä¸å­å¨ã")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("â ï¸ ç¡®è®¤å é¤", callback_data=f"confirm_delete:{pid}")],
            [InlineKeyboardButton("åæ¶", callback_data=f"admin_prod:{pid}")],
        ])
        await safe_edit(query, f"ç¡®å®å é¤ååï¼\n\n{product.get('name')} [{product.get('code') or '-'}]", kb)
        return

    if data.startswith("confirm_delete:"):
        pid = int(data.split(":", 1)[1])
        product_delete(pid)
        await safe_edit(query, "ðï¸ ååå·²å é¤ã", InlineKeyboardMarkup([[InlineKeyboardButton("â¬ï¸ ååç®¡ç", callback_data="admin_products:0")]]))
        return

    if data == "bulk_on" or data == "bulk_off":
        active = 1 if data == "bulk_on" else 0
        execute("UPDATE products SET active=1" if active else "UPDATE products SET active=0")
        await safe_edit(query, "å·²å®æå¨é¨ååç¶ææ´æ°ã", admin_menu_keyboard())
        return

    if data == "bulk_stock":
        clear_state(context)
        context.user_data["state"] = "bulk_stock_import"
        await safe_edit(
            query,
            "ð ä¸é®æ¹éä¿®æ¹åºå­\n\n"
            "è¯·åéä¸ä¸ª CSV æä»¶ã\n\n"
            "æ ¼å¼ï¼\n"
            "code,stock\n"
            "P001,20\n"
            "P002,15\n"
            "P003,30\n\n"
            "åªä¿®æ¹åºå­ï¼ä¸ä¼ä¿®æ¹åååç§°ãä»·æ ¼ãå¾çãåç±»åä¸ä¸æ¶ç¶æã\n"
            "ååç¼å·å¿é¡»ä¸ç°æååä¸è´ã\n\n"
            "ä¹æ¯æä½ åæ¥çå®æ´åå CSVï¼ä½åªè¯»å code å stock ä¸¤åã\n"
            "åé /cancel å¯åæ¶ã"
        )
        return

    if data == "admin_csv":
        clear_state(context)
        context.user_data["state"] = "csv_import"
        await safe_edit(query, "ð¥ åªå¯¼å¥CSV\n\nè¯·æ CSV æä»¶åéå°è¿éã\nå­æ®µï¼name,code,category,price,stock,description")
        return

    if data == "admin_zip":
        clear_state(context)
        context.user_data["state"] = "zip_import"
        await safe_edit(query, "ð¦ ä¸é®ä¸ä¼ åå+å¾ç\n\nè¯·åéä¸ä¸ª ZIP æä»¶ã\n\nè¦æ±ï¼\nâ¢ 1 ä¸ª CSV\nâ¢ 93 è¡åå\nâ¢ 93 å¼ å¾ç\nâ¢ å¾çå½å 01.jpg ï½ 93.jpgï¼æ¯æ jpg/jpeg/png/webpï¼\nâ¢ CSV ç¬¬1è¡å¯¹åºå¾ç01ï¼ç¬¬93è¡å¯¹åºå¾ç93")
        return

    if data == "admin_status":
        await show_status(query)
        return

    if data.startswith("admin_inquiries:"):
        page = max(0, int(data.split(":", 1)[1]))
        await show_admin_inquiries(query, page)
        return

    if data.startswith("inqadmin:"):
        iid = int(data.split(":", 1)[1])
        inquiry = fetchone(
            "SELECT * FROM inquiries WHERE id=?" if not USE_POSTGRES else "SELECT * FROM inquiries WHERE id=%s",
            (iid,),
        )
        if not inquiry:
            await safe_edit(query, "è¯¢ä»·è®°å½ä¸å­å¨ã")
            return
        status = "å·²å¤ç" if inquiry.get("status") == "handled" else "å¾å¤ç"
        text = (
            f"ð¨ è¯¢ä»· #{iid}\n"
            f"ç¶æï¼{status}\n"
            f"ååï¼{inquiry.get('product') or '-'}\n"
            f"ç¨æ·IDï¼{inquiry.get('user_id') or '-'}\n"
            f"ç¨æ·åï¼@{inquiry.get('username') or '-'}\n"
            f"åå®¹ï¼{inquiry.get('message') or '-'}\n"
            f"æ¶é´ï¼{inquiry.get('created_at') or '-'}"
        )
        kb = []
        if inquiry.get("status") != "handled":
            kb.append([InlineKeyboardButton("â æ è®°å·²å¤ç", callback_data=f"handled:{iid}")])
        kb.append([InlineKeyboardButton("â¬ï¸ è¯¢ä»·è®°å½", callback_data="admin_inquiries:0")])
        await safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    if data.startswith("handled:"):
        iid = int(data.split(":", 1)[1])
        p = db_placeholder()
        execute(f"UPDATE inquiries SET status='handled' WHERE id={p}", (iid,))
        await safe_edit(query, "â å·²æ è®°ä¸ºå·²å¤çã", InlineKeyboardMarkup([[InlineKeyboardButton("â¬ï¸ è¯¢ä»·è®°å½", callback_data="admin_inquiries:0")]]))
        return

    if data.startswith("inq:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product or not int(product.get("active", 0) or 0):
            await safe_edit(query, "ååä¸å­å¨æå·²ä¸æ¶ã")
            return
        clear_state(context)
        context.user_data["state"] = "inquiry"
        context.user_data["inquiry_product_id"] = pid
        await safe_edit(query, f"ð¬ è¯¢ä»·ï¼{product['name']}\n\nè¯·è¾å¥æ°éãä»·æ ¼è¦æ±æå¶ä»éæ±ã\n\nåé /cancel å¯åæ¶")
        return


async def show_admin_products(query, page=0):
    per_page = 10
    total = product_count()
    products = fetchall(
        "SELECT * FROM products ORDER BY id DESC LIMIT ? OFFSET ?" if not USE_POSTGRES
        else "SELECT * FROM products ORDER BY id DESC LIMIT %s OFFSET %s",
        (per_page, page * per_page),
    )
    rows = []
    for p in products:
        status = "ð¢" if int(p.get("active", 0) or 0) else "ð´"
        rows.append([InlineKeyboardButton(f"{status} {p['name']} [{p.get('code') or '-'}]", callback_data=f"admin_prod:{p['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("â¬ï¸ ä¸ä¸é¡µ", callback_data=f"admin_products:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("ä¸ä¸é¡µ â¡ï¸", callback_data=f"admin_products:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("â¬ï¸ ç®¡çåå°", callback_data="admin")])
    await safe_edit(query, f"ð¦ ååç®¡ç\n\nå± {total} ä¸ªåå\nç¬¬ {page + 1} é¡µ", InlineKeyboardMarkup(rows))


async def show_admin_inquiries(query, page=0):
    per_page = 10
    total_row = fetchone("SELECT COUNT(*) AS n FROM inquiries")
    total = int(total_row["n"] or 0)
    inquiries = fetchall(
        "SELECT * FROM inquiries ORDER BY id DESC LIMIT ? OFFSET ?" if not USE_POSTGRES
        else "SELECT * FROM inquiries ORDER BY id DESC LIMIT %s OFFSET %s",
        (per_page, page * per_page),
    )
    rows = []
    for x in inquiries:
        status = "â" if x.get("status") == "handled" else "ð "
        label = f"{status} #{x['id']} {x.get('product') or 'åå'}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"inqadmin:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("â¬ï¸ ä¸ä¸é¡µ", callback_data=f"admin_inquiries:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("ä¸ä¸é¡µ â¡ï¸", callback_data=f"admin_inquiries:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("â¬ï¸ ç®¡çåå°", callback_data="admin")])
    await safe_edit(query, f"ð¨ è¯¢ä»·è®°å½\n\nå± {total} æ¡\nç¬¬ {page + 1} é¡µ", InlineKeyboardMarkup(rows))


async def show_status(query):
    total = product_count()
    active = fetchone("SELECT COUNT(*) AS n FROM products WHERE active=1")
    photos = fetchone("SELECT COUNT(*) AS n FROM products WHERE photo_id IS NOT NULL AND photo_id <> ''")
    inquiries = fetchone("SELECT COUNT(*) AS n FROM inquiries")
    csv_path = BASE_DIR / CSV_FILE
    text = (
        "ð ç³»ç»ç¶æ\n\n"
        f"æ°æ®åºï¼{'PostgreSQL' if USE_POSTGRES else 'SQLite'}\n"
        f"ååæ»æ°ï¼{total}\n"
        f"ä¸æ¶ååï¼{int(active['n'] or 0)}\n"
        f"æå¾çååï¼{int(photos['n'] or 0)}\n"
        f"è¯¢ä»·æ»æ°ï¼{int(inquiries['n'] or 0)}\n"
        f"CSVï¼{CSV_FILE}\n"
        f"CSVå­å¨ï¼{'æ¯' if csv_path.exists() else 'å¦'}\n"
        f"Webï¼{WEB_URL}\n"
        f"PORTï¼{PORT}"
    )
    await safe_edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("â¬ï¸ ç®¡çåå°", callback_data="admin")]]))


# -------------------- ææ¬è¾å¥ --------------------

def parse_product_line(text):
    parts = [x.strip() for x in text.split("|")]
    if len(parts) < 6:
        raise ValueError("æ ¼å¼ä¸æ­£ç¡®ï¼å¿é¡»æ¯ï¼åç§°|ç¼å·|åç±»|ä»·æ ¼|åºå­|æè¿°")
    name, code, category, price, stock_text, description = parts[:6]
    if not name:
        raise ValueError("åååç§°ä¸è½ä¸ºç©º")
    if category not in CATEGORIES:
        raise ValueError("åç±»å¿é¡»æ¯ c1-c12")
    try:
        stock = int(stock_text)
    except ValueError:
        raise ValueError("åºå­å¿é¡»æ¯æ´æ°")
    if stock < 0:
        raise ValueError("åºå­ä¸è½å°äº0")
    return name, code, category, price, stock, description


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    state = context.user_data.get("state")

    if state == "search":
        like = f"%{text}%"
        if USE_POSTGRES:
            products = fetchall(
                """SELECT * FROM products WHERE active=1 AND
                   (name ILIKE %s OR code ILIKE %s OR description ILIKE %s)
                   ORDER BY id DESC LIMIT 50""",
                (like, like, like),
            )
        else:
            products = fetchall(
                """SELECT * FROM products WHERE active=1 AND
                   (name LIKE ? OR code LIKE ? OR description LIKE ?)
                   ORDER BY id DESC LIMIT 50""",
                (like, like, like),
            )
        clear_state(context)
        if not products:
            await update.message.reply_text("æ²¡ææ¾å°ååã", reply_markup=main_menu(user_id))
        else:
            await update.message.reply_text(f"ð æ¾å° {len(products)} ä¸ªååï¼", reply_markup=products_keyboard(products, "prod:", "home"))
        return

    if not is_admin(user_id):
        if state == "inquiry":
            pid = context.user_data.get("inquiry_product_id")
            product = product_get(pid) if pid else None
            if not product:
                clear_state(context)
                await update.message.reply_text("ååä¸å­å¨ã", reply_markup=main_menu(user_id))
                return
            username = update.effective_user.username or ""
            if USE_POSTGRES:
                row = execute(
                    """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (str(user_id), username, f"{product['name']} [{product.get('code') or '-'}]", text, "pending", now_text()),
                    returning=True,
                )
                inquiry_id = int(row["id"])
            else:
                inquiry_id = int(execute(
                    """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (str(user_id), username, f"{product['name']} [{product.get('code') or '-'}]", text, "pending", now_text()),
                ))
            notify_text = (
                f"ð¨ æ°è¯¢ä»· #{inquiry_id}\n"
                f"ååï¼{product['name']} [{product.get('code') or '-'}]\n"
                f"ç¨æ·IDï¼{user_id}\n"
                f"ç¨æ·åï¼@{username or '-'}\n"
                f"åå®¹ï¼{text}"
            )
            await notify_admins(context, notify_text)
            clear_state(context)
            await update.message.reply_text(
                f"â è¯¢ä»·å·²æäº¤ã\nå®¢æï¼@{SERVICE_USERNAME}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("ð¬ å®¢æç´è¾¾", url=f"https://t.me/{SERVICE_USERNAME}")],
                    [InlineKeyboardButton("ð  è¿åä¸»èå", callback_data="home")],
                ]),
            )
        else:
            await update.message.reply_text("è¯·éæ©èååè½ã", reply_markup=main_menu(user_id))
        return

    # ç®¡çåç¶æ
    if state == "add_product":
        try:
            name, code, category, price, stock, description = parse_product_line(text)
            pid = product_create(name, code, category, price, stock, description)
            clear_state(context)
            context.user_data["state"] = "add_photo"
            context.user_data["pending_product_id"] = pid
            await update.message.reply_text("ååå·²åå»ºã\n\nè¯·åéååå¾çã\nå¦æä¸éè¦å¾çï¼è¯·åéï¼è·³è¿")
        except Exception as exc:
            await update.message.reply_text(f"â {exc}\n\nè¯·éæ°åéæ­£ç¡®æ ¼å¼ã")
        return

    if state == "add_photo":
        if text == "è·³è¿":
            pid = context.user_data.get("pending_product_id")
            clear_state(context)
            product = product_get(pid) if pid else None
            await update.message.reply_text("å·²è·³è¿å¾çã", reply_markup=admin_product_keyboard(product) if product else admin_menu_keyboard())
            return
        await update.message.reply_text("è¯·åéååå¾çï¼æèåéï¼è·³è¿")
        return

    if state == "edit_product":
        pid = context.user_data.get("edit_product_id")
        if not pid or not product_get(pid):
            clear_state(context)
            await update.message.reply_text("ååä¸å­å¨ã", reply_markup=admin_menu_keyboard())
            return
        try:
            name, code, category, price, stock, description = parse_product_line(text)
            product_update(pid, name, code, category, price, stock, description)
            product = product_get(pid)
            clear_state(context)
            await update.message.reply_text("â ååå·²æ´æ°ã", reply_markup=admin_product_keyboard(product))
        except Exception as exc:
            await update.message.reply_text(f"â {exc}\n\nè¯·éæ°åéå®æ´ä¿¡æ¯ã")
        return

    if state == "stock":
        pid = context.user_data.get("stock_product_id")
        try:
            stock = int(text)
            if stock < 0:
                raise ValueError
            product_set_stock(pid, stock)
            product = product_get(pid)
            clear_state(context)
            await update.message.reply_text("â åºå­å·²ä¿®æ¹ã", reply_markup=admin_product_keyboard(product))
        except Exception:
            await update.message.reply_text("â åºå­å¿é¡»æ¯ 0 æä»¥ä¸çæ´æ°ï¼è¯·éæ°è¾å¥ã")
        return

    if state == "replace_photo":
        if text == "è·³è¿":
            pid = context.user_data.get("photo_product_id")
            product = product_get(pid) if pid else None
            clear_state(context)
            await update.message.reply_text("å·²åæ¶æ´æ¢å¾çã", reply_markup=admin_product_keyboard(product) if product else admin_menu_keyboard())
        else:
            await update.message.reply_text("è¯·åéååå¾çï¼æèåéï¼è·³è¿")
        return

    if state == "inquiry":
        # ç®¡çåä¹åè®¸æ­£å¸¸æäº¤è¯¢ä»·
        pid = context.user_data.get("inquiry_product_id")
        product = product_get(pid) if pid else None
        if not product:
            clear_state(context)
            await update.message.reply_text("ååä¸å­å¨ã", reply_markup=admin_menu_keyboard())
            return
        username = update.effective_user.username or ""
        if USE_POSTGRES:
            row = execute(
                """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (str(user_id), username, f"{product['name']} [{product.get('code') or '-'}]", text, "pending", now_text()),
                returning=True,
            )
            inquiry_id = int(row["id"])
        else:
            inquiry_id = int(execute(
                """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (str(user_id), username, f"{product['name']} [{product.get('code') or '-'}]", text, "pending", now_text()),
            ))
        await notify_admins(context, f"ð¨ æ°è¯¢ä»· #{inquiry_id}\nååï¼{product['name']}\nç¨æ·IDï¼{user_id}\nåå®¹ï¼{text}")
        clear_state(context)
        await update.message.reply_text("â è¯¢ä»·å·²æäº¤ã", reply_markup=main_menu(user_id))
        return

    await update.message.reply_text("è¯·éæ©èååè½ã", reply_markup=admin_menu_keyboard())


# -------------------- å¾ç / æä»¶ --------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    state = context.user_data.get("state")
    if state not in ("add_photo", "replace_photo"):
        return
    photos = update.message.photo
    if not photos:
        return
    photo_id = photos[-1].file_id
    pid = context.user_data.get("pending_product_id") if state == "add_photo" else context.user_data.get("photo_product_id")
    if not pid:
        return
    product_set_photo(pid, photo_id)
    await safe_delete_message(update.message)
    product = product_get(pid)
    clear_state(context)
    if product:
        await update.effective_chat.send_message("â å¾çå·²ä¿å­ï¼ä¸ä¼ å¾çæ¶æ¯å·²å°è¯èªå¨å é¤ã", reply_markup=admin_product_keyboard(product))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    doc = update.message.document
    if not doc:
        return
    filename = doc.file_name or ""
    state = context.user_data.get("state")
    if filename.lower().endswith(".zip"):
        if state != "zip_import":
            await update.message.reply_text("è¯·åå¨ç®¡çåå°ç¹å»âð¦ ä¸é®ä¸ä¼ åå+å¾çâï¼ååé ZIPã")
            return
        await process_zip(update, context, doc)
        return
    if state == "csv_import" and filename.lower().endswith(".csv"):
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            temp_path = UPLOAD_DIR / f"import_{update.effective_user.id}_{doc.file_unique_id}.csv"
            await tg_file.download_to_drive(custom_path=str(temp_path))
            count = import_csv_file(temp_path)
            try:
                temp_path.unlink()
            except Exception:
                pass
            await safe_delete_message(update.message)
            clear_state(context)
            await update.effective_chat.send_message(f"â CSV å¯¼å¥å®æï¼å±å¤ç {count} è¡ã", reply_markup=admin_menu_keyboard())
        except Exception as exc:
            await update.message.reply_text(f"â CSV å¯¼å¥å¤±è´¥ï¼{exc}")
        return
    if state == "bulk_stock_import" and filename.lower().endswith(".csv"):
        temp_path = UPLOAD_DIR / f"bulk_stock_{update.effective_user.id}_{doc.file_unique_id}.csv"
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(custom_path=str(temp_path))
            count = bulk_update_stock_from_csv(temp_path)
            await safe_delete_message(update.message)
            clear_state(context)
            await update.effective_chat.send_message(
                f"â æ¹éä¿®æ¹åºå­å®æï¼\n\næåæ´æ°ï¼{count} ä¸ªåå\n\n"
                "åååç§°ãä»·æ ¼ãå¾çãåç±»åä¸ä¸æ¶ç¶æåæªä¿®æ¹ã",
                reply_markup=admin_menu_keyboard(),
            )
        except Exception as exc:
            print(f"æ¹éä¿®æ¹åºå­å¤±è´¥ï¼{type(exc).__name__}: {exc}")
            traceback.print_exc()
            await update.message.reply_text(f"â æ¹éä¿®æ¹åºå­å¤±è´¥ï¼\n{type(exc).__name__}: {exc}")
        finally:
            try:
                temp_path.unlink()
            except Exception:
                pass
        return
    if state in ("add_photo", "replace_photo") and Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        pid = context.user_data.get("pending_product_id") if state == "add_photo" else context.user_data.get("photo_product_id")
        if pid:
            product_set_photo(pid, doc.file_id)
            await safe_delete_message(update.message)
            product = product_get(pid)
            clear_state(context)
            await update.effective_chat.send_message("â å¾çå·²ä¿å­ï¼ä¸ä¼ å¾çæ¶æ¯å·²å°è¯èªå¨å é¤ã", reply_markup=admin_product_keyboard(product))


async def process_zip(update: Update, context: ContextTypes.DEFAULT_TYPE, doc):
    temp_dir = Path(tempfile.mkdtemp(prefix="catalog_zip_", dir=str(UPLOAD_DIR)))
    zip_path = temp_dir / (doc.file_name or "catalog.zip")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(zip_path))
        await safe_delete_message(update.message)

        with zipfile.ZipFile(zip_path, "r") as z:
            members = [m for m in z.namelist() if not m.endswith("/")]
            csv_members = [m for m in members if m.lower().endswith(".csv")]
            image_members = []
            for m in members:
                ext = Path(m).suffix.lower()
                stem = Path(m).stem
                if ext in {".jpg", ".jpeg", ".png", ".webp"} and re.fullmatch(r"\d{2}", stem):
                    image_members.append(m)
            if len(csv_members) != 1:
                raise ValueError(f"ZIP å¿é¡»ä¸åªè½æ 1 ä¸ª CSVï¼ç®åæ {len(csv_members)} ä¸ª")
            if len(image_members) != 93:
                raise ValueError(f"ZIP å¿é¡»æ 93 å¼ å½åä¸º 01-93 çå¾çï¼ç®åæ£æµå° {len(image_members)} å¼ ")
            image_map = {}
            for m in image_members:
                num = int(Path(m).stem)
                if not 1 <= num <= 93:
                    raise ValueError(f"å¾çç¼å·è¶åºèå´ï¼{m}")
                if num in image_map:
                    raise ValueError(f"å¾çç¼å·éå¤ï¼{num:02d}")
                image_map[num] = m
            if len(image_map) != 93:
                raise ValueError("å¾çå¿é¡»å®æ´è¦ç 01-93")
            csv_data = z.read(csv_members[0])
            csv_text = None
            for enc in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    csv_text = csv_data.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            if csv_text is None:
                raise ValueError("æ æ³è¯å« CSV ç¼ç ")
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames:
                raise ValueError("CSV æ²¡æè¡¨å¤´")
            fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
            required = ["name", "code", "category", "price", "stock"]
            missing = [x for x in required if x not in fields]
            if missing:
                raise ValueError("CSV ç¼ºå°å­æ®µï¼" + ", ".join(missing))
            rows = []
            for raw in reader:
                row = {}
                for key, original in fields.items():
                    row[key] = raw.get(original, "")
                rows.append(row)
            if len(rows) != 93:
                raise ValueError(f"CSV å¿é¡»æ­£å¥½ 93 è¡ï¼ç®åæ¯ {len(rows)} è¡")

            # ä¸¥æ ¼æ ¡éª CSV ç¬¬1~93è¡å¿é¡»å¯¹åº P001~P093ï¼é¿åå¾çééã
            for index, row in enumerate(rows, start=1):
                expected_code = f"P{index:03d}"
                actual_code = str(row.get("code", "")).strip().upper()
                if actual_code != expected_code:
                    raise ValueError(
                        f"CSV ç¬¬ {index} è¡ç¼å·åºä¸º {expected_code}ï¼å®éä¸º {actual_code or 'ç©º'}"
                    )
                if not str(row.get("name", "")).strip():
                    raise ValueError(f"CSV ç¬¬ {index} è¡åååç§°ä¸ºç©º")
                normalized_category = normalize_category(row.get("category", ""))
                if normalized_category not in CATEGORIES:
                    raise ValueError(
                        f"CSV ç¬¬ {index} è¡åç±»æ æï¼{row.get('category', '')}ï¼æ¯æ c1-c12 æä¸­æåç±»åç§°"
                    )
                row["category"] = normalized_category

            await update.effective_chat.send_message("â³ å·²éªè¯ ZIPï¼1ä¸ªCSV + 93å¼ å¾çãå¼å§å¯¼å¥ï¼è¯·ç¨åâ¦")
            for index, row in enumerate(rows, start=1):
                temp_image = temp_dir / f"{index:02d}{Path(image_map[index]).suffix.lower()}"
                temp_image.write_bytes(z.read(image_map[index]))
                ext = temp_image.suffix.lower()
                with temp_image.open("rb") as f:
                    if ext in {".jpg", ".jpeg", ".png"}:
                        sent = await context.bot.send_photo(chat_id=ADMIN_IDS[0], photo=f)
                        photo_id = sent.photo[-1].file_id if sent.photo else None
                    else:
                        sent = await context.bot.send_document(chat_id=ADMIN_IDS[0], document=f)
                        photo_id = sent.document.file_id if sent.document else None
                if not photo_id:
                    raise ValueError(f"ç¬¬ {index} å¼ å¾çæ æ³åå¾ Telegram file_id")
                try:
                    await context.bot.delete_message(chat_id=ADMIN_IDS[0], message_id=sent.message_id)
                except Exception:
                    pass
                product_upsert(row, photo_id=photo_id)
                try:
                    temp_image.unlink()
                except Exception:
                    pass
                if index % 10 == 0 or index == 93:
                    await update.effective_chat.send_message(f"â³ å·²å®æ {index}/93")

        clear_state(context)
        await update.effective_chat.send_message("â 93ä¸ªåå + 93å¼ å¾çå¨é¨å¯¼å¥å®æã", reply_markup=admin_menu_keyboard())
    except Exception as exc:
        clear_state(context)
        error_text = f"{type(exc).__name__}: {exc}"
        print(f"ZIP å¯¼å¥å¤±è´¥ï¼{error_text}")
        traceback.print_exc()
        await update.effective_chat.send_message(
            f"â ZIP å¯¼å¥å¤±è´¥ï¼\n{error_text}",
            reply_markup=admin_menu_keyboard(),
        )
    finally:
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# -------------------- Web Server --------------------

class CatalogHandler(BaseHTTPRequestHandler):
    server_version = "CatalogBotHTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/":
                self.serve_index()
            elif path == "/api/status":
                self.api_status()
            elif path == "/api/categories":
                self.api_categories()
            elif path == "/api/products":
                self.api_products(qs)
            elif path == "/api/photo":
                self.api_photo(qs)
            else:
                self.send_json({"ok": False, "error": "Not Found"}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/inquiry":
            self.send_json({"ok": False, "error": "Not Found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            self.api_inquiry(payload)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)

    def serve_index(self):
        index = WEB_DIR / "index.html"
        if not index.exists():
            data = b"<h1>Catalog Bot</h1><p>web/index.html not found.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_status(self):
        total = product_count()
        active = fetchone("SELECT COUNT(*) AS n FROM products WHERE active=1")
        photos = fetchone("SELECT COUNT(*) AS n FROM products WHERE photo_id IS NOT NULL AND photo_id <> ''")
        self.send_json({
            "ok": True,
            "db": "postgresql" if USE_POSTGRES else "sqlite",
            "db_path": None if USE_POSTGRES else str(DB_PATH),
            "total_products": total,
            "active_products": int(active["n"] or 0),
            "products_with_photos": int(photos["n"] or 0),
            "csv_file": CSV_FILE,
            "csv_exists": (BASE_DIR / CSV_FILE).exists(),
            "web_url": WEB_URL,
        })

    def api_categories(self):
        result = []
        for code, name in CATEGORIES.items():
            p = db_placeholder()
            row = fetchone(f"SELECT COUNT(*) AS n FROM products WHERE category={p} AND active=1", (code,))
            result.append({"code": code, "name": name, "count": int(row["n"] or 0)})
        self.send_json({"ok": True, "categories": result})

    def api_products(self, qs):
        category = qs.get("category", [""])[0].strip()
        search = qs.get("search", [""])[0].strip()
        active = qs.get("active", ["1"])[0].strip()
        clauses = []
        params = []
        ph = db_placeholder()
        if active != "all":
            clauses.append(f"active={ph}")
            params.append(1 if active != "0" else 0)
        if category:
            clauses.append(f"category={ph}")
            params.append(category)
        if search:
            like = f"%{search}%"
            if USE_POSTGRES:
                clauses.append(f"(name ILIKE {ph} OR code ILIKE {ph} OR description ILIKE {ph})")
            else:
                clauses.append(f"(name LIKE {ph} OR code LIKE {ph} OR description LIKE {ph})")
            params.extend([like, like, like])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = "SELECT id,name,code,category,price,stock,description,active,photo_id,created_at FROM products" + where + " ORDER BY id DESC LIMIT 200"
        rows = fetchall(sql, tuple(params))
        for row in rows:
            row["category_name"] = cat_name(row.get("category"))
            row["photo_url"] = f"/api/photo?id={row['id']}" if row.get("photo_id") else ""
        self.send_json({"ok": True, "products": rows})

    def api_photo(self, qs):
        try:
            pid = int(qs.get("id", ["0"])[0])
        except ValueError:
            self.send_json({"ok": False, "error": "invalid id"}, 400)
            return
        product = product_get(pid)
        if not product or not product.get("photo_id"):
            self.send_json({"ok": False, "error": "photo not found"}, 404)
            return
        result = telegram_api("getFile", {"file_id": product["photo_id"]})
        if not result.get("ok"):
            self.send_json({"ok": False, "error": "telegram getFile failed"}, 404)
            return
        file_path = result.get("result", {}).get("file_path")
        if not file_path:
            self.send_json({"ok": False, "error": "file path not found"}, 404)
            return
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{quote(file_path, safe='/') }"
        req = Request(file_url, headers={"User-Agent": "catalog-bot/1.0"})
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
            content_type = resp.headers.get_content_type() or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_inquiry(self, payload):
        try:
            pid = int(payload.get("product_id", 0))
        except Exception:
            pid = 0
        product = product_get(pid) if pid else None
        if not product:
            self.send_json({"ok": False, "error": "ååä¸å­å¨"}, 404)
            return
        message = str(payload.get("message", "")).strip()
        if not message:
            self.send_json({"ok": False, "error": "è¯·å¡«åè¯¢ä»·åå®¹"}, 400)
            return
        user_id = str(payload.get("user_id", "web"))
        username = str(payload.get("username", "web"))
        product_name = f"{product['name']} [{product.get('code') or '-'}]"
        if USE_POSTGRES:
            row = execute(
                """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (user_id, username, product_name, message, "pending", now_text()),
                returning=True,
            )
            inquiry_id = int(row["id"])
        else:
            inquiry_id = int(execute(
                """INSERT INTO inquiries (user_id,username,product,message,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, username, product_name, message, "pending", now_text()),
            ))
        contact = str(payload.get("contact", "")).strip()
        notify = (
            f"ð¨ Webæ°è¯¢ä»· #{inquiry_id}\n"
            f"ååï¼{product_name}\n"
            f"ç¨æ·ï¼{username}\n"
            f"ç¨æ·IDï¼{user_id}\n"
            f"èç³»æ¹å¼ï¼{contact or '-'}\n"
            f"åå®¹ï¼{message}"
        )
        threading.Thread(target=notify_admins_sync, args=(notify,), daemon=True).start()
        self.send_json({"ok": True, "inquiry_id": inquiry_id})


def start_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Web server listening on 0.0.0.0:{PORT}")
    return server


# -------------------- å¯å¨ --------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    print("========== TELEGRAM HANDLER ERROR ==========")
    print(f"Error type: {type(error).__name__}")
    print(f"Error: {error!r}")
    traceback.print_exception(type(error), error, error.__traceback__)
    print("============================================")


def startup_import_if_empty():
    try:
        if product_count() == 0:
            path = BASE_DIR / CSV_FILE
            if path.exists():
                count = import_csv_file(path)
                print(f"Imported {count} products from {path}")
    except Exception as exc:
        print(f"Startup CSV import skipped/failed: {exc}")


def main():
    db_init()
    startup_import_if_empty()
    start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)

    print("Bot starting...")
    print(f"Admins: {ADMIN_IDS}")
    print(f"Database: {'PostgreSQL' if USE_POSTGRES else DB_PATH}")
    print(f"Web URL: {WEB_URL}")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
