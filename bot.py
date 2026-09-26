import os
import csv
import asyncio
import io
import json
import time
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
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()


# ============================================================
# 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

WEB_URL = os.getenv(
    "WEB_URL",
    "https://tg-catalog-bot-10.onrender.com"
).strip().rstrip("/")

PORT = int(os.getenv("PORT", "10000"))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

CSV_FILE = BASE_DIR / "Telegram商品导入表_93个_可直接导入.csv"

UPLOAD_DIR = BASE_DIR / "uploads"

UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# 管理员
# ============================================================

def get_admin_ids():
    raw = os.getenv("ADMIN_IDS", "").strip()

    result = set()

    if not raw:
        return result

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))
        except ValueError:
            print(
                f"警告：ADMIN_IDS 中无法识别：{item}"
            )

    return result


ADMINS = get_admin_ids()


def is_admin(user_id):
    return int(user_id) in ADMINS


# ============================================================
# 12 个系列
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

CAT_BY_NAME = {
    value: key
    for key, value in CATS.items()
}


# ============================================================
# 数据库
#
# 优先使用 DATABASE_URL。
# 没有 DATABASE_URL 时使用 bot.db。
# ============================================================

USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:
        print(
            "检测到 DATABASE_URL，但没有 psycopg2。"
        )
        print(
            "请安装 psycopg2-binary，程序将退出。"
        )
        raise
else:
    DB_FILE = BASE_DIR / "bot.db"


# ============================================================
# 数据库连接
# ============================================================

