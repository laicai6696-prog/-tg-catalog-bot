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

from telegram import (
Update,
InlineKeyboardButton,
InlineKeyboardMarkup,
ReplyKeyboardMarkup,
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

=========================================================

基础配置

=========================================================

load_dotenv()

BASE_DIR = Path(file).resolve().parent

BOT_TOKEN = os.getenv(“BOT_TOKEN”, “”).strip()

WEB_URL = os.getenv(
“WEB_URL”,
“https://tg-catalog-bot-10.onrender.com”,
).strip().rstrip(”/”)

try:
PORT = int(os.getenv(“PORT”, “10000”))
except Exception:
PORT = 10000

DATABASE_URL = os.getenv(“DATABASE_URL”, “”).strip()

CSV_FILE = BASE_DIR / “Telegram商品导入表_93个_可直接导入.csv”

UPLOAD_DIR = BASE_DIR / “uploads”
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

=========================================================

管理员

=========================================================

def get_admin_ids():
raw = os.getenv(“ADMIN_IDS”, “”).strip()

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
        print("警告：ADMIN_IDS 中无法识别：", item)
return result

ADMINS = get_admin_ids()

def is_admin(user_id):
try:
return int(user_id) in ADMINS
except Exception:
return False

=========================================================

12 个商品系列

=========================================================

CATS = {
“c1”: “和天下系列”,
“c2”: “熊猫系列”,
“c3”: “南京系列”,
“c4”: “荷花系列”,
“c5”: “芙蓉王系列”,
“c6”: “牡丹系列”,
“c7”: “黄金叶系列”,
“c8”: “苏烟系列”,
“c9”: “利群系列”,
“c10”: “黄鹤楼系列”,
“c11”: “中华系列”,
“c12”: “白皮系列”,
}

CAT_BY_NAME = {
value: key
for key, value in CATS.items()
}

def category_name(category):
if not category:
return “未分类”

if category in CATS:
    return CATS[category]
return str(category)

=========================================================

数据库配置

=========================================================

USE_POSTGRES = DATABASE_URL.lower().startswith(
(“postgres://”, “postgresql://”, “postgres”)
)

if USE_POSTGRES:
try:
import psycopg2
from psycopg2.extras import RealDictCursor
except Exception:
print(“检测到 DATABASE_URL，但没有安装 psycopg2-binary。”)
raise
else:
DB_FILE = BASE_DIR / “bot.db”

def db_connect():
if USE_POSTGRES:
return psycopg2.connect(
DATABASE_URL,
cursor_factory=RealDictCursor,
)

conn = sqlite3.connect(
    str(DB_FILE),
    timeout=30,
    check_same_thread=False,
)
conn.row_factory = sqlite3.Row
return conn

def placeholder():
return “%s” if USE_POSTGRES else “?”

=========================================================

数据库初始化

=========================================================

def init_db():
conn = db_connect()

try:
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT DEFAULT '',
                category TEXT DEFAULT '',
                price TEXT DEFAULT '询价',
                stock INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                photo_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT DEFAULT '',
                product TEXT DEFAULT '',
                message TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS photo_id TEXT DEFAULT ''
            """
        )
        cur.execute(
            """
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS active INTEGER DEFAULT 1
            """
        )
        cur.execute(
            """
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''
            """
        )
        cur.execute(
            """
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS stock INTEGER DEFAULT 0
            """
        )
    else:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT DEFAULT '',
                category TEXT DEFAULT '',
                price TEXT DEFAULT '询价',
                stock INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                photo_id TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT DEFAULT '',
                product TEXT DEFAULT '',
                message TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("PRAGMA table_info(products)")
        columns = {
            row[1]
            for row in cur.fetchall()
        }
        if "photo_id" not in columns:
            cur.execute(
                """
                ALTER TABLE products
                ADD COLUMN photo_id TEXT DEFAULT ''
                """
            )
        if "active" not in columns:
            cur.execute(
                """
                ALTER TABLE products
                ADD COLUMN active INTEGER DEFAULT 1
                """
            )
        if "description" not in columns:
            cur.execute(
                """
                ALTER TABLE products
                ADD COLUMN description TEXT DEFAULT ''
                """
            )
        if "stock" not in columns:
            cur.execute(
                """
                ALTER TABLE products
                ADD COLUMN stock INTEGER DEFAULT 0
                """
            )
    conn.commit()
finally:
    conn.close()

=========================================================

数据库通用操作

=========================================================

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
finally:
    conn.close()

def insert_product(
name,
code,
category,
price,
stock,
description,
):
conn = db_connect()

try:
    cur = conn.cursor()
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
            (%s, %s, %s, %s, %s, %s, 1)
            RETURNING id
            """,
            (
                name,
                code,
                category,
                price,
                stock,
                description,
            ),
        )
        row = cur.fetchone()
        product_id = row["id"]
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
            (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                name,
                code,
                category,
                price,
                stock,
                description,
            ),
        )
        product_id = cur.lastrowid
    conn.commit()
    return int(product_id)
