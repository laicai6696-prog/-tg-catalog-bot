import os
import csv
import json
import re
import shutil
import sqlite3
import zipfile
import tempfile
import threading
import traceback
import mimetypes
import asyncio
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
CSV_FILE = BASE_DIR / os.getenv(
    "CSV_FILE",
    "Telegramååå¯¼å¥è¡¨_93ä¸ª_å¯ç´æ¥å¯¼å¥.csv"
)

CATS = {
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

CAT_ALIASES = {}
for key, value in CATS.items():
    CAT_ALIASES[key.lower()] = key
    CAT_ALIASES[value] = key


def get_admin_ids():
    raw = os.getenv("ADMIN_IDS", "")
    result = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


ADMINS = get_admin_ids()


def is_admin(user_id):
    return user_id in ADMINS


def db_is_postgres():
    return DATABASE_URL.lower().startswith("postgres")


def db_connect():
    if db_is_postgres():
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        return conn, True, RealDictCursor

    conn = sqlite3.connect(BASE_DIR / "bot.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn, False, sqlite3.Row


def placeholder():
    return "%s" if db_is_postgres() else "?"


def init_db():
    conn, is_pg, row_factory = db_connect()
    cur = conn.cursor()

    if is_pg:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT,
                category TEXT,
                price TEXT,
                stock TEXT,
                description TEXT,
                active INTEGER DEFAULT 1,
                photo_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                product TEXT,
                message TEXT,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_id TEXT")
    else:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT,
                category TEXT,
                price TEXT,
                stock TEXT,
                description TEXT,
                active INTEGER DEFAULT 1,
                photo_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                product TEXT,
                message TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute("PRAGMA table_info(products)")
        columns = {row[1] for row in cur.fetchall()}
        if "photo_id" not in columns:
            cur.execute("ALTER TABLE products ADD COLUMN photo_id TEXT")

    conn.commit()
    conn.close()


def fetch_all(sql, params=()):
    conn, is_pg, row_factory = db_connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    if is_pg:
        return [dict(row) for row in rows]
    return [dict(row) for row in rows]


def fetch_one(sql, params=()):
    conn, is_pg, row_factory = db_connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    conn.close()

    if row is None:
        return None
    return dict(row)


def execute(sql, params=()):
    conn, is_pg, row_factory = db_connect()
    cur = conn.cursor()
    cur.execute(sql, params)

    last_id = None
    if not is_pg:
        last_id = cur.lastrowid

    conn.commit()
    conn.close()
    return last_id


def insert_product(name, code, category, price, stock, description="", active=1):
    conn, is_pg, row_factory = db_connect()
    cur = conn.cursor()

    if is_pg:
        cur.execute(
            """
            INSERT INTO products
            (name, code, category, price, stock, description, active)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, code, category, price, stock, description, active),
        )
        row = cur.fetchone()
        product_id = row["id"] if isinstance(row, dict) else row[0]
    else:
        cur.execute(
            """
            INSERT INTO products
            (name, code, category, price, stock, description, active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, code, category, price, stock, description, active),
        )
        product_id = cur.lastrowid

    conn.commit()
    conn.close()
    return product_id


def product_count():
    row = fetch_one("SELECT COUNT(*) AS c FROM products")
    return int(row["c"]) if row else 0


def normalize_text(value):
    value = str(value or "").strip()
    value = value.replace("\\", "/")
    value = re.sub(r"\s+", "", value)
    return value.lower()


def normalize_category(value):
    value = str(value or "").strip()
    if value in CAT_ALIASES:
        return CAT_ALIASES[value]

    lowered = value.lower()
    if lowered in CAT_ALIASES:
        return CAT_ALIASES[lowered]

    return value


def import_csv_if_empty():
    if product_count() > 0:
        return 0

    if not CSV_FILE.exists():
        return 0

    imported = 0

    with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            name = str(row.get("name", row.get("åç§°", ""))).strip()
            code = str(row.get("code", row.get("ç¼å·", ""))).strip()
            category = normalize_category(
                row.get("category", row.get("åç±»", ""))
            )
            price = str(row.get("price", row.get("ä»·æ ¼", ""))).strip()
            stock = str(row.get("stock", row.get("åºå­", ""))).strip()
            description = str(
                row.get("description", row.get("æè¿°", ""))
            ).strip()

            if not name:
                continue

            insert_product(
                name=name,
                code=code,
                category=category,
                price=price,
                stock=stock,
                description=description,
                active=1,
            )
            imported += 1

    return imported


def get_product(product_id):
    return fetch_one(
        "SELECT * FROM products WHERE id = " + placeholder(),
        (product_id,),
    )


def get_products(category=None, keyword=None, active_only=True):
    sql = "SELECT * FROM products WHERE 1=1"
    params = []

    if active_only:
        sql += " AND active = 1"

    if category:
        sql += " AND category = " + placeholder()
        params.append(category)

    if keyword:
        p = placeholder()
        sql += " AND (name LIKE " + p + " OR code LIKE " + p + ")"
        params.extend(["%" + keyword + "%", "%" + keyword + "%"])

    sql += " ORDER BY id DESC"
    return fetch_all(sql, params)


def telegram_api(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_telegram_file_url(file_id):
    result = telegram_api("getFile", {"file_id": file_id})
    if not result.get("ok"):
        return None

    file_path = result.get("result", {}).get("file_path")
    if not file_path:
        return None

    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def get_product_photo(product):
    if not product:
        return None

    photo_id = product.get("photo_id")
    if not photo_id:
        return None

    return get_telegram_file_url(photo_id)


class WebHandler(BaseHTTPRequestHandler):
    def _send(self, status=200, content_type="text/plain; charset=utf-8", data=b""):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/":
                index_file = WEB_DIR / "index.html"
                if not index_file.exists():
                    self._send(
                        200,
                        "text/html; charset=utf-8",
                        b"<h1>Telegram Catalog</h1>",
                    )
                    return

                data = index_file.read_bytes()
                self._send(200, "text/html; charset=utf-8", data)
                return

            if path == "/api/status":
                total = product_count()
                active = fetch_one(
                    "SELECT COUNT(*) AS c FROM products WHERE active = 1"
                )
                photos = fetch_one(
                    "SELECT COUNT(*) AS c FROM products WHERE photo_id IS NOT NULL AND photo_id != ''"
                )

                self._json(
                    {
                        "ok": True,
                        "db": "postgresql" if db_is_postgres() else "sqlite",
                        "total_products": total,
                        "active_products": int(active["c"]) if active else 0,
                        "products_with_photos": int(photos["c"]) if photos else 0,
                        "csv_file": CSV_FILE.name,
                        "csv_exists": CSV_FILE.exists(),
                    }
                )
                return

            if path == "/api/categories":
                counts = {}
                for key, name in CATS.items():
                    row = fetch_one(
                        "SELECT COUNT(*) AS c FROM products WHERE category = "
                        + placeholder()
                        + " AND active = 1",
                        (key,),
                    )
                    counts[key] = {
                        "id": key,
                        "name": name,
                        "count": int(row["c"]) if row else 0,
                    }

                self._json({"ok": True, "categories": list(counts.values())})
                return

            if path == "/api/products":
                category = query.get("category", [None])[0]
                keyword = query.get("keyword", [None])[0]
                active = query.get("active", ["1"])[0] != "0"

                if category:
                    category = normalize_category(category)

                products = get_products(
                    category=category,
                    keyword=keyword,
                    active_only=active,
                )

                output = []
                for p in products:
                    item = dict(p)
                    if item.get("photo_id"):
                        item["photo_url"] = (
                            WEB_URL + "/api/photo?id=" + str(item["id"])
                        )
                    else:
                        item["photo_url"] = ""
                    output.append(item)

                self._json({"ok": True, "products": output})
                return

            if path == "/api/photo":
                raw_id = query.get("id", [None])[0]
                if not raw_id or not str(raw_id).isdigit():
                    self._send(400, "text/plain; charset=utf-8", b"bad id")
                    return

                product = get_product(int(raw_id))
                if not product or not product.get("photo_id"):
                    self._send(404, "text/plain; charset=utf-8", b"not found")
                    return

                photo_url = get_telegram_file_url(product["photo_id"])
                if not photo_url:
                    self._send(404, "text/plain; charset=utf-8", b"photo unavailable")
                    return

                req = Request(photo_url, method="GET")
                with urlopen(req, timeout=30) as response:
                    content = response.read()
                    content_type = response.headers.get(
                        "Content-Type",
                        "image/jpeg",
                    )

                self._send(200, content_type, content)
                return

            if path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
                return

            if path.startswith("/web/"):
                filename = path[5:].lstrip("/").replace("..", "")
                target = WEB_DIR / filename

                if target.exists() and target.is_file():
                    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    self._send(200, content_type, target.read_bytes())
                    return

            self._send(404, "text/plain; charset=utf-8", b"Not Found")

        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/api/inquiry":
            self._send(404, "text/plain; charset=utf-8", b"Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))

            product = str(data.get("product", "")).strip()
            message = str(data.get("message", "")).strip()
            user_id = data.get("user_id")
            username = str(data.get("username", "")).strip()

            if not product:
                self._json({"ok": False, "error": "è¯·éæ©åå"}, 400)
                return

            sql = """
                INSERT INTO inquiries
                (user_id, username, product, message, status)
                VALUES ({p}, {p}, {p}, {p}, {p})
            """.format(p=placeholder())

            execute(
                sql,
                (
                    user_id,
                    username,
                    product,
                    message,
                    "new",
                ),
            )

            threading.Thread(
                target=notify_admins_sync,
                args=(product, message, username),
                daemon=True,
            ).start()

            self._json({"ok": True})

        except Exception as exc:
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, format, *args):
        return


def start_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Web server started on 0.0.0.0:{PORT}")
    return server


async def send_product_message(update, context, product):
    name = product.get("name", "")
    code = product.get("code", "")
    category = product.get("category", "")
    price = product.get("price", "")
    stock = product.get("stock", "")
    description = product.get("description", "")

    text = (
        f"ð¦ {name}\n"
        f"ç¼å·ï¼{code}\n"
        f"åç±»ï¼{CATS.get(category, category)}\n"
        f"ä»·æ ¼ï¼{price}\n"
        f"åºå­ï¼{stock}\n"
    )

    if description:
        text += f"è¯´æï¼{description}\n"

    keyboard = [
        [
            InlineKeyboardButton(
                "ð¬ è¯¢ä»·",
                callback_data=f"inquiry:{product['id']}",
            )
        ]
    ]

    photo_id = product.get("photo_id")

    if photo_id:
        try:
            await update.message.reply_photo(
                photo=photo_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        except Exception:
            pass

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ðï¸ ååç®å½", callback_data="catalog"),
                InlineKeyboardButton("ð åå", callback_data="products"),
            ],
            [
                InlineKeyboardButton("ð¬ è¯¢ä»·", callback_data="ask"),
                InlineKeyboardButton("â æ·»å åå", callback_data="add"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add_step", None)
    context.user_data.pop("pending_product_id", None)

    text = (
        "æ¬¢è¿ä½¿ç¨ååç®å½æºå¨äºº\n\n"
        "è¯·éæ©åè½ï¼"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "å·²åæ¶å½åæä½ã",
        reply_markup=main_menu(),
    )


async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []

    row = []
    for index, (key, name) in enumerate(CATS.items(), start=1):
        row.append(
            InlineKeyboardButton(
                name,
                callback_data=f"cat:{key}",
            )
        )
        if index % 2 == 0:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append(
        [InlineKeyboardButton("â¬ï¸ è¿å", callback_data="home")]
    )

    await update.effective_message.reply_text(
        "è¯·éæ©ååç³»åï¼",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_category(update, context, category):
    products = get_products(category=category, active_only=True)

    if not products:
        await update.effective_message.reply_text(
            "è¿ä¸ªç³»åææ¶æ²¡æä¸æ¶ååã",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("â¬ï¸ è¿åç®å½", callback_data="catalog")]]
            ),
        )
        return

    keyboard = []

    for product in products:
        title = product["name"]
        code = product.get("code") or ""
        if code:
            title += f" [{code}]"

        keyboard.append(
            [
                InlineKeyboardButton(
                    title[:60],
                    callback_data=f"product:{product['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("â¬ï¸ è¿åç®å½", callback_data="catalog")]
    )

    await update.effective_message.reply_text(
        f"ð {CATS.get(category, category)}\n\nè¯·éæ©ååï¼",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_product(update, context, product_id):
    product = get_product(product_id)

    if not product or int(product.get("active", 0)) != 1:
        await update.effective_message.reply_text("ååä¸å­å¨æå·²ä¸æ¶ã")
        return

    name = product.get("name", "")
    code = product.get("code", "")
    category = product.get("category", "")
    price = product.get("price", "")
    stock = product.get("stock", "")
    description = product.get("description", "")

    text = (
        f"ð¦ {name}\n"
        f"ç¼å·ï¼{code}\n"
        f"åç±»ï¼{CATS.get(category, category)}\n"
        f"ä»·æ ¼ï¼{price}\n"
        f"åºå­ï¼{stock}\n"
    )

    if description:
        text += f"è¯´æï¼{description}\n"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "ð¬ ç«å³è¯¢ä»·",
                    callback_data=f"inquiry:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "â¬ï¸ è¿åç³»å",
                    callback_data=f"cat:{category}",
                )
            ],
        ]
    )

    photo_id = product.get("photo_id")

    if photo_id:
        try:
            await update.effective_message.reply_photo(
                photo=photo_id,
                caption=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
    )


async def show_all_products(update, context):
    products = get_products(active_only=True)

    if not products:
        await update.effective_message.reply_text("ææ ååã")
        return

    keyboard = []

    for product in products[:100]:
        title = product["name"]
        code = product.get("code") or ""
        if code:
            title += f" [{code}]"

        keyboard.append(
            [
                InlineKeyboardButton(
                    title[:60],
                    callback_data=f"product:{product['id']}",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("â¬ï¸ è¿å", callback_data="home")]
    )

    await update.effective_message.reply_text(
        "ð åååè¡¨ï¼",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start_inquiry(update, context, product_id=None):
    context.user_data["inquiry_product_id"] = product_id

    if product_id:
        product = get_product(product_id)
        if product:
            context.user_data["inquiry_product_name"] = product["name"]

    await update.effective_message.reply_text(
        "è¯·ç´æ¥åéä½ è¦è¯¢ä»·çåå®¹ã\n\n"
        "ä¾å¦ï¼\n"
        "æè¦è¯¢ä»· 10 ä»¶\n"
        "æåéï¼æ°é + åååç§°\n\n"
        "åé /cancel å¯ä»¥åæ¶ã"
    )


async def add_product_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("ä½ æ²¡æç®¡çåæéã")
        return

    context.user_data["add_step"] = "text"

    await update.effective_message.reply_text(
        "è¯·è¾å¥ååä¿¡æ¯ï¼æ ¼å¼ï¼\n\n"
        "åç§°|ç¼å·|åç±»|ä»·æ ¼|åºå­|æè¿°\n\n"
        "ä¾å¦ï¼\n"
        "ç¤ºä¾åå|P001|c1|100|20|ååè¯´æ\n\n"
        "åç±»å¯ä»¥å¡«å c1-c12ï¼ä¹å¯ä»¥å¡«åç³»ååç§°ã\n"
        "åé /cancel åæ¶ã"
    )


def parse_product_text(text):
    parts = [x.strip() for x in text.split("|")]

    if len(parts) < 5:
        return None, "æ ¼å¼ä¸æ­£ç¡®ï¼è³å°éè¦ 5 é¡¹ã"

    name = parts[0]
    code = parts[1]
    category = normalize_category(parts[2])
    price = parts[3]
    stock = parts[4]
    description = "|".join(parts[5:]).strip() if len(parts) > 5 else ""

    if not name:
        return None, "åååç§°ä¸è½ä¸ºç©ºã"

    if category not in CATS:
        return None, "åç±»éè¯¯ï¼è¯·ä½¿ç¨ c1-c12 ææ­£ç¡®çç³»ååç§°ã"

    return {
        "name": name,
        "code": code,
        "category": category,
        "price": price,
        "stock": stock,
        "description": description,
    }, None


async def handle_text(update, context):
    text = (update.message.text or "").strip()

    if text == "/cancel":
        await cancel(update, context)
        return

    add_step = context.user_data.get("add_step")

    if add_step == "text":
        if not is_admin(update.effective_user.id):
            context.user_data.pop("add_step", None)
            await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
            return

        data, error = parse_product_text(text)

        if error:
            await update.message.reply_text(error)
            return

        product_id = insert_product(**data)

        context.user_data["pending_product_id"] = product_id
        context.user_data["add_step"] = "photo"

        await update.message.reply_text(
            f"ååå·²æ·»å ï¼ç¼å· IDï¼{product_id}\n\n"
            "è¯·åéååå¾çã\n"
            "å¦æä¸éè¦å¾çï¼è¯·åéï¼è·³è¿"
        )
        return

    if add_step == "photo":
        if not is_admin(update.effective_user.id):
            context.user_data.clear()
            await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
            return

        if text == "è·³è¿":
            context.user_data.clear()
            await update.message.reply_text(
                "å·²è·³è¿å¾çï¼ååæ·»å å®æã",
                reply_markup=main_menu(),
            )
            return

        await update.message.reply_text(
            "è¯·åéååå¾çï¼æèåéï¼è·³è¿"
        )
        return

    inquiry_product_id = context.user_data.get("inquiry_product_id")

    if inquiry_product_id is not None or context.user_data.get("inquiry_product_name"):
        product_name = context.user_data.get("inquiry_product_name", "")

        if not product_name and inquiry_product_id:
            product = get_product(inquiry_product_id)
            product_name = product["name"] if product else ""

        username = update.effective_user.username or ""
        user_id = update.effective_user.id

        execute(
            """
            INSERT INTO inquiries
            (user_id, username, product, message, status)
            VALUES ({p}, {p}, {p}, {p}, {p})
            """.format(p=placeholder()),
            (
                user_id,
                username,
                product_name,
                text,
                "new",
            ),
        )

        context.user_data.pop("inquiry_product_id", None)
        context.user_data.pop("inquiry_product_name", None)

        await update.message.reply_text(
            "â å·²æ¶å°ä½ çè¯¢ä»·ä¿¡æ¯ã\n"
            "æä»¬ä¼å°½å¿«èç³»ä½ ã",
            reply_markup=main_menu(),
        )

        await notify_admins(
            context,
            product_name,
            text,
            username,
            user_id,
        )
        return

    await update.message.reply_text(
        "è¯·éæ©åè½ï¼",
        reply_markup=main_menu(),
    )


async def handle_photo(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
        return

    add_step = context.user_data.get("add_step")

    if add_step != "photo":
        await update.message.reply_text(
            "å¦æä½ è¦ç»ååæ·»å å¾çï¼è¯·åä½¿ç¨ï¼â æ·»å åå"
        )
        return

    product_id = context.user_data.get("pending_product_id")
    if not product_id:
        context.user_data.clear()
        await update.message.reply_text("æ²¡ææ¾å°å¾å¤çååã")
        return

    photos = update.message.photo

    if not photos:
        await update.message.reply_text("æ²¡ææ£æµå°å¾çï¼è¯·éæ°åéã")
        return

    telegram_photo = photos[-1]
    photo_id = telegram_photo.file_id

    execute(
        "UPDATE products SET photo_id = " + placeholder() + " WHERE id = " + placeholder(),
        (photo_id, product_id),
    )

    context.user_data.clear()

    await update.message.reply_text(
        "â ååå¾çå·²ä¿å­ï¼ååæ·»å å®æã",
        reply_markup=main_menu(),
    )


async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
        return

    total = product_count()

    active = fetch_one(
        "SELECT COUNT(*) AS c FROM products WHERE active = 1"
    )

    inquiries = fetch_one(
        "SELECT COUNT(*) AS c FROM inquiries WHERE status = 'new'"
    )

    text = (
        "ð  ç®¡çåå°\n\n"
        f"ååæ»æ°ï¼{total}\n"
        f"ä¸æ¶ååï¼{int(active['c']) if active else 0}\n"
        f"å¾å¤çè¯¢ä»·ï¼{int(inquiries['c']) if inquiries else 0}\n\n"
        "è¯·éæ©æä½ï¼"
    )

    keyboard = [
        [
            InlineKeyboardButton("ð ååç»è®¡", callback_data="admin:status"),
            InlineKeyboardButton("ð¥ å¯¼å¥CSV", callback_data="admin:csv"),
        ],
        [
            InlineKeyboardButton("ð¦ åååè¡¨", callback_data="admin:list"),
            InlineKeyboardButton("ð¬ è¯¢ä»·è®°å½", callback_data="admin:inquiries"),
        ],
        [
            InlineKeyboardButton("ð  è¿å", callback_data="home"),
        ],
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def notify_admins(context, product, message, username, user_id):
    if not ADMINS:
        return

    text = (
        "ð æ°è¯¢ä»·\n\n"
        f"ååï¼{product}\n"
        f"ç¨æ·ï¼@{username if username else 'æ ç¨æ·å'}\n"
        f"ç¨æ·IDï¼{user_id}\n"
        f"åå®¹ï¼{message}"
    )

    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            traceback.print_exc()


def notify_admins_sync(product, message, username):
    async def runner():
        if not BOT_TOKEN:
            return

        for admin_id in ADMINS:
            try:
                telegram_api(
                    "sendMessage",
                    {
                        "chat_id": admin_id,
                        "text": (
                            "ð æ°è¯¢ä»·\n\n"
                            f"ååï¼{product}\n"
                            f"ç¨æ·ï¼@{username if username else 'æ ç¨æ·å'}\n"
                            f"åå®¹ï¼{message}"
                        ),
                    },
                )
            except Exception:
                traceback.print_exc()

    try:
        asyncio.run(runner())
    except Exception:
        traceback.print_exc()


async def admin_status(update, context):
    total = product_count()

    active = fetch_one(
        "SELECT COUNT(*) AS c FROM products WHERE active = 1"
    )

    photos = fetch_one(
        "SELECT COUNT(*) AS c FROM products WHERE photo_id IS NOT NULL AND photo_id != ''"
    )

    await update.effective_message.reply_text(
        "ð ååç»è®¡\n\n"
        f"ååæ»æ°ï¼{total}\n"
        f"ä¸æ¶ååï¼{int(active['c']) if active else 0}\n"
        f"å·²æå¾çï¼{int(photos['c']) if photos else 0}\n"
        f"æ°æ®åºï¼{'PostgreSQL' if db_is_postgres() else 'SQLite'}"
    )


async def admin_csv(update, context):
    if not CSV_FILE.exists():
        await update.effective_message.reply_text(
            f"æ²¡ææ¾å° CSV æä»¶ï¼{CSV_FILE.name}"
        )
        return

    imported = import_csv_if_empty()

    await update.effective_message.reply_text(
        f"CSV æ£æ¥å®æã\n"
        f"æ¬æ¬¡æ°å¢ï¼{imported} æ¡"
    )


async def admin_list(update, context):
    products = get_products(active_only=False)

    if not products:
        await update.effective_message.reply_text("ææ ååã")
        return

    lines = ["ð¦ åååè¡¨"]

    for product in products[:100]:
        status = "ä¸æ¶" if int(product.get("active", 0)) == 1 else "ä¸æ¶"
        photo = "æå¾" if product.get("photo_id") else "æ å¾"

        lines.append(
            f"#{product['id']} {product['name']} "
            f"[{product.get('code', '')}] | {status} | {photo}"
        )

    await update.effective_message.reply_text("\n".join(lines))


async def admin_inquiries(update, context):
    rows = fetch_all(
        "SELECT * FROM inquiries ORDER BY id DESC LIMIT 50"
    )

    if not rows:
        await update.effective_message.reply_text("ææ è¯¢ä»·è®°å½ã")
        return

    lines = ["ð¬ æè¿è¯¢ä»·"]

    for row in rows:
        username = row.get("username") or "æ ç¨æ·å"
        status = row.get("status") or "new"

        lines.append(
            f"#{row['id']} | {username} | {row.get('product', '')}\n"
            f"{row.get('message', '')}\n"
            f"ç¶æï¼{status}"
        )

    await update.effective_message.reply_text(
        "\n\n".join(lines)
    )


def safe_extract_zip(zip_path, target_dir):
    target_dir = Path(target_dir).resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (target_dir / member.filename).resolve()

            if not str(member_path).startswith(str(target_dir)):
                raise ValueError("ZIP ååå«éæ³è·¯å¾")

            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
            else:
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, open(member_path, "wb") as target:
                    shutil.copyfileobj(source, target)


def image_files(directory):
    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    result = []

    for path in Path(directory).rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            result.append(path)

    return result


def find_image_for_product(product, files):
    product_id = str(product.get("id", ""))
    code = normalize_text(product.get("code", ""))
    name = normalize_text(product.get("name", ""))

    normalized = [(p, normalize_text(p.stem)) for p in files]

    for path, stem in normalized:
        if stem == product_id:
            return path

    if code:
        for path, stem in normalized:
            if stem == code:
                return path

    if name:
        for path, stem in normalized:
            if stem == name:
                return path

    if code:
        for path, stem in normalized:
            if code in stem:
                return path

    if name:
        for path, stem in normalized:
            if name in stem:
                return path

    return None


async def process_zip(update, context, document):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
        return

    if not document.file_name.lower().endswith(".zip"):
        await update.message.reply_text("è¯·ä¸ä¼  ZIP æä»¶ã")
        return

    status_message = await update.message.reply_text(
        "æ­£å¨ä¸è½½ ZIPï¼è¯·ç¨å..."
    )

    temp_root = Path(tempfile.mkdtemp(prefix="catalog_zip_"))
    zip_path = temp_root / "upload.zip"
    extract_dir = temp_root / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(zip_path))

        await status_message.edit_text(
            "ZIP å·²ä¸è½½ï¼æ­£å¨è§£åå¹¶å¹éååå¾ç..."
        )

        safe_extract_zip(zip_path, extract_dir)

        products = get_products(active_only=False)
        files = image_files(extract_dir)

        if not products:
            await status_message.edit_text("æ°æ®åºä¸­æ²¡æååï¼æ æ³å¹éå¾çã")
            return

        matched = 0
        failed = []

        for product in products:
            image_path = find_image_for_product(product, files)

            if not image_path:
                failed.append(product["name"])
                continue

            try:
                with open(image_path, "rb") as photo_file:
                    message = await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=photo_file,
                    )

                telegram_photo = message.photo[-1]

                execute(
                    "UPDATE products SET photo_id = "
                    + placeholder()
                    + " WHERE id = "
                    + placeholder(),
                    (
                        telegram_photo.file_id,
                        product["id"],
                    ),
                )

                matched += 1

            except Exception:
                traceback.print_exc()
                failed.append(product["name"])

        result = (
            "â ZIP å¾çå¤çå®æ\n\n"
            f"ååæ°éï¼{len(products)}\n"
            f"å¹éæåï¼{matched}\n"
            f"å¹éå¤±è´¥ï¼{len(failed)}"
        )

        if failed:
            result += "\n\næªå¹éååï¼\n" + "\n".join(failed[:30])

        await status_message.edit_text(result)

    except Exception as exc:
        traceback.print_exc()
        await status_message.edit_text(
            f"â ZIP å¤çå¤±è´¥ï¼{exc}"
        )

    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def handle_document(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ä½ æ²¡æç®¡çåæéã")
        return

    document = update.message.document

    if document.file_name.lower().endswith(".zip"):
        await process_zip(update, context, document)
        return

    if document.file_name.lower().endswith(".csv"):
        telegram_file = await context.bot.get_file(document.file_id)
        target = UPLOAD_DIR / "uploaded.csv"
        await telegram_file.download_to_drive(custom_path=str(target))

        await update.message.reply_text(
            f"CSV å·²ä¿å­ï¼{target.name}\n"
            "å¦ææ°æ®åºä¸ºç©ºï¼å¯ä»¥ä½¿ç¨ /admin å¯¼å¥ã"
        )
        return

    await update.message.reply_text(
        "æ¯æä¸ä¼  ZIP æ CSV æä»¶ã"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "home":
        await query.message.reply_text(
            "è¯·éæ©åè½ï¼",
            reply_markup=main_menu(),
        )
        return

    if data == "catalog":
        await show_catalog(update, context)
        return

    if data == "products":
        await show_all_products(update, context)
        return

    if data == "add":
        await add_product_start(update, context)
        return

    if data == "ask":
        await start_inquiry(update, context)
        return

    if data.startswith("cat:"):
        category = data.split(":", 1)[1]
        await show_category(update, context, category)
        return

    if data.startswith("product:"):
        raw_id = data.split(":", 1)[1]
        if raw_id.isdigit():
            await show_product(update, context, int(raw_id))
        return

    if data.startswith("inquiry:"):
        raw_id = data.split(":", 1)[1]
        if raw_id.isdigit():
            await start_inquiry(update, context, int(raw_id))
        return

    if data == "admin:status":
        if is_admin(update.effective_user.id):
            await admin_status(update, context)
        return

    if data == "admin:csv":
        if is_admin(update.effective_user.id):
            await admin_csv(update, context)
        return

    if data == "admin:list":
        if is_admin(update.effective_user.id):
            await admin_list(update, context)
        return

    if data == "admin:inquiries":
        if is_admin(update.effective_user.id):
            await admin_inquiries(update, context)
        return


async def error_handler(update, context):
    print("BOT ERROR:")
    traceback.print_exception(
        type(context.error),
        context.error,
        context.error.__traceback__,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Please set BOT_TOKEN in Render Environment Variables."
        )

    init_db()

    try:
        imported = import_csv_if_empty()
        print(f"CSV imported: {imported}")
    except Exception:
        traceback.print_exc()

    start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    application.add_error_handler(error_handler)

    print("Telegram bot starting...")
    print(f"Web URL: {WEB_URL}")
    print(f"Port: {PORT}")
    print(f"Admins: {ADMINS}")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