def db_connect():
    if USE_POSTGRES:
        return psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor
        )

    conn = sqlite3.connect(
        str(DB_FILE),
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def placeholder():
    return "%s" if USE_POSTGRES else "?"


def now_sql():
    if USE_POSTGRES:
        return "NOW()"

    return "CURRENT_TIMESTAMP"


# ============================================================
# 数据库初始化
# ============================================================

def init_db():

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
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    photo_id TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS inquiries (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    product TEXT,
                    message TEXT,
                    status TEXT DEFAULT 'new',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

        else:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT,
                    category TEXT,
                    price TEXT,
                    stock INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    photo_id TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    product TEXT,
                    message TEXT,
                    status TEXT DEFAULT 'new',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

        conn.commit()

    finally:
        conn.close()


# ============================================================
# 数据库查询
# ============================================================

def fetch_all(sql, params=()):

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(sql, params)

        rows = cur.fetchall()

        return [dict(row) for row in rows]

    finally:

        conn.close()


def fetch_one(sql, params=()):

    conn = db_connect()

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

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(sql, params)

        conn.commit()

        return cur

    finally:

        conn.close()


# ============================================================
# 商品数量
# ============================================================

def product_count():

    row = fetch_one(
        "SELECT COUNT(*) AS count FROM products"
    )

    return int(row["count"])


# ============================================================
# CSV 导入
# ============================================================

def normalize_header(value):

    if value is None:
        return ""

    return (
        str(value)
        .replace("\ufeff", "")
        .replace("\n", "")
        .replace("\r", "")
        .strip()
    )


def find_csv_column(fieldnames, candidates):

    normalized = {}

    for field in fieldnames:

        normalized[
            normalize_header(field)
        ] = field

    for candidate in candidates:

        candidate = normalize_header(candidate)

        if candidate in normalized:
            return normalized[candidate]

    return None


def parse_stock(value):

    if value is None:
        return 0

    text = str(value).strip()

    if not text:
        return 0

    try:
        return int(float(text))
    except Exception:
        return 0


def normalize_price(value):

    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    return text.replace("￥", "").replace("¥", "").strip()


def import_csv_if_empty():

    if not CSV_FILE.exists():

        print(
            f"没有找到 CSV：{CSV_FILE}"
        )

        return 0

    count = product_count()

    if count > 0:

        print(
            f"数据库已有 {count} 个商品，"
            f"跳过 CSV 自动恢复。"
        )

        return count

    print(
        "数据库为空，开始从 93 商品 CSV 自动恢复..."
    )

    inserted = 0

    with open(
        CSV_FILE,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as f:

        reader = csv.DictReader(f)

        if not reader.fieldnames:
            return 0

        name_col = find_csv_column(
            reader.fieldnames,
            [
                "名称",
                "商品名称",
                "商品",
                "name",
                "Name"
            ]
        )

        code_col = find_csv_column(
            reader.fieldnames,
            [
                "编号",
                "商品编号",
                "编码",
                "code",
                "Code"
            ]
        )

        category_col = find_csv_column(
            reader.fieldnames,
            [
                "分类",
                "类别",
                "系列",
                "category",
                "Category"
            ]
        )

        price_col = find_csv_column(
            reader.fieldnames,
            [
                "价格",
                "售价",
                "报价",
                "price",
                "Price"
            ]
        )

        stock_col = find_csv_column(
            reader.fieldnames,
            [
                "库存",
                "stock",
                "Stock"
            ]
        )

        description_col = find_csv_column(
            reader.fieldnames,
            [
                "描述",
                "商品描述",
                "说明",
                "description",
                "Description"
            ]
        )

        if not name_col:

            print(
                "CSV 中没有找到商品名称字段。"
            )

            print(
                "CSV 字段：",
                reader.fieldnames
            )

            return 0


        for row in reader:

            name = str(
                row.get(name_col, "")
            ).strip()

            if not name:
                continue

            code = (
                str(row.get(code_col, "")).strip()
                if code_col else ""
            )

            category = (
                str(row.get(category_col, "")).strip()
                if category_col else ""
            )

            price = (
                normalize_price(
                    row.get(price_col, "")
                )
                if price_col else ""
            )

            stock = (
                parse_stock(
                    row.get(stock_col, "")
                )
                if stock_col else 0
            )

            description = (
                str(
                    row.get(description_col, "")
                ).strip()
                if description_col else ""
            )


            # 自动识别系列名称
            if category in CAT_BY_NAME:

                category_value = CAT_BY_NAME[
                    category
                ]

            elif category in CATS:

                category_value = category

            else:

                category_value = category


            conn = db_connect()

            try:

                cur = conn.cursor()

                ph = placeholder()

                cur.execute(
                    f"""
                    SELECT id
                    FROM products
                    WHERE name = {ph}
                    AND code = {ph}
                    LIMIT 1
                    """,
                    (
                        name,
                        code
                    )
                )

                exists = cur.fetchone()

                if exists:
                    conn.rollback()
                    continue


                if USE_POSTGRES:

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
                        VALUES
                        (%s,%s,%s,%s,%s,%s,1)
                        """,
                        (
                            name,
                            code,
                            category_value,
                            price,
                            stock,
                            description
                        )
                    )

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
                        VALUES
                        (?,?,?,?,?,?,1)
                        """,
                        (
                            name,
                            code,
                            category_value,
                            price,
                            stock,
                            description
                        )
                    )

                conn.commit()

                inserted += 1

            except Exception:

                conn.rollback()

                print(
                    "导入商品失败：",
                    name
                )

            finally:

                conn.close()


    print(
        f"CSV 自动恢复完成：新增 {inserted} 个商品"
    )

    return product_count()


# ============================================================
# 商品分类名称
# ============================================================

def category_name(category):

    if not category:
        return ""

    if category in CATS:
        return CATS[category]

    return category


# ============================================================
# Telegram Bot API
# ============================================================

def telegram_api(method, payload):

    if not BOT_TOKEN:
        raise RuntimeError(
            "没有设置 BOT_TOKEN"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    data = json.dumps(
        payload
    ).encode("utf-8")

    request = Request(
        url,
        data=data,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST"
    )

    with urlopen(
        request,
        timeout=30
    ) as response:

        return json.loads(
            response.read()
        )


def telegram_get_file(file_id):

    result = telegram_api(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram getFile 失败"
        )

    return result["result"]["file_path"]


def download_telegram_file(file_id):

    file_path = telegram_get_file(
        file_id
    )

    url = (
        f"https://api.telegram.org/"
        f"file/bot{BOT_TOKEN}/"
        f"{file_path}"
    )

    with urlopen(
        url,
        timeout=60
    ) as response:

        return response.read()


# ============================================================
# 图片 API
# ============================================================

def get_product_photo(product_id):

    product = fetch_one(
        """
        SELECT photo_id
        FROM products
        WHERE id = ?
        """
        if not USE_POSTGRES
        else
        """
        SELECT photo_id
        FROM products
        WHERE id = %s
        """,
        (product_id,)
    )

    if not product:
        return None

    photo_id = (
        product.get("photo_id") or ""
    ).strip()

    if not photo_id:
        return None

    return photo_id


# ============================================================
# HTTP 服务
# ============================================================

class WebHandler(BaseHTTPRequestHandler):

    def log_message(
        self,
        format,
        *args
    ):
        return


    def send_json(
        self,
        data,
        status=200
    ):

        body = json.dumps(
            data,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(body)


    def send_bytes(
        self,
        data,
        content_type
    ):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.send_header(
            "Cache-Control",
            "public, max-age=3600"
        )

        self.end_headers()

        self.wfile.write(data)


    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        query = parse_qs(
            parsed.query
        )


        # ----------------------------
        # 首页
        # ----------------------------

        if path == "/":

            index_file = (
                BASE_DIR /
                "web" /
                "index.html"
            )

            if not index_file.exists():

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "web/index.html 不存在"
                    },
                    404
                )

                return


            try:

                data = index_file.read_bytes()

                self.send_bytes(
                    data,
                    "text/html; charset=utf-8"
                )

            except Exception as e:

                self.send_json(
                    {
                        "ok": False,
                        "error": str(e)
                    },
                    500
                )

            return


        # ----------------------------
        # 状态
        # ----------------------------

        if path == "/api/status":

            total = fetch_one(
                "SELECT COUNT(*) AS count FROM products"
            )["count"]

            active = fetch_one(
                "SELECT COUNT(*) AS count "
                "FROM products WHERE active = 1"
            )["count"]

            photos = fetch_one(
                "SELECT COUNT(*) AS count "
                "FROM products "
                "WHERE photo_id IS NOT NULL "
                "AND photo_id <> ''"
            )["count"]

            self.send_json(
                {
                    "ok": True,
                    "database":
                        (
                            "PostgreSQL"
                            if USE_POSTGRES
                            else str(DB_FILE)
                        ),
                    "total_products":
                        int(total),
                    "active_products":
                        int(active),
                    "products_with_photos":
                        int(photos),
                    "csv_file":
                        CSV_FILE.name,
                    "csv_exists":
                        CSV_FILE.exists()
                }
            )

            return


        # ----------------------------
        # 分类
        # ----------------------------

        if path == "/api/categories":

            rows = fetch_all(
                """
                SELECT category, COUNT(*) AS count
                FROM products
                WHERE active = 1
                GROUP BY category
                ORDER BY category
                """
            )

            result = []

            for row in rows:

                result.append(
                    {
                        "id":
                            row["category"],
                        "name":
                            category_name(
                                row["category"]
                            ),
                        "count":
                            int(row["count"])
                    }
                )

            self.send_json(result)

            return


        # ----------------------------
        # 商品
        # ----------------------------

        if path == "/api/products":

            active_only = (
                query.get(
                    "active",
                    ["1"]
                )[0]
            )

            if active_only == "1":

                rows = fetch_all(
                    """
                    SELECT *
                    FROM products
                    WHERE active = 1
                    ORDER BY id DESC
                    """
                )

            else:

                rows = fetch_all(
                    """
                    SELECT *
                    FROM products
                    ORDER BY id DESC
                    """
                )


            result = []

            for row in rows:

                item = dict(row)

                item["category_name"] = (
                    category_name(
                        item.get("category")
                    )
                )

                if item.get("photo_id"):

                    item["photo_url"] = (
                        "/api/photo?id="
                        + str(item["id"])
                    )

                else:

                    item["photo_url"] = ""


                result.append(item)


            self.send_json(result)

            return


        # ----------------------------
        # 商品图片
        # ----------------------------

        if path == "/api/photo":

            raw_id = (
                query.get("id", [""])[0]
            )

            try:

                product_id = int(raw_id)

            except Exception:

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "商品ID错误"
                    },
                    400
                )

                return


            photo_id = get_product_photo(
                product_id
            )

            if not photo_id:

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "商品没有图片"
                    },
                    404
                )

                return


            try:

                file_path = (
                    telegram_get_file(
                        photo_id
                    )
                )

                url = (
                    f"https://api.telegram.org/"
                    f"file/bot{BOT_TOKEN}/"
                    f"{file_path}"
                )

                with urlopen(
                    url,
                    timeout=60
                ) as response:

                    data = response.read()


                content_type = (
                    mimetypes.guess_type(
                        file_path
                    )[0]
                    or "image/jpeg"
                )


                self.send_bytes(
                    data,
                    content_type
                )

            except Exception as e:

                print(
                    "图片读取失败：",
                    e
                )

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "图片读取失败"
                    },
                    500
                )

            return


        # ----------------------------
        # favicon
        # ----------------------------

        if path == "/favicon.ico":

            self.send_response(204)
            self.end_headers()

            return


        self.send_json(
            {
                "ok": False,
                "error": "Not Found"
            },
            404
        )


    def do_POST(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path


        # ----------------------------
        # Mini App 询价
        # ----------------------------

        if path == "/api/inquiry":

            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                raw = self.rfile.read(
                    length
                )

                data = json.loads(
                    raw.decode("utf-8")
                )


                product = str(
                    data.get(
                        "product",
                        ""
                    )
                ).strip()

                message = str(
                    data.get(
                        "message",
                        ""
                    )
                ).strip()


                if not product:

                    self.send_json(
                        {
                            "ok": False,
                            "error":
                                "没有商品"
                        },
                        400
                    )

                    return


                user_id = 0
                username = ""


                # Telegram WebApp 用户
                # 如果 Mini App 在 Telegram 中打开，
                # 这里尝试读取 initData 不做信任认证，
                # 仅作为辅助信息。

                execute(
                    """
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
                        %s,%s,%s,%s,'new'
                    )
                    """
                    if USE_POSTGRES
                    else
                    """
                    INSERT INTO inquiries
                    (
                        user_id,
                        username,
                        product,
                        message,
                        status
                    )
                    VALUES
                    (?,?,?,?, 'new')
                    """,
                    (
                        user_id,
                        username,
                        product,
                        message
                    )
                )


                self.send_json(
                    {
                        "ok": True,
                        "message":
                            "询价已提交"
                    }
                )


                # 异步通知管理员
                threading.Thread(
                    target=
                        notify_admins_sync,
                    args=(
                        product,
                        message
                    ),
                    daemon=True
                ).start()


            except Exception as e:

                print(
                    "询价接口错误：",
                    e
                )

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "提交失败"
                    },
                    500
                )

            return


        self.send_json(
            {
                "ok": False,
                "error": "Not Found"
            },
            404
        )