finally:
    conn.close()

def product_count():
row = fetch_one(
“””
SELECT COUNT(*) AS count
FROM products
“””
)

return int(row["count"])

=========================================================

CSV 工具

=========================================================

def normalize_header(value):
if value is None:
return “”

return (
    str(value)
    .replace("\ufeff", "")
    .replace("\n", "")
    .replace("\r", "")
    .strip()
)

def find_csv_column(fieldnames, candidates):
normalized = {}

for field in fieldnames or []:
    normalized[normalize_header(field)] = field
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
return “”

text = str(value).strip()
if not text:
    return ""
return (
    text
    .replace("￥", "")
    .replace("¥", "")
    .strip()
)

def import_csv_if_empty():
if not CSV_FILE.exists():

    print("没有找到 CSV：", CSV_FILE)
    return 0
current = product_count()
if current > 0:
    print(
        f"数据库已有 {current} 个商品，跳过 CSV 自动恢复。"
    )
    return current
print("数据库为空，开始从 CSV 恢复商品……")
rows = None
fieldnames = None
for encoding in (
    "utf-8-sig",
    "utf-8",
    "gb18030",
):
    try:
        with open(
            CSV_FILE,
            "r",
            encoding=encoding,
            newline="",
        ) as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        break
    except Exception:
        continue
if not rows:
    print("CSV 无法读取或为空。")
    return 0
name_col = find_csv_column(
    fieldnames,
    [
        "名称",
        "商品名称",
        "商品",
        "name",
        "Name",
    ],
)
code_col = find_csv_column(
    fieldnames,
    [
        "编号",
        "商品编号",
        "编码",
        "code",
        "Code",
    ],
)
category_col = find_csv_column(
    fieldnames,
    [
        "分类",
        "类别",
        "系列",
        "category",
        "Category",
    ],
)
price_col = find_csv_column(
    fieldnames,
    [
        "价格",
        "售价",
        "报价",
        "price",
        "Price",
    ],
)
stock_col = find_csv_column(
    fieldnames,
    [
        "库存",
        "stock",
        "Stock",
    ],
)
description_col = find_csv_column(
    fieldnames,
    [
        "描述",
        "商品描述",
        "说明",
        "description",
        "Description",
    ],
)
if not name_col:
    print("CSV 中没有找到商品名称字段。")
    print("CSV 字段：", fieldnames)
    return 0
inserted = 0
for row in rows:
    name = str(
        row.get(name_col, "")
    ).strip()
    if not name:
        continue
    code = (
        str(row.get(code_col, "")).strip()
        if code_col
        else ""
    )
    category = (
        str(
            row.get(category_col, "")
        ).strip()
        if category_col
        else ""
    )
    price = (
        normalize_price(
            row.get(price_col, "")
        )
        if price_col
        else ""
    )
    if not price:
        price = "询价"
    stock = (
        parse_stock(
            row.get(stock_col, "")
        )
        if stock_col
        else 0
    )
    description = (
        str(
            row.get(description_col, "")
        ).strip()
        if description_col
        else ""
    )
    if category in CAT_BY_NAME:
        category = CAT_BY_NAME[category]
    try:
        ph = placeholder()
        exists = fetch_one(
            f"""
            SELECT id
            FROM products
            WHERE name = {ph}
            AND code = {ph}
            LIMIT 1
            """,
            (
                name,
                code,
            ),
        )
        if exists:
            continue
        insert_product(
            name=name,
            code=code,
            category=category,
            price=price,
            stock=stock,
            description=description,
        )
        inserted += 1
    except Exception:
        traceback.print_exc()
total = product_count()
print(
    f"CSV 自动恢复完成：新增 {inserted} 个商品，当前共 {total} 个商品。"
)
return total

=========================================================

Telegram API

=========================================================

def telegram_api(method, payload):
if not BOT_TOKEN:
return None

