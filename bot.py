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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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
    "Telegram\u5546\u54c1\u5bfc\u5165\u8868_93\u4e2a_\u53ef\u76f4\u63a5\u5bfc\u5165.csv"
)

CATS = {
    "c1": "\u548c\u5929\u4e0b\u7cfb\u5217",
    "c2": "\u718a\u732b\u7cfb\u5217",
    "c3": "\u5357\u4eac\u7cfb\u5217",
    "c4": "\u8377\u82b1\u7cfb\u5217",
    "c5": "\u8299\u84c9\u738b\u7cfb\u5217",
    "c6": "\u7261\u4e39\u7cfb\u5217",
    "c7": "\u9ec4\u91d1\u53f6\u7cfb\u5217",
    "c8": "\u82cf\u70df\u7cfb\u5217",
    "c9": "\u5229\u7fa4\u7cfb\u5217",
    "c10": "\u9ec4\u9e64\u697c\u7cfb\u5217",
    "c11": "\u4e2d\u534e\u7cfb\u5217",
    "c12": "\u767d\u76ae\u7cfb\u5217",
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
            name = str(row.get("name", row.get("\u540d\u79f0", ""))).strip()
            code = str(row.get("code", row.get("\u7f16\u53f7", ""))).strip()
            category = normalize_category(
                row.get("category", row.get("\u5206\u7c7b", ""))
            )
            price = str(row.get("price", row.get("\u4ef7\u683c", ""))).strip()
            stock = str(row.get("stock", row.get("\u5e93\u5b58", ""))).strip()
            description = str(
                row.get("description", row.get("\u63cf\u8ff0", ""))
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


def update_product(product_id, name, code, category, price, stock, description):
    execute(
        """UPDATE products SET name = {p}, code = {p}, category = {p}, price = {p}, stock = {p}, description = {p} WHERE id = {p}""".format(p=placeholder()),
        (name, code, category, price, stock, description, product_id),
    )


def set_product_active(product_id, active):
    execute(
        "UPDATE products SET active = " + placeholder() + " WHERE id = " + placeholder(),
        (1 if active else 0, product_id),
    )


def delete_product(product_id):
    execute(
        "DELETE FROM products WHERE id = " + placeholder(),
        (product_id,),
    )


def set_product_photo(product_id, photo_id):
    execute(
        "UPDATE products SET photo_id = " + placeholder() + " WHERE id = " + placeholder(),
        (photo_id, product_id),
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
                self._json({"ok": False, "error": "\u8bf7\u9009\u62e9\u5546\u54c1"}, 400)
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
        f"\U0001f4e6 {name}\n"
        f"\u7f16\u53f7\uff1a{code}\n"
        f"\u5206\u7c7b\uff1a{CATS.get(category, category)}\n"
        f"\u4ef7\u683c\uff1a{price}\n"
        f"\u5e93\u5b58\uff1a{stock}\n"
    )

    if description:
        text += f"\u8bf4\u660e\uff1a{description}\n"

    keyboard = [
        [
            InlineKeyboardButton(
                "\U0001f4ac \u8be2\u4ef7",
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
                InlineKeyboardButton("\U0001f6cd\ufe0f \u5546\u54c1\u76ee\u5f55", callback_data="catalog"),
                InlineKeyboardButton("\U0001f4cb \u5546\u54c1", callback_data="products"),
            ],
            [
                InlineKeyboardButton("\U0001f4ac \u8be2\u4ef7", callback_data="ask"),
                InlineKeyboardButton("\u2795 \u6dfb\u52a0\u5546\u54c1", callback_data="add"),
            ],
            [
                InlineKeyboardButton("\U0001f310 \u6253\u5f00\u5546\u54c1\u5c0f\u7a0b\u5e8f", web_app=WebAppInfo(url=WEB_URL)),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add_step", None)
    context.user_data.pop("pending_product_id", None)

    text = (
        "\u6b22\u8fce\u4f7f\u7528\u5546\u54c1\u76ee\u5f55\u673a\u5668\u4eba\n\n"
        "\u8bf7\u9009\u62e9\u529f\u80fd\uff1a"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "\u5df2\u53d6\u6d88\u5f53\u524d\u64cd\u4f5c\u3002",
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
        [InlineKeyboardButton("\u2b05\ufe0f \u8fd4\u56de", callback_data="home")]
    )

    await update.effective_message.reply_text(
        "\u8bf7\u9009\u62e9\u5546\u54c1\u7cfb\u5217\uff1a",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_category(update, context, category):
    products = get_products(category=category, active_only=True)

    if not products:
        await update.effective_message.reply_text(
            "\u8fd9\u4e2a\u7cfb\u5217\u6682\u65f6\u6ca1\u6709\u4e0a\u67b6\u5546\u54c1\u3002",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2b05\ufe0f \u8fd4\u56de\u76ee\u5f55", callback_data="catalog")]]
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
        [InlineKeyboardButton("\u2b05\ufe0f \u8fd4\u56de\u76ee\u5f55", callback_data="catalog")]
    )

    await update.effective_message.reply_text(
        f"\U0001f4c2 {CATS.get(category, category)}\n\n\u8bf7\u9009\u62e9\u5546\u54c1\uff1a",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_product(update, context, product_id):
    product = get_product(product_id)

    if not product or int(product.get("active", 0)) != 1:
        await update.effective_message.reply_text("\u5546\u54c1\u4e0d\u5b58\u5728\u6216\u5df2\u4e0b\u67b6\u3002")
        return

    name = product.get("name", "")
    code = product.get("code", "")
    category = product.get("category", "")
    price = product.get("price", "")
    stock = product.get("stock", "")
    description = product.get("description", "")

    text = (
        f"\U0001f4e6 {name}\n"
        f"\u7f16\u53f7\uff1a{code}\n"
        f"\u5206\u7c7b\uff1a{CATS.get(category, category)}\n"
        f"\u4ef7\u683c\uff1a{price}\n"
        f"\u5e93\u5b58\uff1a{stock}\n"
    )

    if description:
        text += f"\u8bf4\u660e\uff1a{description}\n"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f4ac \u7acb\u5373\u8be2\u4ef7",
                    callback_data=f"inquiry:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "\u2b05\ufe0f \u8fd4\u56de\u7cfb\u5217",
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
        await update.effective_message.reply_text("\u6682\u65e0\u5546\u54c1\u3002")
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
        [InlineKeyboardButton("\u2b05\ufe0f \u8fd4\u56de", callback_data="home")]
    )

    await update.effective_message.reply_text(
        "\U0001f4cb \u5546\u54c1\u5217\u8868\uff1a",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start_inquiry(update, context, product_id=None):
    context.user_data["inquiry_product_id"] = product_id

    if product_id:
        product = get_product(product_id)
        if product:
            context.user_data["inquiry_product_name"] = product["name"]

    await update.effective_message.reply_text(
        "\u8bf7\u76f4\u63a5\u53d1\u9001\u4f60\u8981\u8be2\u4ef7\u7684\u5185\u5bb9\u3002\n\n"
        "\u4f8b\u5982\uff1a\n"
        "\u6211\u8981\u8be2\u4ef7 10 \u4ef6\n"
        "\u6216\u53d1\u9001\uff1a\u6570\u91cf + \u5546\u54c1\u540d\u79f0\n\n"
        "\u53d1\u9001 /cancel \u53ef\u4ee5\u53d6\u6d88\u3002"
    )


async def add_product_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    context.user_data["add_step"] = "text"

    await update.effective_message.reply_text(
        "\u8bf7\u8f93\u5165\u5546\u54c1\u4fe1\u606f\uff0c\u683c\u5f0f\uff1a\n\n"
        "\u540d\u79f0|\u7f16\u53f7|\u5206\u7c7b|\u4ef7\u683c|\u5e93\u5b58|\u63cf\u8ff0\n\n"
        "\u4f8b\u5982\uff1a\n"
        "\u793a\u4f8b\u5546\u54c1|P001|c1|100|20|\u5546\u54c1\u8bf4\u660e\n\n"
        "\u5206\u7c7b\u53ef\u4ee5\u586b\u5199 c1-c12\uff0c\u4e5f\u53ef\u4ee5\u586b\u5199\u7cfb\u5217\u540d\u79f0\u3002\n"
        "\u53d1\u9001 /cancel \u53d6\u6d88\u3002"
    )


def parse_product_text(text):
    parts = [x.strip() for x in text.split("|")]

    if len(parts) < 5:
        return None, "\u683c\u5f0f\u4e0d\u6b63\u786e\uff0c\u81f3\u5c11\u9700\u8981 5 \u9879\u3002"

    name = parts[0]
    code = parts[1]
    category = normalize_category(parts[2])
    price = parts[3]
    stock = parts[4]
    description = "|".join(parts[5:]).strip() if len(parts) > 5 else ""

    if not name:
        return None, "\u5546\u54c1\u540d\u79f0\u4e0d\u80fd\u4e3a\u7a7a\u3002"

    if category not in CATS:
        return None, "\u5206\u7c7b\u9519\u8bef\uff0c\u8bf7\u4f7f\u7528 c1-c12 \u6216\u6b63\u786e\u7684\u7cfb\u5217\u540d\u79f0\u3002"

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
            await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
            return

        data, error = parse_product_text(text)

        if error:
            await update.message.reply_text(error)
            return

        product_id = insert_product(**data)

        context.user_data["pending_product_id"] = product_id
        context.user_data["add_step"] = "photo"

        await update.message.reply_text(
            f"\u5546\u54c1\u5df2\u6dfb\u52a0\uff0c\u7f16\u53f7 ID\uff1a{product_id}\n\n"
            "\u8bf7\u53d1\u9001\u5546\u54c1\u56fe\u7247\u3002\n"
            "\u5982\u679c\u4e0d\u9700\u8981\u56fe\u7247\uff0c\u8bf7\u53d1\u9001\uff1a\u8df3\u8fc7"
        )
        return

    if add_step == "photo":
        if not is_admin(update.effective_user.id):
            context.user_data.clear()
            await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
            return

        if text == "\u8df3\u8fc7":
            context.user_data.clear()
            await update.message.reply_text(
                "\u5df2\u8df3\u8fc7\u56fe\u7247\uff0c\u5546\u54c1\u6dfb\u52a0\u5b8c\u6210\u3002",
                reply_markup=main_menu(),
            )
            return

        await update.message.reply_text(
            "\u8bf7\u53d1\u9001\u5546\u54c1\u56fe\u7247\uff0c\u6216\u8005\u53d1\u9001\uff1a\u8df3\u8fc7"
        )
        return

    if context.user_data.get("admin_edit_step") == "text":
        if not is_admin(update.effective_user.id):
            context.user_data.clear()
            return
        product_id = context.user_data.get("admin_edit_product_id")
        data, error = parse_product_text(text)
        if error:
            await update.message.reply_text(error)
            return
        if not get_product(product_id):
            context.user_data.clear()
            await update.message.reply_text("ååä¸å­å¨ï¼ç¼è¾å·²åæ¶ã")
            return
        update_product(product_id, **data)
        context.user_data.clear()
        await update.message.reply_text("â ååä¿¡æ¯å·²æ´æ°ã", reply_markup=main_menu())
        return

    if context.user_data.get("admin_edit_step") == "photo":
        await update.message.reply_text("è¯·åéå¾çï¼ä¸è¦åéæå­ã")
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
            "\u2705 \u5df2\u6536\u5230\u4f60\u7684\u8be2\u4ef7\u4fe1\u606f\u3002\n"
            "\u6211\u4eec\u4f1a\u5c3d\u5feb\u8054\u7cfb\u4f60\u3002",
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
        "\u8bf7\u9009\u62e9\u529f\u80fd\uff1a",
        reply_markup=main_menu(),
    )


async def handle_photo(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    if context.user_data.get("admin_edit_step") == "photo":
        product_id = context.user_data.get("admin_photo_product_id")
        if not product_id or not get_product(product_id):
            context.user_data.clear()
            await update.message.reply_text("ååä¸å­å¨ï¼æä½å·²åæ¶ã")
            return
        telegram_photo = update.message.photo[-1]
        set_product_photo(product_id, telegram_photo.file_id)
        try:
            await update.message.delete()
        except Exception as exc:
            print(f"[PHOTO] å é¤æ´æ¢å¾çæ¶æ¯å¤±è´¥: {exc}")
        context.user_data.clear()
        await update.effective_chat.send_message("â ååå¾çå·²æ´æ¢ã", reply_markup=main_menu())
        return

    add_step = context.user_data.get("add_step")

    if add_step != "photo":
        await update.message.reply_text(
            "\u5982\u679c\u4f60\u8981\u7ed9\u5546\u54c1\u6dfb\u52a0\u56fe\u7247\uff0c\u8bf7\u5148\u4f7f\u7528\uff1a\u2795 \u6dfb\u52a0\u5546\u54c1"
        )
        return

    product_id = context.user_data.get("pending_product_id")
    if not product_id:
        context.user_data.clear()
        await update.message.reply_text("\u6ca1\u6709\u627e\u5230\u5f85\u5904\u7406\u5546\u54c1\u3002")
        return

    photos = update.message.photo

    if not photos:
        await update.message.reply_text("\u6ca1\u6709\u68c0\u6d4b\u5230\u56fe\u7247\uff0c\u8bf7\u91cd\u65b0\u53d1\u9001\u3002")
        return

    telegram_photo = photos[-1]
    photo_id = telegram_photo.file_id

    execute(
        "UPDATE products SET photo_id = " + placeholder() + " WHERE id = " + placeholder(),
        (photo_id, product_id),
    )

    # å¾çä¿å­æååï¼èªå¨å é¤ç®¡çåååéçå¾çæ¶æ¯
    try:
        await update.message.delete()
    except Exception as exc:
        print(f"[PHOTO] å é¤ä¸ä¼ å¾çæ¶æ¯å¤±è´¥: {exc}")

    context.user_data.clear()

    await update.message.reply_text(
        "\u2705 \u5546\u54c1\u56fe\u7247\u5df2\u4fdd\u5b58\uff0c\u5546\u54c1\u6dfb\u52a0\u5b8c\u6210\u3002",
        reply_markup=main_menu(),
    )


async def admin_edit_start(update, context, product_id):
    if not is_admin(update.effective_user.id):
        return
    product = get_product(product_id)
    if not product:
        await update.effective_message.reply_text("ååä¸å­å¨ã")
        return
    context.user_data.clear()
    context.user_data["admin_edit_product_id"] = product_id
    context.user_data["admin_edit_step"] = "text"
    await update.effective_message.reply_text(
        "è¯·åéæ°çååä¿¡æ¯ï¼æ ¼å¼ï¼\n\n"
        "åç§°|ç¼å·|åç±»|ä»·æ ¼|åºå­|æè¿°\n\n"
        f"å½åï¼{product.get('name','')}|{product.get('code','')}|{product.get('category','')}|{product.get('price','')}|{product.get('stock','')}|{product.get('description','') or ''}\n\n"
        "åç±»ä½¿ç¨ c1-c12ãåé /cancel åæ¶ã"
    )


async def admin_replace_photo_start(update, context, product_id):
    if not is_admin(update.effective_user.id):
        return
    product = get_product(product_id)
    if not product:
        await update.effective_message.reply_text("ååä¸å­å¨ã")
        return
    context.user_data.clear()
    context.user_data["admin_photo_product_id"] = product_id
    context.user_data["admin_edit_step"] = "photo"
    await update.effective_message.reply_text(
        f"è¯·åéååã{product.get('name','')}ãçæ°å¾çã\nåé /cancel åæ¶ã"
    )


async def admin_toggle_product(update, context, product_id):
    if not is_admin(update.effective_user.id):
        return
    product = get_product(product_id)
    if not product:
        await update.effective_message.reply_text("ååä¸å­å¨ã")
        return
    new_active = 0 if int(product.get("active", 0)) == 1 else 1
    set_product_active(product_id, new_active)
    await update.effective_message.reply_text(
        f"â {product.get('name','')} å·²{'ä¸æ¶' if new_active else 'ä¸æ¶'}ã"
    )
    await admin_list(update, context)


async def admin_delete_confirm(update, context, product_id):
    if not is_admin(update.effective_user.id):
        return
    product = get_product(product_id)
    if not product:
        await update.effective_message.reply_text("ååä¸å­å¨ã")
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("â ï¸ ç¡®è®¤å é¤", callback_data=f"admin:delok:{product_id}"),
            InlineKeyboardButton("åæ¶", callback_data="admin:list"),
        ]
    ])
    await update.effective_message.reply_text(
        f"ç¡®å®å é¤ååï¼\n\n#{product_id} {product.get('name','')} [{product.get('code','')}]\n\nå é¤åæ°æ®åºä¸­çååè®°å½ä¼è¢«å é¤ã",
        reply_markup=keyboard,
    )


async def admin_delete_product(update, context, product_id):
    if not is_admin(update.effective_user.id):
        return
    product = get_product(product_id)
    if not product:
        await update.effective_message.reply_text("ååå·²ç»ä¸å­å¨ã")
        return
    delete_product(product_id)
    await update.effective_message.reply_text(f"â å·²å é¤ï¼{product.get('name','')} [{product.get('code','')}]")
    await admin_list(update, context)


async def admin_mark_inquiry(update, context, inquiry_id, status):
    if not is_admin(update.effective_user.id):
        return
    execute(
        "UPDATE inquiries SET status = " + placeholder() + " WHERE id = " + placeholder(),
        (status, inquiry_id),
    )
    await admin_inquiries(update, context)


async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    total = product_count()

    active = fetch_one(
        "SELECT COUNT(*) AS c FROM products WHERE active = 1"
    )

    inquiries = fetch_one(
        "SELECT COUNT(*) AS c FROM inquiries WHERE status = 'new'"
    )

    text = (
        "\U0001f6e0 \u7ba1\u7406\u540e\u53f0\n\n"
        f"\u5546\u54c1\u603b\u6570\uff1a{total}\n"
        f"\u4e0a\u67b6\u5546\u54c1\uff1a{int(active['c']) if active else 0}\n"
        f"\u5f85\u5904\u7406\u8be2\u4ef7\uff1a{int(inquiries['c']) if inquiries else 0}\n\n"
        "\u8bf7\u9009\u62e9\u64cd\u4f5c\uff1a"
    )

    keyboard = [
        [
            InlineKeyboardButton("\U0001f4ca \u5546\u54c1\u7edf\u8ba1", callback_data="admin:status"),
            InlineKeyboardButton("\U0001f4e5 \u53ea\u5bfc\u5165CSV", callback_data="admin:csv"),
        ],
        [
            InlineKeyboardButton("\U0001f4e6 \u4e00\u952e\u4e0a\u4f20\u5546\u54c1+\u56fe\u7247", callback_data="admin:zip_import"),
        ],
        [
            InlineKeyboardButton("\U0001f4e6 \u5546\u54c1\u5217\u8868", callback_data="admin:list"),
            InlineKeyboardButton("\U0001f4ac \u8be2\u4ef7\u8bb0\u5f55", callback_data="admin:inquiries"),
        ],
        [
            InlineKeyboardButton("\U0001f3e0 \u8fd4\u56de", callback_data="home"),
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
        "\U0001f514 \u65b0\u8be2\u4ef7\n\n"
        f"\u5546\u54c1\uff1a{product}\n"
        f"\u7528\u6237\uff1a@{username if username else '\u65e0\u7528\u6237\u540d'}\n"
        f"\u7528\u6237ID\uff1a{user_id}\n"
        f"\u5185\u5bb9\uff1a{message}"
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
                            "\U0001f514 \u65b0\u8be2\u4ef7\n\n"
                            f"\u5546\u54c1\uff1a{product}\n"
                            f"\u7528\u6237\uff1a@{username if username else '\u65e0\u7528\u6237\u540d'}\n"
                            f"\u5185\u5bb9\uff1a{message}"
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
        "\U0001f4ca \u5546\u54c1\u7edf\u8ba1\n\n"
        f"\u5546\u54c1\u603b\u6570\uff1a{total}\n"
        f"\u4e0a\u67b6\u5546\u54c1\uff1a{int(active['c']) if active else 0}\n"
        f"\u5df2\u6709\u56fe\u7247\uff1a{int(photos['c']) if photos else 0}\n"
        f"\u6570\u636e\u5e93\uff1a{'PostgreSQL' if db_is_postgres() else 'SQLite'}"
    )


async def admin_csv(update, context):
    if not CSV_FILE.exists():
        await update.effective_message.reply_text(
            f"\u6ca1\u6709\u627e\u5230 CSV \u6587\u4ef6\uff1a{CSV_FILE.name}"
        )
        return

    imported = import_csv_if_empty()

    await update.effective_message.reply_text(
        f"CSV \u68c0\u67e5\u5b8c\u6210\u3002\n"
        f"\u672c\u6b21\u65b0\u589e\uff1a{imported} \u6761"
    )


async def admin_list(update, context):
    if not is_admin(update.effective_user.id):
        return
    products = get_products(active_only=False)

    if not products:
        await update.effective_message.reply_text("ææ ååã")
        return

    # Telegram åæ¡æ¶æ¯æé®æ°éæ§å¶ï¼æå¤å±ç¤ºå100ä¸ªååã
    keyboard = []
    for product in products[:100]:
        pid = int(product["id"])
        name = str(product.get("name") or "")
        code = str(product.get("code") or "")
        status = "ä¸æ¶" if int(product.get("active", 0)) == 1 else "ä¸æ¶"
        keyboard.append([InlineKeyboardButton(
            f"#{pid} {name[:28]} [{status}]", callback_data=f"admin:edit:{pid}"
        )])

    keyboard.append([InlineKeyboardButton("â¬ï¸ è¿ååå°", callback_data="admin:home")])
    await update.effective_message.reply_text(
        f"ð¦ ååç®¡çï¼å± {len(products)} ä¸ªï¼\nç¹å»ååè¿å¥ç¼è¾ï¼",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_inquiries(update, context):
    if not is_admin(update.effective_user.id):
        return
    rows = fetch_all("SELECT * FROM inquiries ORDER BY id DESC LIMIT 50")
    if not rows:
        await update.effective_message.reply_text("ææ è¯¢ä»·è®°å½ã")
        return

    keyboard = []
    for row in rows:
        iid = int(row["id"])
        status = row.get("status") or "new"
        status_text = "å¾å¤ç" if status == "new" else "å·²å¤ç"
        product = str(row.get("product") or "æªæå®")
        username = row.get("username") or "æ ç¨æ·å"
        title = f"#{iid} {product[:20]} | {status_text}"
        keyboard.append([InlineKeyboardButton(title, callback_data=f"admin:inq:{iid}")])

    keyboard.append([InlineKeyboardButton("â¬ï¸ è¿ååå°", callback_data="admin:home")])
    await update.effective_message.reply_text(
        "ð¬ è¯¢ä»·ç®¡çï¼æè¿50æ¡ï¼\nç¹å»è®°å½æ¥çè¯¦æï¼",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_inquiry_detail(update, context, inquiry_id):
    if not is_admin(update.effective_user.id):
        return
    row = fetch_one("SELECT * FROM inquiries WHERE id = " + placeholder(), (inquiry_id,))
    if not row:
        await update.effective_message.reply_text("è¯¢ä»·è®°å½ä¸å­å¨ã")
        return
    username = row.get("username") or "æ ç¨æ·å"
    status = row.get("status") or "new"
    status_text = "å¾å¤ç" if status == "new" else "å·²å¤ç"
    text = (
        f"ð¬ è¯¢ä»· #{inquiry_id}\n\n"
        f"ååï¼{row.get('product') or 'æªæå®'}\n"
        f"ç¨æ·ï¼@{username}\n"
        f"ç¨æ·IDï¼{row.get('user_id') or ''}\n"
        f"ç¶æï¼{status_text}\n"
        f"æ¶é´ï¼{row.get('created_at') or ''}\n\n"
        f"åå®¹ï¼{row.get('message') or ''}"
    )
    next_status = "done" if status == "new" else "new"
    next_text = "â æ è®°å·²å¤ç" if status == "new" else "â©ï¸ æ¢å¤å¾å¤ç"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(next_text, callback_data=f"admin:inqstatus:{inquiry_id}:{next_status}")],
        [InlineKeyboardButton("â¬ï¸ è¿åè¯¢ä»·", callback_data="admin:inquiries")],
    ])
    await update.effective_message.reply_text(text, reply_markup=keyboard)


def safe_extract_zip(zip_path, target_dir):
    target_dir = Path(target_dir).resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (target_dir / member.filename).resolve()

            if not str(member_path).startswith(str(target_dir)):
                raise ValueError("ZIP \u5305\u5305\u542b\u975e\u6cd5\u8def\u5f84")

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



async def process_zip_import(update, context, document):
    """Import one CSV plus numbered product images from a ZIP.

    Expected ZIP:
      - one CSV containing exactly 93 product rows
      - images named 01..93
    Mapping:
      01 -> P001, ..., 93 -> P093
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    if not document.file_name or not document.file_name.lower().endswith(".zip"):
        await update.message.reply_text("\u8bf7\u4e0a\u4f20 ZIP \u6587\u4ef6\u3002")
        return

    status_message = await update.message.reply_text(
        "\U0001f4e6 \u6b63\u5728\u4e0b\u8f7d ZIP\uff0c\u8bf7\u7a0d\u5019..."
    )

    temp_root = Path(tempfile.mkdtemp(prefix="catalog_full_zip_"))
    zip_path = temp_root / "upload.zip"
    extract_dir = temp_root / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(zip_path))
        await status_message.edit_text(
            "\u2705 ZIP \u5df2\u4e0b\u8f7d\uff0c\u6b63\u5728\u68c0\u67e5 CSV \u548c 93 \u5f20\u56fe\u7247..."
        )
        safe_extract_zip(zip_path, extract_dir)

        csv_files = [
            x for x in extract_dir.rglob("*")
            if x.is_file() and x.suffix.lower() == ".csv"
        ]
        if len(csv_files) != 1:
            await status_message.edit_text(
                f"\u274c ZIP \u4e2d\u5fc5\u987b\u6709\u4e14\u53ea\u80fd\u6709 1 \u4e2a CSV\uff0c\u5f53\u524d\u627e\u5230 {len(csv_files)} \u4e2a\u3002"
            )
            return

        csv_path = csv_files[0]
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if len(rows) != 93:
            await status_message.edit_text(
                f"\u274c CSV \u5fc5\u987b\u662f 93 \u6761\u5546\u54c1\uff0c\u5f53\u524d\u662f {len(rows)} \u6761\u3002\n\u672c\u6b21\u672a\u5bfc\u5165\u3002"
            )
            return

        parsed = []
        seen_codes = set()
        for index, row in enumerate(rows, start=1):
            name = str(row.get("name", row.get("\u540d\u79f0", ""))).strip()
            code = str(row.get("code", row.get("\u7f16\u53f7", ""))).strip().upper()
            category = normalize_category(row.get("category", row.get("\u5206\u7c7b", "")))
            price = str(row.get("price", row.get("\u4ef7\u683c", ""))).strip()
            stock = str(row.get("stock", row.get("\u5e93\u5b58", ""))).strip()
            description = str(row.get("description", row.get("\u63cf\u8ff0", ""))).strip()

            if not name or not code or category not in CATS:
                await status_message.edit_text(
                    f"\u274c CSV \u7b2c {index} \u884c\u6570\u636e\u4e0d\u6b63\u786e\u3002\n\u9700\u8981\uff1aname, code, category, price, stock\u3002"
                )
                return
            if code in seen_codes:
                await status_message.edit_text(f"\u274c CSV \u4e2d\u5b58\u5728\u91cd\u590d\u7f16\u53f7\uff1a{code}")
                return
            seen_codes.add(code)
            parsed.append({"name": name, "code": code, "category": category, "price": price, "stock": stock, "description": description})

        by_code = {row["code"]: row for row in parsed}
        missing_codes = [f"P{i:03d}" for i in range(1, 94) if f"P{i:03d}" not in by_code]
        if missing_codes:
            await status_message.edit_text(
                "\u274c CSV \u7f16\u53f7\u5fc5\u987b\u5305\u542b P001 \u5230 P093\u3002\n\u7f3a\u5c11\uff1a" + ", ".join(missing_codes[:30])
            )
            return

        image_by_number = {}
        for path in extract_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if re.fullmatch(r"\d{1,3}", path.stem.strip()):
                number = int(path.stem.strip())
                if 1 <= number <= 93:
                    image_by_number[number] = path

        missing_images = [f"{i:02d}" for i in range(1, 94) if i not in image_by_number]
        if missing_images:
            await status_message.edit_text(
                "\u274c ZIP \u56fe\u7247\u4e0d\u5b8c\u6574\uff0c\u7f3a\u5c11\uff1a" + ", ".join(missing_images[:30]) +
                (" \u7b49" if len(missing_images) > 30 else "") +
                "\n\u5fc5\u987b\u6709 01 \u5230 93 \u5171 93 \u5f20\u56fe\u7247\u3002\n\u672c\u6b21\u672a\u5bfc\u5165\u3002"
            )
            return

        existing = product_count()
        if existing > 0:
            await status_message.edit_text(
                f"\u26a0\ufe0f \u6570\u636e\u5e93\u5f53\u524d\u5df2\u6709 {existing} \u4e2a\u5546\u54c1\u3002\n"
                "\u4e3a\u9632\u6b62\u91cd\u590d\u5bfc\u5165\uff0c\u672c\u6b21\u6ca1\u6709\u5bfc\u5165\u3002\n"
                "\u5982\u9700\u91cd\u65b0\u5bfc\u5165\uff0c\u8bf7\u5148\u5904\u7406\u73b0\u6709\u5546\u54c1\u6570\u636e\u3002"
            )
            return

        await status_message.edit_text("\u2705 \u68c0\u67e5\u901a\u8fc7\uff0c\u5f00\u59cb\u5bfc\u5165 93 \u4e2a\u5546\u54c1\u548c\u56fe\u7247...")

        imported = 0
        matched = 0
        failed = []
        created_ids = []

        for i in range(1, 94):
            code = f"P{i:03d}"
            row = by_code[code]
            product_id = insert_product(
                name=row["name"], code=row["code"], category=row["category"],
                price=row["price"], stock=row["stock"],
                description=row["description"], active=1,
            )
            created_ids.append(product_id)
            imported += 1

            try:
                with open(image_by_number[i], "rb") as photo_file:
                    sent = await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=photo_file,
                        caption=f"P{i:03d} {row['name']}",
                    )
                telegram_photo = sent.photo[-1]
                execute(
                    "UPDATE products SET photo_id = " + placeholder() + " WHERE id = " + placeholder(),
                    (telegram_photo.file_id, product_id),
                )
                matched += 1
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=sent.message_id,
                    )
                except Exception as exc:
                    print(f"[ZIP] å é¤ä¸´æ¶ååå¾çæ¶æ¯å¤±è´¥: {exc}")
            except Exception:
                traceback.print_exc()
                failed.append(code)

            if i % 5 == 0 or i == 93:
                try:
                    await status_message.edit_text(
                        f"\u23f3 \u5df2\u5904\u7406 {i}/93\n\u5546\u54c1\uff1a{imported}\n\u56fe\u7247\uff1a{matched}"
                    )
                except Exception:
                    pass

        result = (
            "\u2705 \u4e00\u952e\u4e0a\u4f20\u5b8c\u6210\n\n"
            f"\u5546\u54c1\u5bfc\u5165\uff1a{imported}/93\n"
            f"\u56fe\u7247\u4fdd\u5b58\uff1a{matched}/93"
        )
        if failed:
            result += "\n\n\u274c \u56fe\u7247\u4e0a\u4f20\u5931\u8d25\uff1a" + ", ".join(failed[:30])
        await status_message.edit_text(result)
        try:
            await update.message.delete()
        except Exception as exc:
            print(f"[ZIP] å é¤ä¸ä¼ ZIPæ¶æ¯å¤±è´¥: {exc}")

    except Exception as exc:
        traceback.print_exc()
        try:
            await status_message.edit_text(f"\u274c \u4e00\u952e\u4e0a\u4f20\u5931\u8d25\uff1a{exc}")
        except Exception:
            pass
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def process_zip(update, context, document):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    if not document.file_name.lower().endswith(".zip"):
        await update.message.reply_text("\u8bf7\u4e0a\u4f20 ZIP \u6587\u4ef6\u3002")
        return

    status_message = await update.message.reply_text(
        "\u6b63\u5728\u4e0b\u8f7d ZIP\uff0c\u8bf7\u7a0d\u5019..."
    )

    temp_root = Path(tempfile.mkdtemp(prefix="catalog_zip_"))
    zip_path = temp_root / "upload.zip"
    extract_dir = temp_root / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(zip_path))

        await status_message.edit_text(
            "ZIP \u5df2\u4e0b\u8f7d\uff0c\u6b63\u5728\u89e3\u538b\u5e76\u5339\u914d\u5546\u54c1\u56fe\u7247..."
        )

        safe_extract_zip(zip_path, extract_dir)

        products = get_products(active_only=False)
        files = image_files(extract_dir)

        if not products:
            await status_message.edit_text("\u6570\u636e\u5e93\u4e2d\u6ca1\u6709\u5546\u54c1\uff0c\u65e0\u6cd5\u5339\u914d\u56fe\u7247\u3002")
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
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=message.message_id,
                    )
                except Exception as exc:
                    print(f"[ZIP] å é¤ä¸´æ¶å¾çæ¶æ¯å¤±è´¥: {exc}")

            except Exception:
                traceback.print_exc()
                failed.append(product["name"])

        result = (
            "\u2705 ZIP \u56fe\u7247\u5904\u7406\u5b8c\u6210\n\n"
            f"\u5546\u54c1\u6570\u91cf\uff1a{len(products)}\n"
            f"\u5339\u914d\u6210\u529f\uff1a{matched}\n"
            f"\u5339\u914d\u5931\u8d25\uff1a{len(failed)}"
        )

        if failed:
            result += "\n\n\u672a\u5339\u914d\u5546\u54c1\uff1a\n" + "\n".join(failed[:30])

        await status_message.edit_text(result)
        try:
            await update.message.delete()
        except Exception as exc:
            print(f"[ZIP] å é¤ä¸ä¼ ZIPæ¶æ¯å¤±è´¥: {exc}")

    except Exception as exc:
        traceback.print_exc()
        await status_message.edit_text(
            f"\u274c ZIP \u5904\u7406\u5931\u8d25\uff1a{exc}"
        )

    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def handle_document(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u4f60\u6ca1\u6709\u7ba1\u7406\u5458\u6743\u9650\u3002")
        return

    document = update.message.document

    if document.file_name.lower().endswith(".zip"):
        await process_zip_import(update, context, document)
        return

    if document.file_name.lower().endswith(".csv"):
        telegram_file = await context.bot.get_file(document.file_id)
        target = UPLOAD_DIR / "uploaded.csv"
        await telegram_file.download_to_drive(custom_path=str(target))

        await update.message.reply_text(
            f"CSV \u5df2\u4fdd\u5b58\uff1a{target.name}\n"
            "\u5982\u679c\u6570\u636e\u5e93\u4e3a\u7a7a\uff0c\u53ef\u4ee5\u4f7f\u7528 /admin \u5bfc\u5165\u3002"
        )
        try:
            await update.message.delete()
        except Exception as exc:
            print(f"[CSV] å é¤ä¸ä¼ CSVæ¶æ¯å¤±è´¥: {exc}")
        return

    await update.message.reply_text(
        "\u652f\u6301\u4e0a\u4f20 ZIP \u6216 CSV \u6587\u4ef6\u3002"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if data == "home":
        await query.message.reply_text(
            "\u8bf7\u9009\u62e9\u529f\u80fd\uff1a",
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

    if data == "admin:home":
        if is_admin(update.effective_user.id):
            await admin_command(update, context)
        return

    if data.startswith("admin:edit:"):
        if is_admin(update.effective_user.id):
            pid = data.split(":")[-1]
            if pid.isdigit():
                product = get_product(int(pid))
                if product:
                    status = "ä¸æ¶" if int(product.get("active", 0)) == 1 else "ä¸æ¶"
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("âï¸ ç¼è¾èµæ", callback_data=f"admin:edittext:{pid}"),
                            InlineKeyboardButton("ð¼ æ´æ¢å¾ç", callback_data=f"admin:photo:{pid}"),
                        ],
                        [
                            InlineKeyboardButton("â¬ï¸/â¬ï¸ ä¸ä¸æ¶", callback_data=f"admin:toggle:{pid}"),
                            InlineKeyboardButton("ð å é¤", callback_data=f"admin:delete:{pid}"),
                        ],
                        [InlineKeyboardButton("â¬ï¸ è¿ååååè¡¨", callback_data="admin:list")],
                    ])
                    await query.message.reply_text(
                        f"ð¦ #{pid} {product.get('name','')}\n\n"
                        f"ç¼å·ï¼{product.get('code','')}\n"
                        f"åç±»ï¼{CATS.get(product.get('category',''), product.get('category',''))}\n"
                        f"ä»·æ ¼ï¼{product.get('price','')}\n"
                        f"åºå­ï¼{product.get('stock','')}\n"
                        f"ç¶æï¼{status}\n"
                        f"å¾çï¼{'æ' if product.get('photo_id') else 'æ '}\n"
                        f"æè¿°ï¼{product.get('description') or 'æ '}",
                        reply_markup=keyboard,
                    )
        return

    if data.startswith("admin:edittext:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_edit_start(update, context, int(pid))
        return

    if data.startswith("admin:photo:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_replace_photo_start(update, context, int(pid))
        return

    if data.startswith("admin:toggle:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_toggle_product(update, context, int(pid))
        return

    if data.startswith("admin:delete:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_delete_confirm(update, context, int(pid))
        return

    if data.startswith("admin:delok:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_delete_product(update, context, int(pid))
        return

    if data.startswith("admin:inq:"):
        pid = data.split(":")[-1]
        if pid.isdigit():
            await admin_inquiry_detail(update, context, int(pid))
        return

    if data.startswith("admin:inqstatus:"):
        parts = data.split(":")
        if len(parts) == 4 and parts[2].isdigit():
            await admin_mark_inquiry(update, context, int(parts[2]), parts[3])
        return

    if data == "admin:status":
        if is_admin(update.effective_user.id):
            await admin_status(update, context)
        return

    if data == "admin:zip_import":
        if is_admin(update.effective_user.id):
            await query.message.reply_text(
                "\U0001f4e6 \u4e00\u952e\u4e0a\u4f20\u5546\u54c1+\u56fe\u7247\n\n"
                "\u8bf7\u53d1\u9001\u4e00\u4e2a ZIP \u6587\u4ef6\uff0c\u91cc\u9762\u5fc5\u987b\u5305\u542b\uff1a\n"
                "1. 1 \u4e2a CSV\uff08\u5171 93 \u6761\u5546\u54c1\uff09\n"
                "2. 93 \u5f20\u56fe\u7247\uff1a01 \u5230 93\n\n"
                "\u56fe\u7247\u6620\u5c04\uff1a01\u2192P001\uff0c02\u2192P002\uff0c\u4e00\u76f4\u5230 93\u2192P093\u3002\n"
                "\u56fe\u7247\u652f\u6301 JPG / JPEG / PNG / WEBP\u3002\n\n"
                "\u53d1\u9001 ZIP \u540e\u7cfb\u7edf\u4f1a\u81ea\u52a8\u5bfc\u5165\u5546\u54c1\u5e76\u4e0a\u4f20\u56fe\u7247\u3002"
            )
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

    async def telegram_startup(app):
        """Clean any old webhook and verify Telegram connectivity before polling."""
        print("[TG] Startup check: connecting to Telegram...")
        try:
            # Polling and webhook are mutually exclusive. Remove any old webhook first.
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("[TG] Old webhook removed successfully.")

            me = await app.bot.get_me()
            print(f"[TG] Connected OK: @{me.username} (id={me.id})")

            info = await app.bot.get_webhook_info()
            webhook_url = getattr(info, "url", "") or ""
            pending = getattr(info, "pending_update_count", 0)
            if webhook_url:
                print(f"[TG] WARNING: webhook is still set: {webhook_url}")
            else:
                print(f"[TG] Polling ready. pending_updates={pending}")

        except Exception as exc:
            print("[TG] STARTUP ERROR:")
            print(f"[TG] {type(exc).__name__}: {exc}")
            traceback.print_exc()
            raise

    async def telegram_shutdown(app):
        print("[TG] Telegram application shutting down.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(telegram_startup)
        .post_shutdown(telegram_shutdown)
        .build()
    )

    # The handlers must be registered before polling starts.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    application.add_error_handler(error_handler)

    print("Telegram bot starting...")
    print(f"Web URL: {WEB_URL}")
    print(f"Port: {PORT}")
    print(f"Admins: {ADMINS}")
    print("[TG] Mode: polling")

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )
    except Exception as exc:
        print("[TG] POLLING STOPPED WITH ERROR:")
        print(f"[TG] {type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