# ============================================================
# 管理员通知
# ============================================================

def notify_admins_sync(
    product,
    message
):

    if not ADMINS:
        return

    if not BOT_TOKEN:
        return


    text = (
        "🔔 新询价\n\n"
        f"商品：{product}\n\n"
        f"{message}"
    )


    for admin_id in ADMINS:

        try:

            telegram_api(
                "sendMessage",
                {
                    "chat_id":
                        admin_id,
                    "text":
                        text
                }
            )

        except Exception as e:

            print(
                "通知管理员失败：",
                e
            )


# ============================================================
# Bot 菜单
# ============================================================

def main_menu():

    return ReplyKeyboardMarkup(
        [
            [
                "🛍️ 商品目录",
                "📋 商品"
            ],
            [
                "💬 询价",
                "➕ 添加商品"
            ],
        ],
        resize_keyboard=True
    )


# ============================================================
# /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "欢迎使用商品目录\n\n"
        "请选择下面的功能："
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu()
    )


# ============================================================
# 商品目录按钮
# ============================================================

async def open_catalog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛍️ 打开商品目录",
                    web_app=None
                )
            ]
        ]
    )

    # 不能直接把字符串 URL 塞进 web_app，
    # 需要 WebAppInfo。
    from telegram import WebAppInfo

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛍️ 打开商品目录",
                    web_app=WebAppInfo(
                        url=WEB_URL
                    )
                )
            ]
        ]
    )

    await update.message.reply_text(
        "点击下面打开商品目录：",
        reply_markup=keyboard
    )