url = (
    "https://api.telegram.org/"
    f"bot{BOT_TOKEN}/{method}"
)
data = json.dumps(
    payload,
    ensure_ascii=False,
).encode("utf-8")
request = Request(
    url,
    data=data,
    headers={
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    with urlopen(
        request,
        timeout=30,
    ) as response:
        return json.loads(
            response.read()
        )
except Exception as e:
    print(
        "Telegram API 错误：",
        e,
    )
    return None

def telegram_get_file(file_id):
result = telegram_api(
“getFile”,
{
“file_id”: file_id,
},
)

if not result:
    return None
if not result.get("ok"):
    return None
return result["result"].get("file_path")

=========================================================

商品图片

=========================================================

def get_product_photo(product_id):
ph = placeholder()

row = fetch_one(
    f"""
    SELECT photo_id
    FROM products
    WHERE id = {ph}
    """,
    (
        product_id,
    ),
)
if not row:
    return None
photo_id = str(
    row.get("photo_id") or ""
).strip()
return photo_id or None

=========================================================

HTTP Web 服务

=========================================================

def run_http_server():
server = ThreadingHTTPServer(
(
“0.0.0.0”,
PORT,
),
WebHandler,
)

print(
    f"HTTP Server running on port {PORT}"
)
server.serve_forever()

def notify_admins_sync(product, message):
if not ADMINS:
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
                "chat_id": admin_id,
                "text": text,
            },
        )
    except Exception:
        traceback.print_exc()

class WebHandler(BaseHTTPRequestHandler):

def log_message(self, format, *args):
    return
def send_json(self, data, status=200):
    body = json.dumps(
        data,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    self.send_response(status)
    self.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )
    self.send_header(
        "Access-Control-Allow-Origin",
        "*",
    )
    self.send_header(
        "Content-Length",
        str(len(body)),
    )
    self.end_headers()
    self.wfile.write(body)
def send_bytes(
    self,
    data,
    content_type,
    status=200,
):
    self.send_response(status)
    self.send_header(
        "Content-Type",
        content_type,
    )
    self.send_header(
        "Content-Length",
        str(len(data)),
    )
    self.send_header(
        "Cache-Control",
        "public, max-age=3600",
    )
    self.end_headers()
    self.wfile.write(data)
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
            index_file = (
                BASE_DIR
                / "web"
                / "index.html"
            )
            if not index_file.exists():
                self.send_json(
                    {
                        "ok": False,
                        "error": "web/index.html 不存在",
                    },
                    404,
                )
                return
            self.send_bytes(
                index_file.read_bytes(),
                "text/html; charset=utf-8",
            )
            return
        # 系统状态
        if path == "/api/status":
            total = fetch_one(
                """
                SELECT COUNT(*) AS count
                FROM products
                """
            )["count"]
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
            self.send_json(
                {
                    "ok": True,
                    "total_products": int(total),
                    "active_products": int(active),
                    "products_with_photos": int(photos),
                    "csv_exists": CSV_FILE.exists(),
                }
            )
            return
        # 分类
        if path == "/api/categories":
            result = []
            for code, name in CATS.items():
                ph = placeholder()
                row = fetch_one(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM products
                    WHERE active = 1
                    AND category = {ph}
                    """,
                    (
                        code,
                    ),
                )
                result.append(
                    {
                        "id": code,
                        "name": name,
                        "count": int(row["count"]),
                    }
                )
            self.send_json(result)
            return
        # 商品
        if path == "/api/products":
            active_only = query.get(
                "active",
                ["1"],
            )[0]
            category = query.get(
                "category",
                [""],
            )[0]
            sql = """
                SELECT
                    id,
                    name,
                    code,
                    category,
                    price,
                    stock,
                    description,
                    active,
                    photo_id
                FROM products
            """
            params = []
            conditions = []
            if active_only == "1":
                conditions.append(
                    "active = 1"
                )
            if category:
                conditions.append(
                    "category = " + placeholder()
                )
                params.append(category)
            if conditions:
                sql += (
                    " WHERE "
                    + " AND ".join(conditions)
                )
            sql += " ORDER BY id DESC"
            rows = fetch_all(
                sql,
                tuple(params),
            )
            result = []
            for row in rows:
                item = dict(row)
                item["category_name"] = category_name(
                    item.get("category")
                )
                if item.get("photo_id"):
                    item["photo_url"] = (
                        "/api/photo?id="
                        + str(item["id"])
                    )
                else:
                    item["photo_url"] = ""
                item.pop("photo_id", None)
                result.append(item)
            self.send_json(result)
            return
        # 商品图片
        if path == "/api/photo":
            raw_id = query.get(
                "id",
                [""],
            )[0]
            try:
                product_id = int(raw_id)
            except Exception:
                self.send_json(
                    {
                        "ok": False,
                        "error": "商品ID错误",
                    },
                    400,
                )
                return
            photo_id = get_product_photo(
                product_id
            )
            if not photo_id:
                self.send_json(
                    {
                        "ok": False,
                        "error": "商品没有图片",
                    },
                    404,
                )
                return
            file_path = telegram_get_file(
                photo_id
            )
            if not file_path:
                self.send_json(
                    {
                        "ok": False,
                        "error": "Telegram 图片不存在",
                    },
                    404,
                )
                return
            file_url = (
                "https://api.telegram.org/"
                f"file/bot{BOT_TOKEN}/"
                f"{file_path}"
            )
            with urlopen(
                file_url,
                timeout=60,
            ) as response:
                data = response.read()
            content_type = (
                mimetypes.guess_type(file_path)[0]
                or "image/jpeg"
            )
            self.send_bytes(
                data,
                content_type,
            )
            return
        # favicon
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_json(
            {
                "ok": False,
                "error": "Not Found",
            },
            404,
        )
    except Exception as e:
        traceback.print_exc()
        try:
            self.send_json(
                {
                    "ok": False,
                    "error": str(e),
                },
                500,
            )
        except Exception:
            pass
def do_POST(self):
    parsed = urlparse(self.path)
    if parsed.path != "/api/inquiry":
        self.send_json(
            {
                "ok": False,
                "error": "Not Found",
            },
            404,
        )
        return
    try:
        length = int(
            self.headers.get(
                "Content-Length",
                "0",
            )
        )
        if length <= 0 or length > 100000:
            self.send_json(
                {
                    "ok": False,
                    "error": "请求数据无效",
                },
                400,
            )
            return
        raw = self.rfile.read(length)
        data = json.loads(
            raw.decode("utf-8")
        )
        product = str(
            data.get(
                "product",
                "",
            )
        ).strip()
        message = str(
            data.get(
                "message",
                "",
            )
        ).strip()
        if not product:
            self.send_json(
                {
                    "ok": False,
                    "error": "没有商品",
                },
                400,
            )
            return
        if not message:
            message = "客户未填写具体内容"
        ph = placeholder()
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
                {ph},
                {ph},
                {ph},
                {ph},
                'new'
            )
            """,
            (
                0,
                "",
                product,
                message,
            ),
        )
        self.send_json(
            {
                "ok": True,
                "message": "询价已提交",
            }
        )
        threading.Thread(
            target=notify_admins_sync,
            args=(
                product,
                message,
            ),
            daemon=True,
        ).start()
    except Exception:
        traceback.print_exc()
        self.send_json(
            {
                "ok": False,
                "error": "提交失败",
            },
            500,
        )

