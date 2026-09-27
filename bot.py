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
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# 基础配置
# ============================================================

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
UPLOAD_DIR = BASE_DIR / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WEB_URL = os.getenv(
    "WEB_URL",
    "https://tg-catalog-bot-10.onrender.com",
).strip().rstrip("/")

try:
    PORT = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

CSV_FILE = BASE_DIR / os.getenv(
    "CSV_FILE",
    "Telegram商品导入表_93个_可直接导入.csv",
)


# ============================================================
# 商品分类
# ============================================================

CATS = {
    "c1": "和天下系列",
    "c2": "熊猫系列",
    "c3": "南京系列",
    "c4": "荷花系列",
    "c5": "芙蓉王系列",
    "c6": "牡丹系列",
    "c7": "黄金叶系列",
    "c8": "苏烟系列",
    "c9": "利群系列",
    "c10": "黄鹤楼系列",
    "c11": "中华系列",
    "c12": "白皮系列",
}


CAT_ALIASES = {}

for key, value in CATS.items():
    CAT_ALIASES[key.lower()] = key
    CAT_ALIASES[value] = key


# ============================================================
# 管理员
# ============================================================

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


# ============================================================
# 数据库
# ============================================================

def db_is_postgres():
    return DATABASE_URL.lower().startswith("postgres")


def db_connect():
    if db_is_postgres():
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True

        return conn, True, RealDictCursor

    conn = sqlite3.connect(
        BASE_DIR / "bot.db",
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn, False, sqlite3.Row


def placeholder():
    if db_is_postgres():
        return "%s"

    return "?"


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

        cur.execute(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_id TEXT"
        )

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

        columns = set()

        for row in cur.fetchall():
            columns.add(row[1])

        if "photo_id" not in columns:
            cur.execute(
                "ALTER TABLE products ADD COLUMN photo_id TEXT"
            )

    conn.commit()
    conn.close()


def fetch_all(sql, params=()):
    conn, is_pg, row_factory = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def fetch_one(sql, params=()):
    conn, is_pg, row_factory = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)

        row = cur.fetchone()

        if row is None:
            return None

        return dict(row)

    finally:
        conn.close()


def execute(sql, params=()):
    conn, is_pg, row_factory = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)

        last_id = None

        if not is_pg:
            last_id = cur.lastrowid

        conn.commit()

        return last_id

    finally:
        conn.close()


# ============================================================
# 商品数据库操作
# ============================================================

def insert_product(
    name,
    code,
    category,
    price,
    stock,
    description="",
    active=1,
):
    conn, is_pg, row_factory = db_connect()

    try:
        cur = conn.cursor()

        if is_pg:
            cur.execute(
                """
                INSERT INTO products
                (
                    name,
                    code,
                    category,
                    price,
                    stock,
                    description,
                    active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    name,
                    code,
                    category,
                    price,
                    stock,
                    description,
                    active,
                ),
            )

            row = cur.fetchone()

            if isinstance(row, dict):
                product_id = row["id"]
            else:
                product_id = row[0]

        else:
            cur.execute(
                """
                INSERT INTO products
                (
                    name,
                    code,
                    category,
                    price,
                    stock,
                    description,
                    active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    code,
                    category,
                    price,
                    stock,
                    description,
                    active,
                ),
            )

            product_id = cur.lastrowid

        conn.commit()

        return product_id

    finally:
        conn.close()


def product_count():
    row = fetch_one(
        "SELECT COUNT(*) AS c FROM products"
    )

    if not row:
        return 0

    return int(row["c"])


def get_product(product_id):
    return fetch_one(
        "SELECT * FROM products WHERE id = " + placeholder(),
        (product_id,),
    )


def update_product(
    product_id,
    name,
    code,
    category,
    price,
    stock,
    description,
):
    p = placeholder()

    sql = f"""
        UPDATE products
        SET
            name = {p},
            code = {p},
            category = {p},
            price = {p},
            stock = {p},
            description = {p}
        WHERE id = {p}
    """

    execute(
        sql,
        (
            name,
            code,
            category,
            price,
            stock,
            description,
            product_id,
        ),
    )


def set_product_stock(product_id, stock):
    """
    单独修改库存。
    """

    execute(
        "UPDATE products SET stock = "
        + placeholder()
        + " WHERE id = "
        + placeholder(),
        (
            stock,
            product_id,
        ),
    )


def set_product_active(product_id, active):
    execute(
        "UPDATE products SET active = "
        + placeholder()
        + " WHERE id = "
        + placeholder(),
        (
            1 if active else 0,
            product_id,
        ),
    )


def delete_product(product_id):
    execute(
        "DELETE FROM products WHERE id = "
        + placeholder(),
        (product_id,),
    )


def set_product_photo(product_id, photo_id):
    execute(
        "UPDATE products SET photo_id = "
        + placeholder()
        + " WHERE id = "
        + placeholder(),
        (
            photo_id,
            product_id,
        ),
    )


def get_products(
    category=None,
    keyword=None,
    active_only=True,
):
    sql = "SELECT * FROM products WHERE 1=1"
    params = []

    if active_only:
        sql += " AND active = 1"

    if category:
        sql += " AND category = " + placeholder()
        params.append(category)

    if keyword:
        p = placeholder()

        sql += (
            " AND (name LIKE "
            + p
            + " OR code LIKE "
            + p
            + ")"
        )

        params.extend(
            [
                "%" + keyword + "%",
                "%" + keyword + "%",
            ]
        )

    sql += " ORDER BY id DESC"

    return fetch_all(sql, params)


# ============================================================
# 数据清洗
# ============================================================

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


# ============================================================
# CSV 导入
# ============================================================