# ============================================================
# 商品列表
# ============================================================

async def show_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    rows = fetch_all(
        """
        SELECT *
        FROM products
        WHERE active = 1
        ORDER BY id DESC
        """
    )


    if not rows:

        await update.message.reply_text(
            "目前没有商品。\n\n"
            "管理员可以使用 /admin 管理商品。"
        )

        return


    text = "📦 商品列表\n\n"

    buttons = []


    for row in rows[:30]:

        name = row["name"]

        price = (
            row["price"]
            or "询价"
        )

        text += (
            f"#{row['id']} "
            f"{name} | "
            f"{price}\n"
        )


        buttons.append(
            [
                InlineKeyboardButton(
                    name[:30],
                    callback_data=
                        f"product:{row['id']}"
                )
            ]
        )


    await update.message.reply_text(
        text,
        reply_markup=
            InlineKeyboardMarkup(
                buttons
            )
    )


# ============================================================
# 商品详情 Callback
# ============================================================

async def product_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    data = query.data

    if not data.startswith(
        "product:"
    ):
        return


    product_id = int(
        data.split(":", 1)[1]
    )


    ph = placeholder()

    row = fetch_one(
        f"""
        SELECT *
        FROM products
        WHERE id = {ph}
        """,
        (product_id,)
    )


    if not row:

        await query.message.reply_text(
            "商品不存在。"
        )

        return


    text = (
        f"📦 {row['name']}\n\n"
        f"编号：{row.get('code') or '-'}\n"
        f"分类："
        f"{category_name(row.get('category'))}\n"
        f"价格："
        f"{row.get('price') or '询价'}\n"
        f"库存："
        f"{row.get('stock', 0)}\n"
    )


    if row.get("description"):

        text += (
            "\n说明：\n"
            f"{row['description']}"
        )


    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💬 我要询价",
                    callback_data=
                        f"inquiry:{row['id']}"
                )
            ]
        ]
    )


    if row.get("photo_id"):

        try:

            await query.message.reply_photo(
                photo=row["photo_id"],
                caption=text,
                reply_markup=keyboard
            )

            return

        except Exception as e:

            print(
                "发送商品图片失败：",
                e
            )


    await query.message.reply_text(
        text,
        reply_markup=keyboard
    )