=========================================================

Telegram 主菜单

=========================================================

def main_menu():

return ReplyKeyboardMarkup(
    [
        [
            "🛍️ 商品目录",
            "📋 商品",
        ],
        [
            "💬 询价",
            "➕ 添加商品",
        ],
    ],
    resize_keyboard=True,
)

=========================================================

/start

=========================================================

async def start(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

context.user_data.clear()
await update.message.reply_text(
    "欢迎使用商品目录机器人！\n\n"
    "请选择下面的功能：",
    reply_markup=main_menu(),
)

=========================================================

/cancel

=========================================================

async def cancel(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

context.user_data.clear()
await update.message.reply_text(
    "✅ 当前操作已取消。",
    reply_markup=main_menu(),
)

=========================================================

商品目录

=========================================================

async def open_catalog(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

keyboard = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "🛍️ 打开商品目录",
                web_app=WebAppInfo(
                    url=WEB_URL
                ),
            )
        ]
    ]
)
await update.message.reply_text(
    "点击下面按钮打开商品目录：",
    reply_markup=keyboard,
)

=========================================================

商品列表

=========================================================

async def show_products(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

rows = fetch_all(
    """
    SELECT
        id,
        name,
        price,
        stock
    FROM products
    WHERE active = 1
    ORDER BY id DESC
    """
)
if not rows:
    await update.message.reply_text(
        "目前没有商品。"
    )
    return
buttons = []
for row in rows[:60]:
    buttons.append(
        [
            InlineKeyboardButton(
                str(row["name"])[:40],
                callback_data=f"product:{row['id']}",
            )
        ]
    )
await update.message.reply_text(
    f"📦 商品列表\n\n"
    f"当前共有 {len(rows)} 个商品：",
    reply_markup=InlineKeyboardMarkup(
        buttons
    ),
)

=========================================================

商品详情

=========================================================

async def product_callback(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

query = update.callback_query
await query.answer()
try:
    product_id = int(
        query.data.split(":", 1)[1]
    )
except Exception:
    return
ph = placeholder()
row = fetch_one(
    f"""
    SELECT *
    FROM products
    WHERE id = {ph}
    """,
    (
        product_id,
    ),
)
if not row:
    await query.message.reply_text(
        "商品不存在。"
    )
    return
text = (
    f"📦 {row['name']}\n\n"
    f"编号：{row.get('code') or '-'}\n"
    f"分类：{category_name(row.get('category'))}\n"
    f"价格：{row.get('price') or '询价'}\n"
    f"库存：{row.get('stock', 0)}"
)
if row.get("description"):
    text += (
        "\n\n说明：\n"
        + str(row["description"])
    )
keyboard = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "💬 我要询价",
                callback_data=f"inquiry:{row['id']}",
            )
        ]
    ]
)
if row.get("photo_id"):
    try:
        await query.message.reply_photo(
            photo=row["photo_id"],
            caption=text,
            reply_markup=keyboard,
        )
        return
    except Exception as e:
        print(
            "发送商品图片失败：",
            e,
        )