def read_csv_rows(csv_path):
    with open(
        csv_path,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:

        reader = csv.DictReader(f)

        return list(reader)


def row_to_product(row):
    name = str(
        row.get("name", row.get("名称", ""))
    ).strip()

    code = str(
        row.get("code", row.get("编号", ""))
    ).strip()

    category = normalize_category(
        row.get(
            "category",
            row.get("分类", ""),
        )
    )

    price = str(
        row.get("price", row.get("价格", ""))
    ).strip()

    stock = str(
        row.get("stock", row.get("库存", ""))
    ).strip()

    description = str(
        row.get(
            "description",
            row.get("描述", ""),
        )
    ).strip()

    return {
        "name": name,
        "code": code,
        "category": category,
        "price": price,
        "stock": stock,
        "description": description,
    }


def import_csv_file(csv_path):
    if not Path(csv_path).exists():
        return 0

    rows = read_csv_rows(csv_path)

    imported = 0

    for row in rows:
        data = row_to_product(row)

        if not data["name"]:
            continue

        if data["category"] not in CATS:
            continue

        insert_product(
            name=data["name"],
            code=data["code"],
            category=data["category"],
            price=data["price"],
            stock=data["stock"],
            description=data["description"],
            active=1,
        )

        imported += 1

    return imported


def import_csv_if_empty():
    if product_count() > 0:
        return 0

    if not CSV_FILE.exists():
        return 0

    return import_csv_file(CSV_FILE)


# ============================================================
# Telegram API
# ============================================================

def telegram_api(method, payload=None):
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    data = json.dumps(
        payload or {}
    ).encode("utf-8")

    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    with urlopen(req, timeout=30) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def get_telegram_file_url(file_id):
    result = telegram_api(
        "getFile",
        {
            "file_id": file_id
        },
    )

    if not result.get("ok"):
        return None

    file_path = (
        result
        .get("result", {})
        .get("file_path")
    )

    if not file_path:
        return None

    return (
        "https://api.telegram.org/file/"
        f"bot{BOT_TOKEN}/{file_path}"
    )


# ============================================================
# Web 服务
# ============================================================

class WebHandler(BaseHTTPRequestHandler):

    def _send(
        self,
        status=200,
        content_type="text/plain; charset=utf-8",
        data=b"",
    ):
        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Cache-Control",
            "no-cache",
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.end_headers()

        self.wfile.write(data)

    def _json(self, obj, status=200):
        data = json.dumps(
            obj,
            ensure_ascii=False,
        ).encode("utf-8")

        self._send(
            status,
            "application/json; charset=utf-8",
            data,
        )

    def do_OPTIONS(self):
        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )

        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        path = parsed.path
        query = parse_qs(parsed.query)

        try:

            # 首页
            if path == "/":

                index_file = WEB_DIR / "index.html"

                if not index_file.exists():
                    self._send(
                        200,
                        "text/html; charset=utf-8",
                        (
                            "<h1>Telegram 商品目录</h1>"
                        ).encode("utf-8"),
                    )

                    return

                self._send(
                    200,
                    "text/html; charset=utf-8",
                    index_file.read_bytes(),
                )

                return

            # 状态
            if path == "/api/status":

                total = product_count()

                active = fetch_one(
                    """
                    SELECT COUNT(*) AS c
                    FROM products
                    WHERE active = 1
                    """
                )

                photos = fetch_one(
                    """
                    SELECT COUNT(*) AS c
                    FROM products
                    WHERE photo_id IS NOT NULL
                    AND photo_id != ''
                    """
                )

                self._json(
                    {
                        "ok": True,
                        "db": (
                            "postgresql"
                            if db_is_postgres()
                            else "sqlite"
                        ),
                        "total_products": total,
                        "active_products": (
                            int(active["c"])
                            if active
                            else 0
                        ),
                        "products_with_photos": (
                            int(photos["c"])
                            if photos
                            else 0
                        ),
                        "csv_file": CSV_FILE.name,
                        "csv_exists": CSV_FILE.exists(),
                    }
                )

                return

            # 分类
            if path == "/api/categories":

                categories = []

                for key, name in CATS.items():

                    row = fetch_one(
                        """
                        SELECT COUNT(*) AS c
                        FROM products
                        WHERE category =
                        """
                        + placeholder()
                        + """
                        AND active = 1
                        """,
                        (key,),
                    )

                    categories.append(
                        {
                            "id": key,
                            "name": name,
                            "count": (
                                int(row["c"])
                                if row
                                else 0
                            ),
                        }
                    )

                self._json(
                    {
                        "ok": True,
                        "categories": categories,
                    }
                )

                return

            # 商品列表
            if path == "/api/products":

                category = query.get(
                    "category",
                    [None],
                )[0]

                keyword = query.get(
                    "keyword",
                    [None],
                )[0]

                active = (
                    query.get(
                        "active",
                        ["1"],
                    )[0]
                    != "0"
                )

                if category:
                    category = normalize_category(
                        category
                    )

                products = get_products(
                    category=category,
                    keyword=keyword,
                    active_only=active,
                )

                output = []

                for product in products:

                    item = dict(product)

                    if item.get("photo_id"):

                        item["photo_url"] = (
                            WEB_URL
                            + "/api/photo?id="
                            + str(item["id"])
                        )

                    else:
                        item["photo_url"] = ""

                    output.append(item)

                self._json(
                    {
                        "ok": True,
                        "products": output,
                    }
                )

                return

            # 商品图片
            if path == "/api/photo":

                raw_id = query.get(
                    "id",
                    [None],
                )[0]

                if (
                    not raw_id
                    or not str(raw_id).isdigit()
                ):
                    self._send(
                        400,
                        "text/plain; charset=utf-8",
                        b"bad id",
                    )

                    return

                product = get_product(
                    int(raw_id)
                )

                if (
                    not product
                    or not product.get("photo_id")
                ):
                    self._send(
                        404,
                        "text/plain; charset=utf-8",
                        b"not found",
                    )

                    return

                photo_url = get_telegram_file_url(
                    product["photo_id"]
                )

                if not photo_url:
                    self._send(
                        404,
                        "text/plain; charset=utf-8",
                        b"photo unavailable",
                    )

                    return

                req = Request(
                    photo_url,
                    method="GET",
                )

                with urlopen(
                    req,
                    timeout=30,
                ) as response:

                    content = response.read()

                    content_type = response.headers.get(
                        "Content-Type",
                        "image/jpeg",
                    )

                self._send(
                    200,
                    content_type,
                    content,
                )

                return

            # favicon
            if path == "/favicon.ico":
                self._send(
                    204,
                    "image/x-icon",
                    b"",
                )

                return

            # web 静态文件
            if path.startswith("/web/"):

                filename = (
                    path[5:]
                    .lstrip("/")
                    .replace("..", "")
                )

                target = WEB_DIR / filename

                if (
                    target.exists()
                    and target.is_file()
                ):

                    content_type = (
                        mimetypes.guess_type(
                            str(target)
                        )[0]
                        or "application/octet-stream"
                    )

                    self._send(
                        200,
                        content_type,
                        target.read_bytes(),
                    )

                    return

            self._send(
                404,
                "text/plain; charset=utf-8",
                b"Not Found",
            )

        except Exception as exc:

            traceback.print_exc()

            self._json(
                {
                    "ok": False,
                    "error": str(exc),
                },
                500,
            )

    def do_POST(self):

        parsed = urlparse(self.path)

        if parsed.path != "/api/inquiry":

            self._send(
                404,
                "text/plain; charset=utf-8",
                b"Not Found",
            )

            return

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            raw = self.rfile.read(length)

            data = json.loads(
                raw.decode("utf-8")
            )

            product = str(
                data.get("product", "")
            ).strip()

            message = str(
                data.get("message", "")
            ).strip()

            user_id = data.get("user_id")

            username = str(
                data.get("username", "")
            ).strip()

            if not product:

                self._json(
                    {
                        "ok": False,
                        "error": "请选择商品",
                    },
                    400,
                )

                return

            p = placeholder()

            sql = f"""
                INSERT INTO inquiries
                (
                    user_id,
                    username,
                    product,
                    message,
                    status
                )
                VALUES
                (
                    {p},
                    {p},
                    {p},
                    {p},
                    {p}
                )
            """

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
                args=(
                    product,
                    message,
                    username,
                    user_id,
                ),
                daemon=True,
            ).start()

            self._json(
                {
                    "ok": True
                }
            )

        except Exception as exc:

            traceback.print_exc()

            self._json(
                {
                    "ok": False,
                    "error": str(exc),
                },
                500,
            )

    def log_message(self, format, *args):
        return


def start_web_server():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        WebHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    print(
        f"Web server started on 0.0.0.0:{PORT}"
    )

    return server


# ============================================================
# Telegram 菜单
# ============================================================

def main_menu():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛍️ 商品目录",
                    callback_data="catalog",
                ),
                InlineKeyboardButton(
                    "📋 商品",
                    callback_data="products",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💬 询价",
                    callback_data="ask",
                ),
                InlineKeyboardButton(
                    "➕ 添加商品",
                    callback_data="add",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🌐 打开商品小程序",
                    web_app=WebAppInfo(
                        url=WEB_URL
                    ),
                ),
            ],
        ]
    )


# ============================================================
# 商品展示
# ============================================================