# ============================================================
# Telegram 询价
# ============================================================

async def inquiry_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    product_id = int(
        query.data.split(":", 1)[1]
    )


    ph = placeholder()

    product = fetch_one(
        f"""
        SELECT *
        FROM products
        WHERE id = {ph}
        """,
        (product_id,)
    )


    if not product:

        await query.message.reply_text(
            "商品不存在。"
        )

        return


    context.user_data[
        "inquiry_product"
    ] = product["name"]


    await query.message.reply_text(
        "请输入您的询价内容。\n\n"
        "例如：\n"
        "需要10件，请报价\n\n"
        "也可以直接发送：\n"
        "数量 + 联系方式"
    )


# ============================================================
# 普通消息
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return


    text = (
        update.message.text or ""
    ).strip()


    # --------------------------------
    # 正在询价
    # --------------------------------

    inquiry_product = (
        context.user_data.get(
            "inquiry_product"
        )
    )


    if inquiry_product:

        user = update.effective_user

        username = (
            user.username
            or ""
        )

        user_id = (
            user.id
            if user
            else 0
        )


        execute(
            """
            INSERT INTO inquiries
            (
                user_id,
                username,
                product,
                message,
                status
            )
            VALUES
            (%s,%s,%s,%s,'new')
            """
            if USE_POSTGRES
            else
            """
            INSERT INTO inquiries
            (
                user_id,
                username,
                product,
                message,
                status
            )
            VALUES
            (?,?,?,?, 'new')
            """,
            (
                user_id,
                username,
                inquiry_product,
                text
            )
        )


        context.user_data.pop(
            "inquiry_product",
            None
        )


        await update.message.reply_text(
            "✅ 询价已提交。\n"
            "我们会尽快联系您。"
        )


        for admin_id in ADMINS:

            try:

                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🔔 新询价\n\n"
                        f"商品："
                        f"{inquiry_product}\n\n"
                        f"客户："
                        f"@{username or '未知'}\n"
                        f"用户ID："
                        f"{user_id}\n\n"
                        f"内容：\n"
                        f"{text}"
                    )
                )

            except Exception as e:

                print(
                    "管理员通知失败：",
                    e
                )


        return


    # --------------------------------
    # 菜单
    # --------------------------------

    if text in (
        "🛍️ 商品目录",
        "商品目录",
        "小程序"
    ):

        await open_catalog(
            update,
            context
        )

        return


    if text in (
        "📋 商品",
        "商品"
    ):

        await show_products(
            update,
            context
        )

        return


    if text in (
        "💬 询价",
        "询价"
    ):

        await update.message.reply_text(
            "请先打开商品目录，"
            "选择商品后点击“立即询价”。"
        )

        return


    if text in (
        "➕ 添加商品",
        "添加商品"
    ):

        if not is_admin(
            update.effective_user.id
        ):

            await update.message.reply_text(
                "没有管理员权限。"
            )

            return


        context.user_data[
            "adding_product"
        ] = True


        await update.message.reply_text(
            "请输入商品信息：\n\n"
            "名称|编号|分类|价格|库存|描述\n\n"
            "例如：\n"
            "软和天下|HT001|和天下系列|350|20|软和天下"
        )

        return


    # --------------------------------
    # 添加商品
    # --------------------------------

    if context.user_data.get(
        "adding_product"
    ):

        if not is_admin(
            update.effective_user.id
        ):

            return


        parts = [
            x.strip()
            for x in text.split("|")
        ]


        if len(parts) < 5:

            await update.message.reply_text(
                "格式不正确。\n\n"
                "请使用：\n"
                "名称|编号|分类|价格|库存|描述"
            )

            return


        name = parts[0]
        code = parts[1]
        category = parts[2]
        price = parts[3]
        stock = parse_stock(parts[4])
        description = (
            parts[5]
            if len(parts) > 5
            else ""
        )


        if category in CAT_BY_NAME:

            category = CAT_BY_NAME[
                category
            ]


        if category not in CATS:

            await update.message.reply_text(
                "分类不正确。\n\n"
                "可用分类：\n" +
                "\n".join(
                    CATS.values()
                )
            )

            return


        execute(
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
            VALUES
            (%s,%s,%s,%s,%s,%s,1)
            """
            if USE_POSTGRES
            else
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
            VALUES
            (?,?,?,?,?, ?,1)
            """,
            (
                name,
                code,
                category,
                price,
                stock,
                description
            )
        )


        context.user_data.pop(
            "adding_product",
            None
        )


        await update.message.reply_text(
            "✅ 商品添加成功。"
        )

        return