await query.message.reply_text(
    text,
    reply_markup=keyboard,
)

=========================================================

询价按钮

=========================================================

async def inquiry_callback(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

query = update.callback_query
await query.answer()
try:
    product_id = int(
        query.data.split(":", 1)[1]
    )
except Exception:
    return
ph = placeholder()
row = fetch_one(
    f"""
    SELECT *
    FROM products
    WHERE id = {ph}
    """,
    (
        product_id,
    ),
)
if not row:
    await query.message.reply_text(
        "商品不存在。"
    )
    return
context.user_data["inquiry_product"] = row["name"]
await query.message.reply_text(
    "请输入您的询价内容。\n\n"
    "例如：\n"
    "需要10件，请报价。\n\n"
    "输入 /cancel 可以取消。"
)

=========================================================

添加商品

=========================================================

async def start_add_product(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

if not is_admin(update.effective_user.id):
    await update.message.reply_text(
        "没有管理员权限。"
    )
    return
context.user_data["adding_product"] = True
await update.message.reply_text(
    "请输入商品信息：\n\n"
    "名称|编号|分类|价格|库存|描述\n\n"
    "例如：\n"
    "软和天下|HT001|c1|350|20|软和天下\n\n"
    "输入 /cancel 可以取消。"
)

=========================================================

普通文字处理

=========================================================

async def message_handler(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

if not update.message:
    return
text = (
    update.message.text or ""
).strip()
user = update.effective_user
# -----------------------------------------------------
# 等待商品图片
# -----------------------------------------------------
waiting_photo_id = context.user_data.get(
    "waiting_photo_product_id"
)
if waiting_photo_id:
    if text == "跳过":
        context.user_data.pop(
            "waiting_photo_product_id",
            None,
        )
        await update.message.reply_text(
            "✅ 已跳过商品图片。\n"
            "商品添加完成。",
            reply_markup=main_menu(),
        )
        return
    await update.message.reply_text(
        "请发送商品图片。\n"
        "如果不需要图片，请发送：跳过\n\n"
        "输入 /cancel 可以取消。"
    )
    return
# -----------------------------------------------------
# 正在询价
# -----------------------------------------------------
inquiry_product = context.user_data.get(
    "inquiry_product"
)
if inquiry_product:
    if not text:
        return
    username = (
        user.username
        or user.full_name
        or "未知"
    )
    user_id = (
        user.id
        if user
        else 0
    )
    ph = placeholder()
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
            {ph},
            {ph},
            {ph},
            {ph},
            'new'
        )
        """,
        (
            user_id,
            username,
            inquiry_product,
            text,
        ),
    )
    context.user_data.pop(
        "inquiry_product",
        None,
    )
    await update.message.reply_text(
        "✅ 询价已提交。\n"
        "我们会尽快联系您。",
        reply_markup=main_menu(),
    )
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🔔 新询价\n\n"
                    f"商品：{inquiry_product}\n\n"
                    f"客户：@{username}\n"
                    f"用户ID：{user_id}\n\n"
                    f"内容：\n{text}"
                ),
            )
        except Exception as e:
            print(
                "管理员通知失败：",
                e,
            )
    return
# -----------------------------------------------------
# 主菜单
# -----------------------------------------------------
if text in (
    "🛍️ 商品目录",
    "商品目录",
    "小程序",
):
    await open_catalog(
        update,
        context,
    )
    return
if text in (
    "📋 商品",
    "商品",
):
    await show_products(
        update,
        context,
    )
    return
if text in (
    "💬 询价",
    "询价",
):
    await update.message.reply_text(
        "请先选择商品，然后点击“我要询价”。"
    )
    return
if text in (
    "➕ 添加商品",
    "添加商品",
):
    await start_add_product(
        update,
        context,
    )
    return
# -----------------------------------------------------
# 添加商品
# -----------------------------------------------------
if context.user_data.get(
    "adding_product"
):
    if not is_admin(user.id):
        return
    parts = [
        item.strip()
        for item in text.split("|")
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
        "|".join(parts[5:])
        if len(parts) > 5
        else ""
    )
    if category in CAT_BY_NAME:
        category = CAT_BY_NAME[category]
    if category not in CATS:
        await update.message.reply_text(
            "分类不正确。\n\n"
            "可用分类：\n"
            + "\n".join(
                f"{code} = {name}"
                for code, name in CATS.items()
            )
        )
        return
    if not name:
        await update.message.reply_text(
            "商品名称不能为空。"
        )
        return
    try:
        product_id = insert_product(
            name=name,
            code=code,
            category=category,
            price=price or "询价",
            stock=stock,
            description=description,
        )
        context.user_data.pop(
            "adding_product",
            None,
        )
        context.user_data[
            "waiting_photo_product_id"
        ] = product_id
        await update.message.reply_text(
            "✅ 商品添加成功。\n\n"
            f"商品ID：#{product_id}\n"
            f"商品：{name}\n\n"
            "请发送商品图片。\n"
            "如果不需要图片，请发送：跳过"
        )
    except Exception as e:
        traceback.print_exc()
        await update.message.reply_text(
            "❌ 商品添加失败：\n"
            + str(e)
        )
    return
await update.message.reply_text(
    "请选择下面功能：",
    reply_markup=main_menu(),
)

=========================================================

商品图片上传

=========================================================

async def photo_handler(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

if not update.message:
    return
product_id = context.user_data.get(
    "waiting_photo_product_id"
)
if not product_id:
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "目前没有等待图片的商品。"
        )
    return
photo = update.message.photo
if not photo:
    return
telegram_photo = photo[-1]
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
        product_id,
    ),
)
product = fetch_one(
    f"""
    SELECT name
    FROM products
    WHERE id = {ph}
    """,
    (
        product_id,
    ),
)
context.user_data.pop(
    "waiting_photo_product_id",
    None,
)
name = (
    product["name"]
    if product
    else "商品"
)
await update.message.reply_text(
    "✅ 商品图片已保存。\n\n"
    f"商品：{name}\n"
    f"商品ID：#{product_id}\n\n"
    "商品已经完成添加。",
    reply_markup=main_menu(),
)

=========================================================

管理员后台

=========================================================

async def admin(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

if not is_admin(update.effective_user.id):
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
                callback_data="admin:list",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 系统状态",
                callback_data="admin:status",
            )
        ],
        [
            InlineKeyboardButton(
                "📥 CSV恢复商品",
                callback_data="admin:importcsv",
            )
        ],
        [
            InlineKeyboardButton(
                "💬 查看询价",
                callback_data="admin:inquiries",
            )
        ],
    ]
)
await update.message.reply_text(
    "🔐 管理员后台\n\n"
    f"当前商品：{total}\n\n"
    "请选择操作：",
    reply_markup=keyboard,
)

=========================================================

管理员 Callback

=========================================================

async def admin_callback(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

query = update.callback_query
await query.answer()
if not is_admin(query.from_user.id):
    await query.message.reply_text(
        "没有管理员权限。"
    )
    return
data = query.data
# -----------------------------------------------------
# 系统状态
# -----------------------------------------------------
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
        f"商品总数：{int(total)}\n"
        f"上架商品：{int(active)}\n"
        f"有图片：{int(photos)}\n"
        f"数据库："
        + (
            "PostgreSQL"
            if USE_POSTGRES
            else "SQLite"
        )
        + "\n"
        f"WEB：{WEB_URL}"
    )
    return
# -----------------------------------------------------
# CSV 恢复
# -----------------------------------------------------
if data == "admin:importcsv":
    if not CSV_FILE.exists():
        await query.message.reply_text(
            "找不到 93 商品 CSV 文件：\n"
            f"{CSV_FILE.name}"
        )
        return
    before = product_count()
    if before == 0:
        after = import_csv_if_empty()
    else:
        after = before
    await query.message.reply_text(
        "✅ CSV 恢复检查完成。\n\n"
        f"商品数量：{after}"
    )
    return
# -----------------------------------------------------
# 商品列表
# -----------------------------------------------------
if data == "admin:list":
    rows = fetch_all(
        """
        SELECT
            id,
            name,
            category,
            price,
            stock,
            photo_id
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
        "📦 商品列表",
        "",
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
    for i in range(
        0,
        len(text),
        3500,
    ):
        await query.message.reply_text(
            text[i:i + 3500]
        )
    return
# -----------------------------------------------------
# 询价
# -----------------------------------------------------
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
        "💬 最近询价",
        "",
    ]
    for row in rows:
        username = row.get("username") or "-"
        lines.append(
            f"#{row['id']} "
            f"{row.get('product') or '-'}\n"
            f"客户：{username}\n"
            f"{row.get('message') or '-'}\n"
            f"状态：{row.get('status') or '-'}\n"
        )
    text = "\n".join(lines)
    for i in range(
        0,
        len(text),
        3500,
    ):
        await query.message.reply_text(
            text[i:i + 3500]
        )
    return

=========================================================

ZIP 图片匹配

=========================================================

def normalize_match_text(value):
value = str(value or “”).lower()

value = re.sub(
    r"[\s_\-—–.()（）\[\]【】]+",
    "",
    value,
)
return value

def normalize_filename(filename):
return normalize_match_text(
Path(str(filename)).stem
)

def find_product_for_filename(
filename,
products,
):

target = normalize_filename(filename)
if not target:
    return None
# 1. 商品 ID
for product in products:
    if str(product["id"]) == target:
        return product
# 2. 商品编号
for product in products:
    code = normalize_match_text(
        product.get("code")
    )
    if code and code == target:
        return product
# 3. 商品名称完全匹配
for product in products:
    name = normalize_match_text(
        product.get("name")
    )
    if name and name == target:
        return product
# 4. 文件名包含编号
for product in products:
    code = normalize_match_text(
        product.get("code")
    )
    if code and code in target:
        return product
# 5. 文件名包含商品名称
for product in products:
    name = normalize_match_text(
        product.get("name")
    )
    if name and name in target:
        return product
return None

=========================================================

ZIP 安全解压

=========================================================

def safe_extract_zip(
zip_path,
extract_dir,
):

root = extract_dir.resolve()
with zipfile.ZipFile(
    zip_path,
    "r",
) as z:
    for member in z.infolist():
        target = (
            extract_dir
            / member.filename
        ).resolve()
        if target != root and root not in target.parents:
            raise ValueError(
                "ZIP 中存在非法文件路径。"
            )
    z.extractall(extract_dir)

=========================================================

ZIP 上传

=========================================================

async def zip_document_handler(
update: Update,
context: ContextTypes.DEFAULT_TYPE,
):

if not update.message:
    return
user_id = update.effective_user.id
if not is_admin(user_id):
    await update.message.reply_text(
        "没有管理员权限。"
    )
    return
document = update.message.document
if not document:
    return
filename = (
    document.file_name
    or "products.zip"
)
if not filename.lower().endswith(".zip"):
    await update.message.reply_text(
        "请发送 ZIP 文件。"
    )
    return
temp_dir = Path(
    tempfile.mkdtemp(
        prefix="catalog_zip_"
    )
)
zip_path = temp_dir / "images.zip"
try:
    telegram_file = await context.bot.get_file(
        document.file_id
    )
    await telegram_file.download_to_drive(
        custom_path=str(zip_path)
    )
    await update.message.reply_text(
        "✅ ZIP 已收到。\n\n"
        "正在后台处理图片，请不要重复上传。\n"
        "处理完成后会通知你。"
    )
    context.application.create_task(
        process_zip_background(
            context.bot,
            update.effective_chat.id,
            zip_path,
            temp_dir,
        )
    )
except Exception as e:
    traceback.print_exc()
    shutil.rmtree(
        temp_dir,
        ignore_errors=True,
    )
    await update.message.reply_text(
        "❌ ZIP 接收失败：\n"
        + str(e)
    )

=========================================================

ZIP 后台处理

=========================================================

async def process_zip_background(
bot,
chat_id,
zip_path,
temp_dir,
):

success = 0
failed = 0
skipped = 0
try:
    extract_dir = temp_dir / "extracted"
    extract_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    # -------------------------------------------------
    # 安全解压
    # -------------------------------------------------
    safe_extract_zip(
        zip_path,
        extract_dir,
    )
    # -------------------------------------------------
    # 找图片
    # -------------------------------------------------
    allowed = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }
    image_files = []
    for file in extract_dir.rglob("*"):
        if not file.is_file():
            continue
        if file.suffix.lower() in allowed:
            image_files.append(file)
    if not image_files:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ ZIP 里面没有找到图片。\n\n"
                "支持：JPG、JPEG、PNG、WEBP"
            ),
        )
        return
    # -------------------------------------------------
    # 商品
    # -------------------------------------------------
    products = fetch_all(
        """
        SELECT
            id,
            name,
            code
        FROM products
        ORDER BY id
        """
    )
    if not products:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ 当前没有商品。\n\n"
                "请先恢复商品。"
            ),
        )
        return
    # -------------------------------------------------
    # 逐张匹配并上传
    # -------------------------------------------------
    for image_file in image_files:
        try:
            product = find_product_for_filename(
                image_file.name,
                products,
            )
            if not product:
                skipped += 1
                continue
            with open(
                image_file,
                "rb",
            ) as f:
                message = await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=(
                        "商品图片："
                        f"{product['name']}\n"
                        f"商品ID：#{product['id']}"
                    ),
                )
            if not message.photo:
                failed += 1
                continue
            telegram_photo = message.photo[-1]
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
                    product["id"],
                ),
            )
            success += 1
            await asyncio.sleep(0.25)
        except Exception as e:
            failed += 1
            print(
                "图片处理失败：",
                image_file,
                e,
            )
    # -------------------------------------------------
    # 完成
    # -------------------------------------------------
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ ZIP 图片处理完成\n\n"
            f"图片总数：{len(image_files)}\n"
            f"成功匹配：{success}\n"
            f"未匹配：{skipped}\n"
            f"失败：{failed}\n\n"
            "现在可以打开商品目录检查图片。"
        ),
    )