async def send_product_message(
    update,
    context,
    product,
):

    name = product.get("name", "")
    code = product.get("code", "")
    category = product.get("category", "")
    price = product.get("price", "")
    stock = product.get("stock", "")
    description = product.get(
        "description",
        "",
    )

    text = (
        f"📦 {name}\n"
        f"编号：{code}\n"
        f"分类：{CATS.get(category, category)}\n"
        f"价格：{price}\n"
        f"库存：{stock}\n"
    )

    if description:
        text += (
            f"说明：{description}\n"
        )

    keyboard = [
        [
            InlineKeyboardButton(
                "💬 询价",
                callback_data=(
                    f"inquiry:{product['id']}"
                ),
            )
        ]
    ]

    photo_id = product.get("photo_id")

    if photo_id:

        try:

            await update.effective_message.reply_photo(
                photo=photo_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(
                    keyboard
                ),
            )

            return

        except Exception:
            pass

    await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def show_catalog(
    update,
    context,
):

    keyboard = []

    row = []

    for index, (key, name) in enumerate(
        CATS.items(),
        start=1,
    ):

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
        [
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="home",
            )
        ]
    )

    await update.effective_message.reply_text(
        "请选择商品系列：",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def show_category(
    update,
    context,
    category,
):

    products = get_products(
        category=category,
        active_only=True,
    )

    if not products:

        await update.effective_message.reply_text(
            "这个系列暂时没有上架商品。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ 返回目录",
                            callback_data="catalog",
                        )
                    ]
                ]
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
                    callback_data=(
                        f"product:{product['id']}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回目录",
                callback_data="catalog",
            )
        ]
    )

    await update.effective_message.reply_text(
        (
            f"📂 {CATS.get(category, category)}\n\n"
            "请选择商品："
        ),
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def show_product(
    update,
    context,
    product_id,
):

    product = get_product(product_id)

    if (
        not product
        or int(product.get("active", 0)) != 1
    ):

        await update.effective_message.reply_text(
            "商品不存在或已下架。"
        )

        return

    name = product.get("name", "")
    code = product.get("code", "")
    category = product.get("category", "")
    price = product.get("price", "")
    stock = product.get("stock", "")
    description = product.get(
        "description",
        "",
    )

    text = (
        f"📦 {name}\n"
        f"编号：{code}\n"
        f"分类：{CATS.get(category, category)}\n"
        f"价格：{price}\n"
        f"库存：{stock}\n"
    )

    if description:
        text += (
            f"说明：{description}\n"
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💬 立即询价",
                    callback_data=(
                        f"inquiry:{product_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回系列",
                    callback_data=(
                        f"cat:{category}"
                    ),
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


async def show_all_products(
    update,
    context,
):

    products = get_products(
        active_only=True
    )

    if not products:

        await update.effective_message.reply_text(
            "暂无商品。"
        )

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
                    callback_data=(
                        f"product:{product['id']}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="home",
            )
        ]
    )

    await update.effective_message.reply_text(
        "📋 商品列表：",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# /start /cancel
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "欢迎使用商品目录机器人\n\n"
        "请选择功能：",
        reply_markup=main_menu(),
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "已取消当前操作。",
        reply_markup=main_menu(),
    )


# ============================================================
# 询价
# ============================================================

async def start_inquiry(
    update,
    context,
    product_id=None,
):

    context.user_data["inquiry_product_id"] = (
        product_id
    )

    context.user_data.pop(
        "inquiry_product_name",
        None,
    )

    if product_id:

        product = get_product(
            product_id
        )

        if product:
            context.user_data[
                "inquiry_product_name"
            ] = product["name"]

    await update.effective_message.reply_text(
        "请直接发送你要询价的内容。\n\n"
        "例如：\n"
        "我要询价 10 件\n"
        "或者发送：数量 + 商品名称\n\n"
        "发送 /cancel 可以取消。"
    )


async def notify_admins(
    context,
    product,
    message,
    username,
    user_id,
):

    if not ADMINS:
        return

    display_username = (
        f"@{username}"
        if username
        else "无用户名"
    )

    text = (
        "🔔 新询价\n\n"
        f"商品：{product}\n"
        f"用户：{display_username}\n"
        f"用户ID：{user_id}\n"
        f"内容：{message}"
    )

    for admin_id in ADMINS:

        try:

            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
            )

        except Exception:
            traceback.print_exc()


def notify_admins_sync(
    product,
    message,
    username,
    user_id=None,
):

    if not BOT_TOKEN:
        return

    if not ADMINS:
        return

    display_username = (
        f"@{username}"
        if username
        else "无用户名"
    )

    text = (
        "🔔 新询价\n\n"
        f"商品：{product}\n"
        f"用户：{display_username}\n"
    )

    if user_id:
        text += f"用户ID：{user_id}\n"

    text += f"内容：{message}"

    for admin_id in ADMINS:

        try:

            telegram_api(
                "sendMessage",
                {
                    "chat_id": admin_id,
                    "text": text,
                },
            )

        except Exception:
            traceback.print_exc()


# ============================================================
# 添加商品
# ============================================================