# ============================================================
# 接收图片
# ============================================================

async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return


    # 如果正在添加商品
    if context.user_data.get(
        "waiting_photo_product_id"
    ):

        product_id = context.user_data[
            "waiting_photo_product_id"
        ]


        photo = update.message.photo

        if not photo:
            return


        telegram_photo =
            photo[-1]


        photo_id = telegram_photo.file_id


        ph = placeholder()


        execute(
            f"""
            UPDATE products
            SET photo_id = {ph}
            WHERE id = {ph}
            """,
            (
                photo_id,
                product_id
            )
        )


        context.user_data.pop(
            "waiting_photo_product_id",
            None
        )


        await update.message.reply_text(
            "✅ 商品图片已保存。"
        )

        return


    await update.message.reply_text(
        "如果是商品图片，请先通过管理员商品管理流程上传。"
    )


# ============================================================
# /admin
# ============================================================

async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = (
        update.effective_user.id
    )


    if not is_admin(user_id):

        await update.message.reply_text(
            "没有管理员权限。"
        )

        return


    total = product_count()


    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📦 商品列表",
                    callback_data="admin:list"
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 系统状态",
                    callback_data="admin:status"
                )
            ],
            [
                InlineKeyboardButton(
                    "📥 CSV恢复商品",
                    callback_data="admin:importcsv"
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 查看询价",
                    callback_data="admin:inquiries"
                )
            ]
        ]
    )


    await update.message.reply_text(
        "🔐 管理员后台\n\n"
        f"当前商品：{total}\n\n"
        "请选择操作：",
        reply_markup=keyboard
    )


# ============================================================
# 管理员 Callback
# ============================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()


    if not is_admin(
        query.from_user.id
    ):

        await query.message.reply_text(
            "没有管理员权限。"
        )

        return


    data = query.data


    # --------------------------------
    # 状态
    # --------------------------------

    if data == "admin:status":

        total = product_count()

        active = fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM products
            WHERE active = 1
            """
        )["count"]

        photos = fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM products
            WHERE photo_id IS NOT NULL
            AND photo_id <> ''
            """
        )["count"]


        await query.message.reply_text(
            "📊 系统状态\n\n"
            f"商品总数：{total}\n"
            f"上架商品：{active}\n"
            f"有图片：{photos}\n"
            f"数据库："
            f"{'PostgreSQL' if USE_POSTGRES else 'SQLite'}"
        )

        return


    # --------------------------------
    # CSV
    # --------------------------------

    if data == "admin:importcsv":

        if not CSV_FILE.exists():

            await query.message.reply_text(
                "找不到 93 商品 CSV。"
            )

            return


        before = product_count()

        if before == 0:

            after = import_csv_if_empty()

        else:

            after = before


        await query.message.reply_text(
            "✅ CSV恢复检查完成。\n\n"
            f"商品数量：{after}"
        )

        return


    # --------------------------------
    # 商品列表
    # --------------------------------

    if data == "admin:list":

        rows = fetch_all(
            """
            SELECT id,name,category,price,stock,photo_id
            FROM products
            ORDER BY id
            """
        )


        if not rows:

            await query.message.reply_text(
                "暂无商品。"
            )

            return


        lines = [
            "📦 商品列表\n"
        ]


        for row in rows:

            photo_mark = (
                "🖼️"
                if row.get("photo_id")
                else "⬜"
            )


            lines.append(
                f"#{row['id']} "
                f"{photo_mark} "
                f"{row['name']} "
                f"| {row.get('price') or '询价'}"
            )


        text = "\n".join(lines)


        # Telegram 单条消息有限制
        for i in range(
            0,
            len(text),
            3500
        ):

            await query.message.reply_text(
                text[i:i+3500]
            )

        return


    # --------------------------------
    # 询价
    # --------------------------------

    if data == "admin:inquiries":

        rows = fetch_all(
            """
            SELECT *
            FROM inquiries
            ORDER BY id DESC
            LIMIT 30
            """
        )


        if not rows:

            await query.message.reply_text(
                "暂无询价。"
            )

            return


        lines = [
            "💬 最近询价\n"
        ]


        for row in rows:

            lines.append(
                f"#{row['id']} "
                f"{row['product']}\n"
                f"{row['message']}\n"
                f"状态：{row['status']}\n"
            )


        text = "\n".join(lines)


        await query.message.reply_text(
            text[:3900]
        )

        return