except Exception:
    traceback.print_exc()
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ ZIP 后台处理失败。\n\n"
                "请查看 Render Logs。"
            ),
        )
    except Exception:
        pass
finally:
    shutil.rmtree(
        temp_dir,
        ignore_errors=True,
    )

=========================================================

错误处理

=========================================================

async def error_handler(
update,
context,
):

print("Telegram Bot Error:")
if context.error:
    traceback.print_exception(
        type(context.error),
        context.error,
        context.error.__traceback__,
    )

=========================================================

主程序

=========================================================

def main():

if not BOT_TOKEN:
    raise RuntimeError(
        "请在 Render Environment Variables 设置 BOT_TOKEN"
    )
print("=" * 60)
print(
    "Telegram 商品目录 Bot 启动"
)
print(
    "WEB_URL:",
    WEB_URL,
)
print(
    "PORT:",
    PORT,
)
print(
    "DATABASE:",
    (
        "PostgreSQL"
        if USE_POSTGRES
        else str(DB_FILE)
    ),
)
print(
    "ADMIN_IDS:",
    ADMINS,
)
print("=" * 60)
# -----------------------------------------------------
# 数据库
# -----------------------------------------------------
init_db()
# -----------------------------------------------------
# CSV 自动恢复
# -----------------------------------------------------
try:
    import_csv_if_empty()