async def add_product_start(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.effective_message.reply_text(
            "你没有管理员权限。"
        )

        return

    context.user_data.clear()

    context.user_data["add_step"] = "text"

    await update.effective_message.reply_text(
        "请输入商品信息，格式：\n\n"
        "名称|编号|分类|价格|库存|描述\n\n"
        "例如：\n"
        "示例商品|P001|c1|100|20|商品说明\n\n"
        "分类可以填写 c1-c12，也可以填写系列名称。\n"
        "发送 /cancel 取消。"
    )


def parse_product_text(text):

    parts = [
        x.strip()
        for x in text.split("|")
    ]

    if len(parts) < 5:

        return (
            None,
            "格式不正确，至少需要 5 项。\n"
            "格式：名称|编号|分类|价格|库存|描述",
        )

    name = parts[0]
    code = parts[1]
    category = normalize_category(
        parts[2]
    )
    price = parts[3]
    stock = parts[4]

    description = (
        "|".join(parts[5:]).strip()
        if len(parts) > 5
        else ""
    )

    if not name:

        return (
            None,
            "商品名称不能为空。",
        )

    if category not in CATS:

        return (
            None,
            "分类错误，请使用 c1-c12 "
            "或正确的系列名称。",
        )

    return (
        {
            "name": name,
            "code": code,
            "category": category,
            "price": price,
            "stock": stock,
            "description": description,
        },
        None,
    )


# ============================================================
# 修改商品资料
# ============================================================

async def admin_edit_start(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(product_id)

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    context.user_data.clear()

    context.user_data[
        "admin_edit_product_id"
    ] = product_id

    context.user_data[
        "admin_edit_step"
    ] = "text"

    await update.effective_message.reply_text(
        "请输入新的商品信息，格式：\n\n"
        "名称|编号|分类|价格|库存|描述\n\n"
        f"当前："
        f"{product.get('name', '')}|"
        f"{product.get('code', '')}|"
        f"{product.get('category', '')}|"
        f"{product.get('price', '')}|"
        f"{product.get('stock', '')}|"
        f"{product.get('description') or ''}\n\n"
        "分类使用 c1-c12。\n"
        "发送 /cancel 取消。"
    )


# ============================================================
# 修改库存
# ============================================================

async def admin_stock_start(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(product_id)

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    context.user_data.clear()

    context.user_data[
        "admin_stock_product_id"
    ] = product_id

    context.user_data[
        "admin_edit_step"
    ] = "stock"

    await update.effective_message.reply_text(
        f"📦 修改库存\n\n"
        f"商品：{product.get('name', '')}\n"
        f"编号：{product.get('code', '')}\n"
        f"当前库存：{product.get('stock', '')}\n\n"
        "请输入新的库存数量。\n"
        "只填写数字，例如：50\n\n"
        "库存可以填写 0。\n"
        "发送 /cancel 取消。"
    )


# ============================================================
# 修改图片
# ============================================================

async def admin_replace_photo_start(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(product_id)

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    context.user_data.clear()

    context.user_data[
        "admin_photo_product_id"
    ] = product_id

    context.user_data[
        "admin_edit_step"
    ] = "photo"

    await update.effective_message.reply_text(
        f"请发送商品【{product.get('name', '')}】的新图片。\n\n"
        "发送 /cancel 取消。"
    )


# ============================================================
# 文本消息
# ============================================================

async def handle_text(
    update,
    context,
):

    text = (
        update.message.text or ""
    ).strip()

    if text == "/cancel":

        await cancel(
            update,
            context,
        )

        return

    user_id = update.effective_user.id

    # --------------------------------------------------------
    # 修改库存
    # --------------------------------------------------------

    if (
        context.user_data.get(
            "admin_edit_step"
        )
        == "stock"
    ):

        if not is_admin(user_id):

            context.user_data.clear()

            await update.message.reply_text(
                "你没有管理员权限。"
            )

            return

        product_id = context.user_data.get(
            "admin_stock_product_id"
        )

        if not product_id:

            context.user_data.clear()

            await update.message.reply_text(
                "没有找到要修改的商品。"
            )

            return

        product = get_product(
            product_id
        )

        if not product:

            context.user_data.clear()

            await update.message.reply_text(
                "商品不存在，修改已取消。"
            )

            return

        # 只允许数字
        if not text.isdigit():

            await update.message.reply_text(
                "库存必须是数字。\n\n"
                "例如：50\n"
                "库存为 0 也可以。\n\n"
                "请重新输入，或发送 /cancel 取消。"
            )

            return

        new_stock = str(
            int(text)
        )

        set_product_stock(
            product_id,
            new_stock,
        )

        product_name = product.get(
            "name",
            "",
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ 库存修改成功\n\n"
            f"商品：{product_name}\n"
            f"新库存：{new_stock}",
            reply_markup=main_menu(),
        )

        return

    # --------------------------------------------------------
    # 添加商品：填写资料
    # --------------------------------------------------------

    add_step = context.user_data.get(
        "add_step"
    )

    if add_step == "text":

        if not is_admin(user_id):

            context.user_data.clear()

            await update.message.reply_text(
                "你没有管理员权限。"
            )

            return

        data, error = parse_product_text(
            text
        )

        if error:

            await update.message.reply_text(
                error
            )

            return

        product_id = insert_product(
            **data
        )

        context.user_data[
            "pending_product_id"
        ] = product_id

        context.user_data[
            "add_step"
        ] = "photo"

        await update.message.reply_text(
            f"商品已添加，ID：{product_id}\n\n"
            "请发送商品图片。\n"
            "如果不需要图片，请发送：跳过"
        )

        return

    # --------------------------------------------------------
    # 添加商品：等待图片
    # --------------------------------------------------------

    if add_step == "photo":

        if not is_admin(user_id):

            context.user_data.clear()

            await update.message.reply_text(
                "你没有管理员权限。"
            )

            return

        if text == "跳过":

            context.user_data.clear()

            await update.message.reply_text(
                "已跳过图片，商品添加完成。",
                reply_markup=main_menu(),
            )

            return

        await update.message.reply_text(
            "请发送商品图片，或者发送：跳过"
        )

        return

    # --------------------------------------------------------
    # 编辑商品资料
    # --------------------------------------------------------

    if (
        context.user_data.get(
            "admin_edit_step"
        )
        == "text"
    ):

        if not is_admin(user_id):

            context.user_data.clear()

            await update.message.reply_text(
                "你没有管理员权限。"
            )

            return

        product_id = context.user_data.get(
            "admin_edit_product_id"
        )

        if not product_id:

            context.user_data.clear()

            await update.message.reply_text(
                "没有找到要编辑的商品。"
            )

            return

        data, error = parse_product_text(
            text
        )

        if error:

            await update.message.reply_text(
                error
            )

            return

        if not get_product(product_id):

            context.user_data.clear()

            await update.message.reply_text(
                "商品不存在，编辑已取消。"
            )

            return

        update_product(
            product_id,
            **data,
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ 商品信息已更新。",
            reply_markup=main_menu(),
        )

        return

    # --------------------------------------------------------
    # 编辑商品图片时发文字
    # --------------------------------------------------------

    if (
        context.user_data.get(
            "admin_edit_step"
        )
        == "photo"
    ):

        await update.message.reply_text(
            "请发送图片，不要发送文字。\n"
            "发送 /cancel 可以取消。"
        )

        return

    # --------------------------------------------------------
    # 用户询价
    # --------------------------------------------------------

    inquiry_product_id = (
        context.user_data.get(
            "inquiry_product_id"
        )
    )

    inquiry_product_name = (
        context.user_data.get(
            "inquiry_product_name"
        )
    )

    if (
        inquiry_product_id is not None
        or inquiry_product_name
    ):

        product_name = (
            inquiry_product_name
            or ""
        )

        if (
            not product_name
            and inquiry_product_id
        ):

            product = get_product(
                inquiry_product_id
            )

            if product:
                product_name = product[
                    "name"
                ]

        username = (
            update.effective_user.username
            or ""
        )

        user_id = update.effective_user.id

        p = placeholder()

        execute(
            f"""
            INSERT INTO inquiries
            (
                user_id,
                username,
                product,
                message,
                status
            )
            VALUES
            (
                {p},
                {p},
                {p},
                {p},
                {p}
            )
            """,
            (
                user_id,
                username,
                product_name,
                text,
                "new",
            ),
        )

        context.user_data.pop(
            "inquiry_product_id",
            None,
        )

        context.user_data.pop(
            "inquiry_product_name",
            None,
        )

        await update.message.reply_text(
            "✅ 已收到你的询价信息。\n"
            "我们会尽快联系你。",
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

    # --------------------------------------------------------
    # 默认
    # --------------------------------------------------------

    await update.message.reply_text(
        "请选择功能：",
        reply_markup=main_menu(),
    )


# ============================================================
# 图片消息
# ============================================================

async def handle_photo(
    update,
    context,
):

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "你没有管理员权限。"
        )

        return

    # --------------------------------------------------------
    # 修改商品图片
    # --------------------------------------------------------

    if (
        context.user_data.get(
            "admin_edit_step"
        )
        == "photo"
    ):

        product_id = context.user_data.get(
            "admin_photo_product_id"
        )

        if (
            not product_id
            or not get_product(product_id)
        ):

            context.user_data.clear()

            await update.message.reply_text(
                "商品不存在，操作已取消。"
            )

            return

        telegram_photo = (
            update.message.photo[-1]
        )

        set_product_photo(
            product_id,
            telegram_photo.file_id,
        )

        try:
            await update.message.delete()

        except Exception as exc:
            print(
                "[PHOTO] 删除旧图片消息失败:",
                exc,
            )

        context.user_data.clear()

        await update.effective_chat.send_message(
            "✅ 商品图片已更换。",
            reply_markup=main_menu(),
        )

        return

    # --------------------------------------------------------
    # 添加商品图片
    # --------------------------------------------------------

    add_step = context.user_data.get(
        "add_step"
    )

    if add_step != "photo":

        await update.message.reply_text(
            "如果你要给商品添加图片，请先使用："
            "➕ 添加商品"
        )

        return

    product_id = context.user_data.get(
        "pending_product_id"
    )

    if not product_id:

        context.user_data.clear()

        await update.message.reply_text(
            "没有找到待处理商品。"
        )

        return

    photos = update.message.photo

    if not photos:

        await update.message.reply_text(
            "没有检测到图片，请重新发送。"
        )

        return

    telegram_photo = photos[-1]

    set_product_photo(
        product_id,
        telegram_photo.file_id,
    )

    # 保存成功后删除管理员刚发送的图片
    try:

        await update.message.delete()

    except Exception as exc:

        print(
            "[PHOTO] 删除上传图片消息失败:",
            exc,
        )

    context.user_data.clear()

    await update.effective_chat.send_message(
        "✅ 商品图片已保存，商品添加完成。",
        reply_markup=main_menu(),
    )


# ============================================================
# 管理后台
# ============================================================

async def admin_command(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "你没有管理员权限。"
        )

        return

    total = product_count()

    active = fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM products
        WHERE active = 1
        """
    )

    inquiries = fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM inquiries
        WHERE status = 'new'
        """
    )

    text = (
        "🛠 管理后台\n\n"
        f"商品总数：{total}\n"
        f"上架商品："
        f"{int(active['c']) if active else 0}\n"
        f"待处理询价："
        f"{int(inquiries['c']) if inquiries else 0}\n\n"
        "请选择操作："
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "📊 商品统计",
                callback_data="admin:status",
            ),
            InlineKeyboardButton(
                "📥 只导入CSV",
                callback_data="admin:csv",
            ),
        ],
        [
            InlineKeyboardButton(
                "📦 一键上传商品+图片",
                callback_data="admin:zip_import",
            ),
        ],
        [
            InlineKeyboardButton(
                "📦 商品列表",
                callback_data="admin:list",
            ),
            InlineKeyboardButton(
                "💬 询价记录",
                callback_data="admin:inquiries",
            ),
        ],
        [
            InlineKeyboardButton(
                "🏠 返回",
                callback_data="home",
            ),
        ],
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def admin_status(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    total = product_count()

    active = fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM products
        WHERE active = 1
        """
    )

    photos = fetch_one(
        """
        SELECT COUNT(*) AS c
        FROM products
        WHERE photo_id IS NOT NULL
        AND photo_id != ''
        """
    )

    await update.effective_message.reply_text(
        "📊 商品统计\n\n"
        f"商品总数：{total}\n"
        f"上架商品："
        f"{int(active['c']) if active else 0}\n"
        f"已有图片："
        f"{int(photos['c']) if photos else 0}\n"
        f"数据库："
        f"{'PostgreSQL' if db_is_postgres() else 'SQLite'}"
    )


# ============================================================
# 管理商品列表
# ============================================================

async def admin_list(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    products = get_products(
        active_only=False
    )

    if not products:

        await update.effective_message.reply_text(
            "暂无商品。"
        )

        return

    keyboard = []

    for product in products[:100]:

        pid = int(
            product["id"]
        )

        name = str(
            product.get("name") or ""
        )

        status = (
            "上架"
            if int(
                product.get(
                    "active",
                    0,
                )
            ) == 1
            else "下架"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    f"#{pid} {name[:28]} [{status}]",
                    callback_data=(
                        f"admin:edit:{pid}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回后台",
                callback_data="admin:home",
            )
        ]
    )

    await update.effective_message.reply_text(
        (
            f"📦 商品管理（共 {len(products)} 个）\n"
            "点击商品进入管理："
        ),
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


# ============================================================
# 商品编辑菜单
# ============================================================

async def show_admin_product(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(product_id)

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    pid = int(product["id"])

    active = int(
        product.get(
            "active",
            0,
        )
    ) == 1

    status = (
        "上架"
        if active
        else "下架"
    )

    photo_status = (
        "有"
        if product.get("photo_id")
        else "无"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ 编辑资料",
                    callback_data=(
                        f"admin:edittext:{pid}"
                    ),
                ),
                InlineKeyboardButton(
                    "📦 修改库存",
                    callback_data=(
                        f"admin:stock:{pid}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼️ 更换图片",
                    callback_data=(
                        f"admin:photo:{pid}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬆️/⬇️ 上下架",
                    callback_data=(
                        f"admin:toggle:{pid}"
                    ),
                ),
                InlineKeyboardButton(
                    "🗑 删除",
                    callback_data=(
                        f"admin:delete:{pid}"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回商品列表",
                    callback_data="admin:list",
                )
            ],
        ]
    )

    text = (
        f"📦 #{pid} {product.get('name', '')}\n\n"
        f"编号：{product.get('code', '')}\n"
        f"分类："
        f"{CATS.get(product.get('category', ''), product.get('category', ''))}\n"
        f"价格：{product.get('price', '')}\n"
        f"库存：{product.get('stock', '')}\n"
        f"状态：{status}\n"
        f"图片：{photo_status}\n"
        f"描述："
        f"{product.get('description') or '无'}"
    )

    await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
    )


# ============================================================
# 上架 / 下架
# ============================================================

async def admin_toggle_product(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(
        product_id
    )

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    current_active = int(
        product.get(
            "active",
            0,
        )
    )

    new_active = (
        0
        if current_active == 1
        else 1
    )

    set_product_active(
        product_id,
        new_active,
    )

    status_text = (
        "上架"
        if new_active
        else "下架"
    )

    await update.effective_message.reply_text(
        f"✅ {product.get('name', '')} 已{status_text}。"
    )

    await admin_list(
        update,
        context,
    )


# ============================================================
# 删除商品
# ============================================================

async def admin_delete_confirm(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(
        product_id
    )

    if not product:

        await update.effective_message.reply_text(
            "商品不存在。"
        )

        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚠️ 确认删除",
                    callback_data=(
                        f"admin:delok:{product_id}"
                    ),
                ),
                InlineKeyboardButton(
                    "取消",
                    callback_data="admin:list",
                ),
            ]
        ]
    )

    await update.effective_message.reply_text(
        "确定删除商品？\n\n"
        f"#{product_id} "
        f"{product.get('name', '')} "
        f"[{product.get('code', '')}]\n\n"
        "删除后数据库中的商品记录会被删除。",
        reply_markup=keyboard,
    )


async def admin_delete_product(
    update,
    context,
    product_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    product = get_product(
        product_id
    )

    if not product:

        await update.effective_message.reply_text(
            "商品已经不存在。"
        )

        return

    delete_product(
        product_id
    )

    await update.effective_message.reply_text(
        "✅ 已删除："
        f"{product.get('name', '')} "
        f"[{product.get('code', '')}]"
    )

    await admin_list(
        update,
        context,
    )


# ============================================================
# 询价管理
# ============================================================

async def admin_inquiries(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    rows = fetch_all(
        """
        SELECT *
        FROM inquiries
        ORDER BY id DESC
        LIMIT 50
        """
    )

    if not rows:

        await update.effective_message.reply_text(
            "暂无询价记录。"
        )

        return

    keyboard = []

    for row in rows:

        iid = int(
            row["id"]
        )

        status = (
            row.get("status")
            or "new"
        )

        status_text = (
            "待处理"
            if status == "new"
            else "已处理"
        )

        product = str(
            row.get("product")
            or "未指定"
        )

        title = (
            f"#{iid} "
            f"{product[:20]} "
            f"| {status_text}"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    title,
                    callback_data=(
                        f"admin:inq:{iid}"
                    ),
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回后台",
                callback_data="admin:home",
            )
        ]
    )

    await update.effective_message.reply_text(
        "💬 询价管理（最近50条）\n"
        "点击记录查看详情：",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
    )


async def admin_inquiry_detail(
    update,
    context,
    inquiry_id,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    row = fetch_one(
        "SELECT * FROM inquiries WHERE id = "
        + placeholder(),
        (inquiry_id,),
    )

    if not row:

        await update.effective_message.reply_text(
            "询价记录不存在。"
        )

        return

    username = (
        row.get("username")
        or "无用户名"
    )

    status = (
        row.get("status")
        or "new"
    )

    status_text = (
        "待处理"
        if status == "new"
        else "已处理"
    )

    text = (
        f"💬 询价 #{inquiry_id}\n\n"
        f"商品："
        f"{row.get('product') or '未指定'}\n"
        f"用户：@{username}\n"
        f"用户ID："
        f"{row.get('user_id') or ''}\n"
        f"状态：{status_text}\n"
        f"时间："
        f"{row.get('created_at') or ''}\n\n"
        f"内容："
        f"{row.get('message') or ''}"
    )

    next_status = (
        "done"
        if status == "new"
        else "new"
    )

    next_text = (
        "✅ 标记已处理"
        if status == "new"
        else "↩️ 恢复待处理"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    next_text,
                    callback_data=(
                        f"admin:inqstatus:"
                        f"{inquiry_id}:"
                        f"{next_status}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回询价",
                    callback_data="admin:inquiries",
                )
            ],
        ]
    )

    await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
    )


async def admin_mark_inquiry(
    update,
    context,
    inquiry_id,
    status,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    execute(
        "UPDATE inquiries SET status = "
        + placeholder()
        + " WHERE id = "
        + placeholder(),
        (
            status,
            inquiry_id,
        ),
    )

    await admin_inquiries(
        update,
        context,
    )


# ============================================================
# ZIP 安全解压
# ============================================================

def safe_extract_zip(
    zip_path,
    target_dir,
):

    target_dir = Path(
        target_dir
    ).resolve()

    with zipfile.ZipFile(
        zip_path,
        "r",
    ) as zf:

        for member in zf.infolist():

            member_path = (
                target_dir
                / member.filename
            ).resolve()

            try:

                common = os.path.commonpath(
                    [
                        str(target_dir),
                        str(member_path),
                    ]
                )

            except ValueError:

                raise ValueError(
                    "ZIP 包含非法路径"
                )

            if common != str(
                target_dir
            ):

                raise ValueError(
                    "ZIP 包含非法路径"
                )

            if member.is_dir():

                member_path.mkdir(
                    parents=True,
                    exist_ok=True,
                )

            else:

                member_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                with zf.open(
                    member
                ) as source:

                    with open(
                        member_path,
                        "wb",
                    ) as target:

                        shutil.copyfileobj(
                            source,
                            target,
                        )


# ============================================================
# 93 商品 + 93 图片 ZIP 导入
# ============================================================

async def process_zip_import(
    update,
    context,
    document,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "你没有管理员权限。"
        )

        return

    filename = (
        document.file_name
        or ""
    )

    if not filename.lower().endswith(
        ".zip"
    ):

        await update.message.reply_text(
            "请上传 ZIP 文件。"
        )

        return

    status_message = (
        await update.message.reply_text(
            "📦 正在下载 ZIP，请稍候..."
        )
    )

    temp_root = Path(
        tempfile.mkdtemp(
            prefix="catalog_full_zip_"
        )
    )

    zip_path = (
        temp_root / "upload.zip"
    )

    extract_dir = (
        temp_root / "extract"
    )

    extract_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        # ----------------------------------------------------
        # 下载
        # ----------------------------------------------------

        telegram_file = (
            await context.bot.get_file(
                document.file_id
            )
        )

        await telegram_file.download_to_drive(
            custom_path=str(zip_path)
        )

        await status_message.edit_text(
            "✅ ZIP 已下载，正在检查 CSV 和 93 张图片..."
        )

        # ----------------------------------------------------
        # 解压
        # ----------------------------------------------------

        safe_extract_zip(
            zip_path,
            extract_dir,
        )

        # ----------------------------------------------------
        # 找 CSV
        # ----------------------------------------------------

        csv_files = [
            path
            for path in extract_dir.rglob("*")
            if (
                path.is_file()
                and path.suffix.lower()
                == ".csv"
            )
        ]

        if len(csv_files) != 1:

            await status_message.edit_text(
                "❌ ZIP 中必须有且只能有 1 个 CSV。\n"
                f"当前找到：{len(csv_files)} 个。\n"
                "本次未导入。"
            )

            return

        csv_path = csv_files[0]

        # ----------------------------------------------------
        # 读取 CSV
        # ----------------------------------------------------

        rows = read_csv_rows(
            csv_path
        )

        if len(rows) != 93:

            await status_message.edit_text(
                "❌ CSV 必须是 93 条商品。\n"
                f"当前是：{len(rows)} 条。\n"
                "本次未导入。"
            )

            return

        parsed = []

        seen_codes = set()

        for index, row in enumerate(
            rows,
            start=1,
        ):

            data = row_to_product(
                row
            )

            data["code"] = (
                data["code"]
                .strip()
                .upper()
            )

            if (
                not data["name"]
                or not data["code"]
                or data["category"]
                not in CATS
            ):

                await status_message.edit_text(
                    f"❌ CSV 第 {index} 行数据不正确。\n"
                    "需要：name, code, category, price, stock"
                )

                return

            if data["code"] in seen_codes:

                await status_message.edit_text(
                    "❌ CSV 中存在重复编号："
                    f"{data['code']}"
                )

                return

            seen_codes.add(
                data["code"]
            )

            parsed.append(
                data
            )

        # ----------------------------------------------------
        # 检查 P001-P093
        # ----------------------------------------------------

        by_code = {
            row["code"]: row
            for row in parsed
        }

        missing_codes = []

        for i in range(1, 94):

            code = f"P{i:03d}"

            if code not in by_code:
                missing_codes.append(
                    code
                )

        if missing_codes:

            await status_message.edit_text(
                "❌ CSV 编号必须包含 P001 到 P093。\n"
                "缺少："
                + ", ".join(
                    missing_codes[:30]
                )
            )

            return

        # ----------------------------------------------------
        # 检查 01-93 图片
        # ----------------------------------------------------

        allowed_extensions = {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }

        image_by_number = {}

        for path in extract_dir.rglob("*"):

            if not path.is_file():
                continue

            if (
                path.suffix.lower()
                not in allowed_extensions
            ):
                continue

            stem = path.stem.strip()

            if not re.fullmatch(
                r"\d{1,3}",
                stem,
            ):
                continue

            number = int(stem)

            if 1 <= number <= 93:

                image_by_number[
                    number
                ] = path

        missing_images = []

        for i in range(1, 94):

            if i not in image_by_number:

                missing_images.append(
                    f"{i:02d}"
                )

        if missing_images:

            extra = ""

            if len(missing_images) > 30:
                extra = " 等"

            await status_message.edit_text(
                "❌ ZIP 图片不完整。\n"
                "缺少："
                + ", ".join(
                    missing_images[:30]
                )
                + extra
                + "\n\n"
                "必须有 01 到 93 共 93 张图片。\n"
                "本次未导入。"
            )

            return

        # ----------------------------------------------------
        # 防止重复导入
        # ----------------------------------------------------

        existing = product_count()

        if existing > 0:

            await status_message.edit_text(
                f"⚠️ 数据库当前已有 {existing} 个商品。\n\n"
                "为防止重复导入，本次没有导入。\n\n"
                "如需重新导入，请先处理现有商品数据。"
            )

            return

        # ----------------------------------------------------
        # 开始导入
        # ----------------------------------------------------

        await status_message.edit_text(
            "✅ 检查通过。\n\n"
            "开始导入 93 个商品和 93 张图片..."
        )

        imported = 0
        matched = 0
        failed = []

        for i in range(1, 94):

            code = f"P{i:03d}"

            data = by_code[code]

            # 创建商品
            product_id = insert_product(
                name=data["name"],
                code=data["code"],
                category=data["category"],
                price=data["price"],
                stock=data["stock"],
                description=data["description"],
                active=1,
            )

            imported += 1

            # 上传图片到 Telegram
            try:

                with open(
                    image_by_number[i],
                    "rb",
                ) as photo_file:

                    sent = (
                        await context.bot.send_photo(
                            chat_id=(
                                update.effective_chat.id
                            ),
                            photo=photo_file,
                            caption=(
                                f"{code} "
                                f"{data['name']}"
                            ),
                        )
                    )

                telegram_photo = (
                    sent.photo[-1]
                )

                set_product_photo(
                    product_id,
                    telegram_photo.file_id,
                )

                matched += 1

                # 删除临时图片消息
                try:

                    await context.bot.delete_message(
                        chat_id=(
                            update.effective_chat.id
                        ),
                        message_id=(
                            sent.message_id
                        ),
                    )

                except Exception as exc:

                    print(
                        "[ZIP] 删除临时图片消息失败:",
                        exc,
                    )

            except Exception:

                traceback.print_exc()

                failed.append(
                    code
                )

            # 更新进度
            if (
                i % 5 == 0
                or i == 93
            ):

                try:

                    await status_message.edit_text(
                        f"⏳ 已处理 {i}/93\n"
                        f"商品：{imported}\n"
                        f"图片：{matched}"
                    )

                except Exception:
                    pass

        # ----------------------------------------------------
        # 完成
        # ----------------------------------------------------

        result = (
            "✅ 一键上传完成\n\n"
            f"商品导入：{imported}/93\n"
            f"图片保存：{matched}/93"
        )

        if failed:

            result += (
                "\n\n❌ 图片上传失败："
                + ", ".join(
                    failed[:30]
                )
            )

        await status_message.edit_text(
            result
        )

        # 删除管理员上传的 ZIP 消息
        try:

            await update.message.delete()

        except Exception as exc:

            print(
                "[ZIP] 删除上传 ZIP 消息失败:",
                exc,
            )

    except Exception as exc:

        traceback.print_exc()

        try:

            await status_message.edit_text(
                "❌ 一键上传失败："
                f"{exc}"
            )

        except Exception:
            pass

    finally:

        shutil.rmtree(
            temp_root,
            ignore_errors=True,
        )


# ============================================================
# CSV / ZIP 文件消息
# ============================================================

async def handle_document(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "你没有管理员权限。"
        )

        return

    document = (
        update.message.document
    )

    filename = (
        document.file_name
        or ""
    ).lower()

    # ZIP
    if filename.endswith(".zip"):

        await process_zip_import(
            update,
            context,
            document,
        )

        return

    # CSV
    if filename.endswith(".csv"):

        try:

            telegram_file = (
                await context.bot.get_file(
                    document.file_id
                )
            )

            target = (
                UPLOAD_DIR
                / "uploaded.csv"
            )

            await telegram_file.download_to_drive(
                custom_path=str(target)
            )

            # 如果数据库为空，直接导入
            if product_count() == 0:

                imported = import_csv_file(
                    target
                )

                await update.message.reply_text(
                    "✅ CSV 上传并导入完成。\n\n"
                    f"新增商品：{imported} 条",
                    reply_markup=main_menu(),
                )

            else:

                await update.message.reply_text(
                    "✅ CSV 已保存。\n\n"
                    f"文件：{target.name}\n"
                    "当前数据库已有商品，为防止重复导入，"
                    "本次没有自动导入。",
                    reply_markup=main_menu(),
                )

            try:

                await update.message.delete()

            except Exception as exc:

                print(
                    "[CSV] 删除上传 CSV 消息失败:",
                    exc,
                )

        except Exception as exc:

            traceback.print_exc()

            await update.message.reply_text(
                "❌ CSV 处理失败："
                f"{exc}"
            )

        return

    await update.message.reply_text(
        "支持上传 ZIP 或 CSV 文件。"
    )


# ============================================================
# 按钮处理
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    # --------------------------------------------------------
    # 首页
    # --------------------------------------------------------

    if data == "home":

        context.user_data.clear()

        await query.message.reply_text(
            "请选择功能：",
            reply_markup=main_menu(),
        )

        return

    # --------------------------------------------------------
    # 商品目录
    # --------------------------------------------------------

    if data == "catalog":

        await show_catalog(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # 所有商品
    # --------------------------------------------------------

    if data == "products":

        await show_all_products(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # 添加商品
    # --------------------------------------------------------

    if data == "add":

        await add_product_start(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # 询价
    # --------------------------------------------------------

    if data == "ask":

        await start_inquiry(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # 分类
    # --------------------------------------------------------

    if data.startswith("cat:"):

        category = data.split(
            ":",
            1,
        )[1]

        if category in CATS:

            await show_category(
                update,
                context,
                category,
            )

        return

    # --------------------------------------------------------
    # 商品
    # --------------------------------------------------------

    if data.startswith("product:"):

        raw_id = data.split(
            ":",
            1,
        )[1]

        if raw_id.isdigit():

            await show_product(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 商品询价
    # --------------------------------------------------------

    if data.startswith("inquiry:"):

        raw_id = data.split(
            ":",
            1,
        )[1]

        if raw_id.isdigit():

            await start_inquiry(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 管理后台首页
    # --------------------------------------------------------

    if data == "admin:home":

        if is_admin(
            update.effective_user.id
        ):

            # callback 没有 update.message
            # 这里直接发送后台
            total = product_count()

            active = fetch_one(
                """
                SELECT COUNT(*) AS c
                FROM products
                WHERE active = 1
                """
            )

            inquiries = fetch_one(
                """
                SELECT COUNT(*) AS c
                FROM inquiries
                WHERE status = 'new'
                """
            )

            text = (
                "🛠 管理后台\n\n"
                f"商品总数：{total}\n"
                f"上架商品："
                f"{int(active['c']) if active else 0}\n"
                f"待处理询价："
                f"{int(inquiries['c']) if inquiries else 0}\n\n"
                "请选择操作："
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "📊 商品统计",
                        callback_data="admin:status",
                    ),
                    InlineKeyboardButton(
                        "📥 只导入CSV",
                        callback_data="admin:csv",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📦 一键上传商品+图片",
                        callback_data="admin:zip_import",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📦 商品列表",
                        callback_data="admin:list",
                    ),
                    InlineKeyboardButton(
                        "💬 询价记录",
                        callback_data="admin:inquiries",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🏠 返回",
                        callback_data="home",
                    ),
                ],
            ]

            await query.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    keyboard
                ),
            )

        return

    # --------------------------------------------------------
    # 管理商品
    # --------------------------------------------------------

    if data.startswith("admin:edit:"):

        if not is_admin(
            update.effective_user.id
        ):
            return

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await show_admin_product(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 编辑商品资料
    # --------------------------------------------------------

    if data.startswith(
        "admin:edittext:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_edit_start(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 修改库存
    # --------------------------------------------------------

    if data.startswith(
        "admin:stock:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_stock_start(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 更换图片
    # --------------------------------------------------------

    if data.startswith(
        "admin:photo:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_replace_photo_start(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 上下架
    # --------------------------------------------------------

    if data.startswith(
        "admin:toggle:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_toggle_product(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 删除确认
    # --------------------------------------------------------

    if data.startswith(
        "admin:delete:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_delete_confirm(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 确认删除
    # --------------------------------------------------------

    if data.startswith(
        "admin:delok:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_delete_product(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 询价详情
    # --------------------------------------------------------

    if data.startswith(
        "admin:inq:"
    ):

        raw_id = data.split(
            ":"
        )[-1]

        if raw_id.isdigit():

            await admin_inquiry_detail(
                update,
                context,
                int(raw_id),
            )

        return

    # --------------------------------------------------------
    # 询价状态
    # --------------------------------------------------------

    if data.startswith(
        "admin:inqstatus:"
    ):

        parts = data.split(":")

        if (
            len(parts) == 4
            and parts[2].isdigit()
            and parts[3] in {
                "new",
                "done",
            }
        ):

            await admin_mark_inquiry(
                update,
                context,
                int(parts[2]),
                parts[3],
            )

        return

    # --------------------------------------------------------
    # 商品统计
    # --------------------------------------------------------

    if data == "admin:status":

        if is_admin(
            update.effective_user.id
        ):

            await admin_status(
                update,
                context,
            )

        return

    # --------------------------------------------------------
    # ZIP 导入说明
    # --------------------------------------------------------

    if data == "admin:zip_import":

        if is_admin(
            update.effective_user.id
        ):

            await query.message.reply_text(
                "📦 一键上传商品+图片\n\n"
                "请发送一个 ZIP 文件，里面必须包含：\n\n"
                "1. 1 个 CSV，共 93 条商品\n"
                "2. 93 张图片：01 到 93\n\n"
                "图片映射：\n"
                "01 → P001\n"
                "02 → P002\n"
                "03 → P003\n"
                "……\n"
                "93 → P093\n\n"
                "图片支持：JPG / JPEG / PNG / WEBP\n\n"
                "发送 ZIP 后，系统会自动导入商品并保存图片。"
            )

        return

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    if data == "admin:csv":

        if is_admin(
            update.effective_user.id
        ):

            await admin_csv(
                update,
                context,
            )

        return

    # --------------------------------------------------------
    # 商品列表
    # --------------------------------------------------------

    if data == "admin:list":

        if is_admin(
            update.effective_user.id
        ):

            await admin_list(
                update,
                context,
            )

        return

    # --------------------------------------------------------
    # 询价列表
    # --------------------------------------------------------

    if data == "admin:inquiries":

        if is_admin(
            update.effective_user.id
        ):

            await admin_inquiries(
                update,
                context,
            )

        return


# ============================================================
# CSV 后台按钮
# ============================================================

async def admin_csv(
    update,
    context,
):

    if not is_admin(
        update.effective_user.id
    ):
        return

    # 优先使用管理员刚刚上传的 CSV
    uploaded_csv = (
        UPLOAD_DIR
        / "uploaded.csv"
    )

    target_csv = None

    if uploaded_csv.exists():
        target_csv = uploaded_csv

    elif CSV_FILE.exists():
        target_csv = CSV_FILE

    if target_csv is None:

        await update.effective_message.reply_text(
            "没有找到 CSV 文件。\n\n"
            f"默认文件：{CSV_FILE.name}"
        )

        return

    if product_count() > 0:

        await update.effective_message.reply_text(
            "当前数据库已有商品。\n\n"
            "为了防止重复导入，本次没有导入。"
        )

        return

    try:

        imported = import_csv_file(
            target_csv
        )

        await update.effective_message.reply_text(
            "✅ CSV 导入完成。\n\n"
            f"新增：{imported} 条"
        )

    except Exception as exc:

        traceback.print_exc()

        await update.effective_message.reply_text(
            "❌ CSV 导入失败："
            f"{exc}"
        )


# ============================================================
# 错误处理
# ============================================================

async def error_handler(
    update,
    context,
):

    print(
        "BOT ERROR:"
    )

    if context.error:

        traceback.print_exception(
            type(context.error),
            context.error,
            context.error.__traceback__,
        )


# ============================================================
# 主程序
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing. "
            "Please set BOT_TOKEN in Render Environment Variables."
        )

    # 初始化数据库
    init_db()

    # 如果数据库为空，自动读取默认 CSV
    try:

        imported = (
            import_csv_if_empty()
        )

        print(
            f"CSV imported: {imported}"
        )

    except Exception:

        traceback.print_exc()

    # 启动 Web
    start_web_server()

    # --------------------------------------------------------
    # Telegram 启动检查
    # --------------------------------------------------------

    async def telegram_startup(
        app,
    ):

        print(
            "[TG] Startup check: connecting to Telegram..."
        )

        try:

            # 删除旧 webhook
            await app.bot.delete_webhook(
                drop_pending_updates=True
            )

            print(
                "[TG] Old webhook removed successfully."
            )

            # 测试 Bot
            me = await app.bot.get_me()

            print(
                f"[TG] Connected OK: "
                f"@{me.username} "
                f"(id={me.id})"
            )

            info = (
                await app.bot.get_webhook_info()
            )

            webhook_url = (
                getattr(
                    info,
                    "url",
                    "",
                )
                or ""
            )

            pending = getattr(
                info,
                "pending_update_count",
                0,
            )

            if webhook_url:

                print(
                    "[TG] WARNING: webhook is still set: "
                    f"{webhook_url}"
                )

            else:

                print(
                    "[TG] Polling ready. "
                    f"pending_updates={pending}"
                )

        except Exception as exc:

            print(
                "[TG] STARTUP ERROR:"
            )

            print(
                f"[TG] {type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            raise

    async def telegram_shutdown(
        app,
    ):

        print(
            "[TG] Telegram application shutting down."
        )

    # --------------------------------------------------------
    # 创建 Application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(
            telegram_startup
        )
        .post_shutdown(
            telegram_shutdown
        )
        .build()
    )

    # --------------------------------------------------------
    # 命令
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    # --------------------------------------------------------
    # 按钮
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # --------------------------------------------------------
    # 图片
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    # --------------------------------------------------------
    # 文件
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    # --------------------------------------------------------
    # 普通文字
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text,
        )
    )

    # --------------------------------------------------------
    # 错误处理
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # 启动
    # --------------------------------------------------------

    print(
        "Telegram bot starting..."
    )

    print(
        f"Web URL: {WEB_URL}"
    )

    print(
        f"Port: {PORT}"
    )

    print(
        f"Admins: {ADMINS}"
    )

    print(
        "[TG] Mode: polling"
    )

    try:

        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )

    except Exception as exc:

        print(
            "[TG] POLLING STOPPED WITH ERROR:"
        )

        print(
            f"[TG] {type(exc).__name__}: {exc}"
        )

        traceback.print_exc()

        raise


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    main()