# ============================================================
# ZIP 图片后台处理
#
# 重要：
# 收到 ZIP 后马上回复。
# 真正处理在后台进行。
# 因此不会因为 93 张图片处理时间长
# 导致 Telegram 请求 Timed out。
# ============================================================

async def zip_document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return


    user_id = (
        update.effective_user.id
    )


    if not is_admin(user_id):

        await update.message.reply_text(
            "没有管理员权限。"
        )

        return


    document = update.message.document

    if not document:
        return


    file_name = (
        document.file_name
        or "products.zip"
    )


    if not file_name.lower().endswith(
        ".zip"
    ):

        await update.message.reply_text(
            "请发送 ZIP 文件。"
        )

        return


    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="catalog_zip_"
        )
    )


    zip_path = (
        temp_dir /
        file_name
    )


    try:

        telegram_file =
            await context.bot.get_file(
                document.file_id
            )


        await telegram_file.download_to_drive(
            custom_path=str(zip_path)
        )


        await update.message.reply_text(
            "✅ ZIP 已收到。\n\n"
            "正在后台处理图片，请不要重复上传。\n"
            "处理完成后我会通知你。"
        )


        # 后台任务
        asyncio_task = context.application.create_task(
            process_zip_background(
                context.bot,
                update.effective_chat.id,
                zip_path,
                temp_dir
            )
        )

    except Exception as e:

        print(
            "ZIP接收失败：",
            traceback.format_exc()
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


        await update.message.reply_text(
            "❌ ZIP 接收失败："
            f"{str(e)[:300]}"
        )


# ============================================================
# ZIP 后台处理
# ============================================================

async def process_zip_background(
    bot,
    chat_id,
    zip_path,
    temp_dir
):

    success = 0
    failed = 0
    skipped = 0


    try:

        extract_dir = (
            temp_dir /
            "extracted"
        )

        extract_dir.mkdir(
            parents=True,
            exist_ok=True
        )


        # ----------------------------
        # 解压
        # ----------------------------

        with zipfile.ZipFile(
            zip_path,
            "r"
        ) as z:

            z.extractall(
                extract_dir
            )


        # ----------------------------
        # 找图片
        # ----------------------------

        image_files = []


        for file in extract_dir.rglob("*"):

            if not file.is_file():
                continue


            suffix = (
                file.suffix.lower()
            )


            if suffix in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp"
            ):

                image_files.append(file)


        if not image_files:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ ZIP 里没有找到 "
                    "JPG / PNG / WEBP 图片。"
                )
            )

            return


        # ----------------------------
        # 获取商品
        # ----------------------------

        rows = fetch_all(
            """
            SELECT id,name,code
            FROM products
            ORDER BY id
            """
        )


        if not rows:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ 当前数据库没有商品。\n\n"
                    "请先恢复 93 个商品 CSV，"
                    "再上传图片 ZIP。"
                )
            )

            return


        # ----------------------------
        # 商品匹配
        # ----------------------------

        def normalized(text):

            return "".join(
                str(text or "")
                .lower()
                .split()
            )


        product_map = {}


        for product in rows:

            pid = str(
                product["id"]
            )

            name = normalized(
                product["name"]
            )

            code = normalized(
                product["code"]
            )


            product_map[pid] = product


            if code:

                product_map[
                    "code:" + code
                ] = product


            if name:

                product_map[
                    "name:" + name
                ] = product


        # ----------------------------
        # 逐张处理
        # ----------------------------

        for image_file in image_files:

            try:

                stem = normalized(
                    image_file.stem
                )


                product = None


                # 1. 商品ID
                if stem in product_map:

                    product = product_map[
                        stem
                    ]


                # 2. 编号
                if not product:

                    product = product_map.get(
                        "code:" + stem
                    )


                # 3. 商品名称
                if not product:

                    product = product_map.get(
                        "name:" + stem
                    )


                # 4. 包含编号
                if not product:

                    for key, item in product_map.items():

                        if not key.startswith(
                            "code:"
                        ):
                            continue

                        code = key[5:]

                        if (
                            code
                            and code in stem
                        ):

                            product = item

                            break


                # 5. 包含商品名称
                if not product:

                    for key, item in product_map.items():

                        if not key.startswith(
                            "name:"
                        ):
                            continue

                        name = key[5:]

                        if (
                            name
                            and name in stem
                        ):

                            product = item

                            break


                if not product:

                    skipped += 1

                    continue


                # ------------------------
                # 上传到 Telegram
                # ------------------------

                with open(
                    image_file,
                    "rb"
                ) as f:

                    message = (
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=(
                                "商品图片："
                                f"{product['name']}"
                            )
                        )
                    )


                if not message.photo:

                    failed += 1

                    continue


                photo_id = (
                    message.photo[-1].file_id
                )


                ph = placeholder()


                execute(
                    f"""
                    UPDATE products
                    SET photo_id = {ph}
                    WHERE id = {ph}
                    """,
                    (
                        photo_id,
                        product["id"]
                    )
                )


                success += 1


                # 避免一次性发送过快
                await asyncio.sleep(
                    0.15
                )


            except Exception as e:

                failed += 1

                print(
                    "图片处理失败：",
                    image_file,
                    e
                )


        # ----------------------------
        # 完成通知
        # ----------------------------

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ ZIP 图片处理完成\n\n"
                f"成功：{success}\n"
                f"跳过：{skipped}\n"
                f"失败：{failed}\n\n"
                "现在可以打开商品目录检查图片。"
            )
        )


    except Exception:

        print(
            "ZIP后台处理失败："
        )

        print(
            traceback.format_exc()
        )


        try:

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ ZIP 后台处理失败。\n\n"
                    "请把这条消息截图发给我。"
                )
            )

        except Exception:
            pass


    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