except Exception:
    traceback.print_exc()
print(
    "当前商品数量：",
    product_count(),
)
# -----------------------------------------------------
# HTTP 服务
# -----------------------------------------------------
http_thread = threading.Thread(
    target=run_http_server,
    daemon=True,
)
http_thread.start()
# -----------------------------------------------------
# Telegram Application
# -----------------------------------------------------
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)
# -----------------------------------------------------
# Commands
# -----------------------------------------------------
application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)
application.add_handler(
    CommandHandler(
        "admin",
        admin,
    )
)
application.add_handler(
    CommandHandler(
        "cancel",
        cancel,
    )
)
# -----------------------------------------------------
# 商品图片
# -----------------------------------------------------
application.add_handler(
    MessageHandler(
        filters.PHOTO,
        photo_handler,
    )
)
# -----------------------------------------------------
# ZIP
# -----------------------------------------------------
application.add_handler(
    MessageHandler(
        filters.Document.ALL,
        zip_document_handler,
    )
)
# -----------------------------------------------------
# 管理员 Callback
# -----------------------------------------------------
application.add_handler(
    CallbackQueryHandler(
        admin_callback,
        pattern=r"^admin:",
    )
)
# -----------------------------------------------------
# 商品 Callback
# -----------------------------------------------------
application.add_handler(
    CallbackQueryHandler(
        product_callback,
        pattern=r"^product:",
    )
)
# -----------------------------------------------------
# 询价 Callback
# -----------------------------------------------------
application.add_handler(
    CallbackQueryHandler(
        inquiry_callback,
        pattern=r"^inquiry:",
    )
)
# -----------------------------------------------------
# 普通文字
# -----------------------------------------------------
application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_handler,
    )
)
# -----------------------------------------------------
# 错误处理
# -----------------------------------------------------
application.add_error_handler(
    error_handler
)
print(
    "Bot polling started..."
)
application.run_polling(
    allowed_updates=Update.ALL_TYPES
)

=========================================================

程序入口

=========================================================

if name == “main”:
main()