# ============================================================
# HTTP Server
# ============================================================

def run_http_server():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        WebHandler
    )


    print(
        f"HTTP server running on port {PORT}"
    )


    server.serve_forever()


# ============================================================
# 主程序
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "请在 Render Environment "
            "设置 BOT_TOKEN"
        )


    print("=" * 50)

    print(
        "Telegram 商品目录 Bot 启动"
    )

    print(
        "数据库：",
        (
            "PostgreSQL"
            if USE_POSTGRES
            else str(DB_FILE)
        )
    )

    print(
        "CSV：",
        CSV_FILE
    )

    print(
        "WEB_URL：",
        WEB_URL
    )

    print(
        "管理员：",
        ADMINS
    )

    print("=" * 50)


    # ----------------------------
    # 数据库
    # ----------------------------

    init_db()


    # ----------------------------
    # 自动恢复 CSV
    # ----------------------------

    import_csv_if_empty()


    print(
        "当前商品数量：",
        product_count()
    )


    # ----------------------------
    # HTTP
    # ----------------------------

    http_thread = threading.Thread(
        target=run_http_server,
        daemon=True
    )

    http_thread.start()


    # ----------------------------
    # Telegram
    # ----------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )


    # 基础命令

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    application.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )


    # 文本

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            message_handler
        )
    )


    # 图片

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )


    # ZIP

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            zip_document_handler
        )
    )


    # Callback

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:"
        )
    )


    application.add_handler(
        CallbackQueryHandler(
            product_callback,
            pattern=r"^product:"
        )
    )


    application.add_handler(
        CallbackQueryHandler(
            inquiry_callback,
            pattern=r"^inquiry:"
        )
    )


    print(
        "Bot polling started."
    )


    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":

    main()
