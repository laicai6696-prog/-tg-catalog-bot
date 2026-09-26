import os
import io
import csv
import json
import time
import sqlite3
import zipfile
import threading
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# 基础配置
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

DB = str(BASE_DIR / "bot.db")

WEB_DIR = BASE_DIR / "web"

PORT = int(os.getenv("PORT", "10000"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()

ADMINS = set()

if ADMIN_IDS_RAW:
    for item in ADMIN_IDS_RAW.split(","):
        item = item.strip()
        if item.isdigit():
            ADMINS.add(int(item))


WEB_URL = os.getenv(
    "WEB_URL",
    "https://tg-catalog-bot-10.onrender.com"
).rstrip("/")


# =========================================================
# 12个商品系列
# =========================================================

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


# =========================================================
# 全局变量
# =========================================================

zip_jobs = {}

zip_jobs_lock = threading.Lock()

http_server = None


# =========================================================
# 数据库
# =========================================================

def get_conn():
    conn = sqlite3.connect(
        DB,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


def init_db():

    conn = get_conn()

    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT DEFAULT '',
            category TEXT DEFAULT 'c1',
            price TEXT DEFAULT '询价',
            stock INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            photo_id TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            username TEXT DEFAULT '',
            product TEXT DEFAULT '',
            message TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS zip_jobs (
            id TEXT PRIMARY KEY,
            filename TEXT,
            status TEXT,
            total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            message TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# 数据库商品操作
# =========================================================

def product_count():

    conn = get_conn()

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM products"
    ).fetchone()

    conn.close()

    return int(row["n"])


def products_with_photos():

    conn = get_conn()

    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM products
        WHERE photo_id IS NOT NULL
        AND photo_id != ''
        """
    ).fetchone()

    conn.close()

    return int(row["n"])


def get_products(active_only=False):

    conn = get_conn()

    if active_only:
        rows = conn.execute(
            """
            SELECT *
            FROM products
            WHERE active = 1
            ORDER BY
                CASE
                    WHEN category LIKE 'c%' THEN CAST(SUBSTR(category, 2) AS INTEGER)
                    ELSE 99
                END,
                id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM products
            ORDER BY id DESC
            """
        ).fetchall()

    conn.close()

    return rows


def get_product(product_id):

    conn = get_conn()

    row = conn.execute(
        """
        SELECT *
        FROM products
        WHERE id = ?
        """,
        (product_id,)
    ).fetchone()

    conn.close()

    return row


def add_product(
    name,
    code="",
    category="c1",
    price="询价",
    stock=0,
    description="",
    photo_id=""
):

    conn = get_conn()

    cur = conn.cursor()

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
            active,
            photo_id
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            name,
            code,
            category,
            str(price),
            int(stock or 0),
            description,
            photo_id
        )
    )

    product_id = cur.lastrowid

    conn.commit()
    conn.close()

    return product_id


def update_product_photo(product_id, photo_id):

    conn = get_conn()

    conn.execute(
        """
        UPDATE products
        SET photo_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            photo_id,
            product_id
        )
    )

    conn.commit()
    conn.close()


def delete_product(product_id):

    conn = get_conn()

    conn.execute(
        """
        UPDATE products
        SET active = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (product_id,)
    )

    conn.commit()
    conn.close()


# =========================================================
# CSV自动恢复
# =========================================================

def find_csv_file():

    candidates = list(BASE_DIR.glob("*.csv"))

    if not candidates:
        return None

    # 优先找名字中包含 product / 商品 的文件
    preferred = []

    for path in candidates:

        name = path.name.lower()

        if (
            "product" in name
            or "商品" in name
            or "catalog" in name
            or "产品" in name
        ):
            preferred.append(path)

    if preferred:
        return preferred[0]

    return candidates[0]


def normalize_header(value):

    if value is None:
        return ""

    return (
        str(value)
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def get_csv_value(row, aliases):

    normalized = {}

    for key, value in row.items():

        normalized[
            normalize_header(key)
        ] = value

    for alias in aliases:

        key = normalize_header(alias)

        if key in normalized:
            return normalized[key]

    return ""


def parse_category(value):

    value = str(value or "").strip()

    if not value:
        return "c1"

    if value in CATS:
        return value

    if value in CAT_BY_NAME:
        return CAT_BY_NAME[value]

    for key, name in CATS.items():

        if value == name:
            return key

        if name.replace("系列", "") in value:
            return key

    if value.isdigit():

        number = int(value)

        if 1 <= number <= 12:
            return "c" + str(number)

    return "c1"


def import_csv_file(path):

    if not path.exists():
        return 0

    imported = 0

    conn = get_conn()

    try:

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            newline=""
        ) as f:

            reader = csv.DictReader(f)

            for row in reader:

                name = get_csv_value(
                    row,
                    [
                        "name",
                        "商品名称",
                        "商品",
                        "名称",
                        "产品名称",
                        "产品"
                    ]
                ).strip()

                if not name:
                    continue

                code = get_csv_value(
                    row,
                    [
                        "code",
                        "编号",
                        "商品编号",
                        "产品编号"
                    ]
                ).strip()

                category = parse_category(
                    get_csv_value(
                        row,
                        [
                            "category",
                            "分类",
                            "系列",
                            "商品分类"
                        ]
                    )
                )

                price = get_csv_value(
                    row,
                    [
                        "price",
                        "价格",
                        "报价",
                        "商品价格"
                    ]
                ).strip()

                if not price:
                    price = "询价"

                stock_raw = get_csv_value(
                    row,
                    [
                        "stock",
                        "库存",
                        "数量"
                    ]
                ).strip()

                try:
                    stock = int(
                        float(stock_raw)
                    ) if stock_raw else 0
                except Exception:
                    stock = 0

                description = get_csv_value(
                    row,
                    [
                        "description",
                        "描述",
                        "说明",
                        "备注"
                    ]
                ).strip()

                photo_id = get_csv_value(
                    row,
                    [
                        "photo_id",
                        "photoid",
                        "图片id",
                        "图片ID",
                        "telegram_file_id"
                    ]
                ).strip()

                # 防止重复导入
                existing = conn.execute(
                    """
                    SELECT id
                    FROM products
                    WHERE name = ?
                    AND code = ?
                    LIMIT 1
                    """,
                    (
                        name,
                        code
                    )
                ).fetchone()

                if existing:

                    conn.execute(
                        """
                        UPDATE products
                        SET category = ?,
                            price = ?,
                            stock = ?,
                            description = ?,
                            photo_id = CASE
                                WHEN ? != '' THEN ?
                                ELSE photo_id
                            END,
                            active = 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            category,
                            price,
                            stock,
                            description,
                            photo_id,
                            photo_id,
                            existing["id"]
                        )
                    )

                else:

                    conn.execute(
                        """
                        INSERT INTO products
                        (
                            name,
                            code,
                            category,
                            price,
                            stock,
                            description,
                            active,
                            photo_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            name,
                            code,
                            category,
                            price,
                            stock,
                            description,
                            photo_id
                        )
                    )

                imported += 1

        conn.commit()

    finally:

        conn.close()

    return imported


def auto_restore_from_csv():

    if product_count() > 0:
        return 0

    path = find_csv_file()

    if not path:
        return 0

    try:

        count = import_csv_file(path)

        print(
            f"CSV自动恢复完成：{count} 个商品"
        )

        return count

    except Exception as e:

        print(
            f"CSV自动恢复失败：{e}"
        )

        return 0


# =========================================================
# Telegram API 图片下载
# =========================================================

def telegram_api(method, params=None):

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN 未设置"
        )

    url = (
        "https://api.telegram.org/bot"
        + BOT_TOKEN
        + "/"
        + method
    )

    if params:

        query = "&".join(
            f"{k}={str(v)}"
            for k, v in params.items()
        )

        url += "?" + query

    req = Request(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0"
        }
    )

    with urlopen(
        req,
        timeout=30
    ) as response:

        return json.loads(
            response.read().decode(
                "utf-8"
            )
        )


def get_telegram_file_path(file_id):

    result = telegram_api(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "description",
                "Telegram getFile failed"
            )
        )

    return result["result"]["file_path"]


# =========================================================
# HTTP / Mini App
# =========================================================

class WebHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
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


    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        params = parse_qs(
            parsed.query
        )


        # -------------------------
        # 状态
        # -------------------------

        if path == "/api/status":

            total = product_count()

            self.send_json(
                {
                    "ok": True,
                    "database": DB,
                    "total_products": total,
                    "active_products": len(
                        get_products(True)
                    ),
                    "products_with_photos":
                        products_with_photos()
                }
            )

            return


        # -------------------------
        # 分类
        # -------------------------

        if path == "/api/categories":

            data = []

            for key, name in CATS.items():

                data.append(
                    {
                        "id": key,
                        "name": name
                    }
                )

            self.send_json(data)

            return


        # -------------------------
        # 商品
        # -------------------------

        if path == "/api/products":

            active_only = True

            if params.get("active"):

                active_only = (
                    params["active"][0]
                    != "0"
                )

            rows = get_products(
                active_only
            )

            result = []

            for row in rows:

                category_name = CATS.get(
                    row["category"],
                    row["category"]
                )

                item = {
                    "id": row["id"],
                    "name": row["name"],
                    "code": row["code"],
                    "category": row["category"],
                    "category_name":
                        category_name,
                    "price": row["price"],
                    "stock": row["stock"],
                    "description":
                        row["description"],
                    "active":
                        row["active"],
                    "photo_id":
                        row["photo_id"],
                    "photo_url":
                        (
                            "/api/photo?id="
                            + str(row["id"])
                        )
                        if row["photo_id"]
                        else ""
                }

                result.append(item)

            self.send_json(result)

            return


        # -------------------------
        # 图片
        # -------------------------

        if path == "/api/photo":

            try:

                product_id = int(
                    params.get(
                        "id",
                        ["0"]
                    )[0]
                )

            except Exception:

                self.send_error(
                    400,
                    "invalid id"
                )

                return


            row = get_product(
                product_id
            )

            if not row:

                self.send_error(
                    404,
                    "product not found"
                )

                return


            photo_id = row["photo_id"]

            if not photo_id:

                self.send_error(
                    404,
                    "photo not found"
                )

                return


            try:

                file_path = (
                    get_telegram_file_path(
                        photo_id
                    )
                )

                file_url = (
                    "https://api.telegram.org/file/bot"
                    + BOT_TOKEN
                    + "/"
                    + file_path
                )

                req = Request(
                    file_url,
                    headers={
                        "User-Agent":
                            "Mozilla/5.0"
                    }
                )

                with urlopen(
                    req,
                    timeout=60
                ) as response:

                    data = response.read()

                    content_type = (
                        response.headers.get(
                            "Content-Type"
                        )
                        or mimetypes.guess_type(
                            file_path
                        )[0]
                        or "image/jpeg"
                    )


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

            except Exception as e:

                print(
                    "图片读取失败:",
                    e
                )

                self.send_error(
                    404,
                    "image unavailable"
                )

            return


        # -------------------------
        # ZIP任务状态
        # -------------------------

        if path == "/api/zip/status":

            job_id = params.get(
                "id",
                [""]
            )[0]

            with zip_jobs_lock:

                job = zip_jobs.get(
                    job_id
                )

            if not job:

                conn = get_conn()

                row = conn.execute(
                    """
                    SELECT *
                    FROM zip_jobs
                    WHERE id = ?
                    """,
                    (job_id,)
                ).fetchone()

                conn.close()

                if row:

                    job = dict(row)


            if not job:

                self.send_json(
                    {
                        "ok": False,
                        "error":
                            "任务不存在"
                    },
                    404
                )

                return


            self.send_json(
                {
                    "ok": True,
                    **dict(job)
                }
            )

            return


        # -------------------------
        # 首页
        # -------------------------

        if path in (
            "/",
            "/index.html"
        ):

            file_path = (
                WEB_DIR /
                "index.html"
            )

            if not file_path.exists():

                self.send_error(
                    404,
                    "index.html not found"
                )

                return


            data = file_path.read_bytes()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(data))
            )

            self.send_header(
                "Cache-Control",
                "no-cache"
            )

            self.end_headers()

            self.wfile.write(data)

            return


        self.send_error(
            404,
            "Not Found"
        )


# =========================================================
# 启动 HTTP
# =========================================================

def start_web_server():

    global http_server

    http_server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        WebHandler
    )

    print(
        f"Web服务器启动：http://0.0.0.0:{PORT}"
    )

    thread = threading.Thread(
        target=http_server.serve_forever,
        daemon=True
    )

    thread.start()


# =========================================================
# Telegram Bot
# =========================================================

def is_admin(user_id):

    return user_id in ADMINS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "🛍️ 打开商品目录",
                web_app=None
            )
        ]
    ]

    # Telegram WebApp按钮需要 WebAppInfo
    from telegram import WebAppInfo

    keyboard = [
        [
            InlineKeyboardButton(
                "🛍️ 打开商品目录",
                web_app=WebAppInfo(
                    url=WEB_URL
                )
            )
        ],
        [
            InlineKeyboardButton(
                "📋 商品",
                callback_data="products"
            ),
            InlineKeyboardButton(
                "📞 询价",
                callback_data="help_inquiry"
            )
        ]
    ]

    if is_admin(
        update.effective_user.id
    ):

        keyboard.append(
            [
                InlineKeyboardButton(
                    "⚙️ 管理后台",
                    callback_data="admin"
                )
            ]
        )

    await update.message.reply_text(
        "欢迎使用商品目录\n\n"
        "点击下面按钮打开商品目录：",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


async def admin_command(
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

    count = product_count()

    photo_count = products_with_photos()

    text = (
        "⚙️ 管理后台\n\n"
        f"商品数量：{count}\n"
        f"有图片：{photo_count}\n\n"
        "可用功能："
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "📦 商品列表",
                callback_data="admin_products"
            )
        ],
        [
            InlineKeyboardButton(
                "📥 导入CSV",
                callback_data="admin_csv_help"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 导入ZIP图片",
                callback_data="admin_zip_help"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 刷新",
                callback_data="admin"
            )
        ]
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "products":

        rows = get_products(True)

        if not rows:

            await query.message.reply_text(
                "目前没有商品。"
            )

            return

        lines = [
            "📦 商品列表",
            ""
        ]

        for row in rows[:50]:

            lines.append(
                f"#{row['id']} "
                f"{row['name']} "
                f"[{row['code']}]"
            )

        if len(rows) > 50:

            lines.append(
                f"\n还有 {len(rows)-50} 个商品"
            )

        await query.message.reply_text(
            "\n".join(lines)
        )

        return


    if data == "help_inquiry":

        await query.message.reply_text(
            "打开商品目录后，"
            "进入商品详情即可提交询价。"
        )

        return


    if data == "admin":

        if not is_admin(
            query.from_user.id
        ):
            return

        count = product_count()

        photo_count = products_with_photos()

        keyboard = [
            [
                InlineKeyboardButton(
                    "📦 商品列表",
                    callback_data="admin_products"
                )
            ],
            [
                InlineKeyboardButton(
                    "📥 CSV导入说明",
                    callback_data="admin_csv_help"
                )
            ],
            [
                InlineKeyboardButton(
                    "📦 ZIP图片导入说明",
                    callback_data="admin_zip_help"
                )
            ]
        ]

        await query.message.reply_text(
            "⚙️ 管理后台\n\n"
            f"商品：{count}\n"
            f"图片：{photo_count}",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return


    if data == "admin_products":

        if not is_admin(
            query.from_user.id
        ):
            return

        rows = get_products()

        if not rows:

            await query.message.reply_text(
                "数据库目前没有商品。"
            )

            return

        lines = [
            "📦 商品列表"
        ]

        for row in rows[:100]:

            status = (
                "上架"
                if row["active"]
                else "下架"
            )

            photo = (
                "🖼️"
                if row["photo_id"]
                else "▫️"
            )

            category = CATS.get(
                row["category"],
                row["category"]
            )

            lines.append(
                f"\n#{row['id']} "
                f"{photo} "
                f"{row['name']}\n"
                f"分类：{category}\n"
                f"价格：{row['price']}\n"
                f"库存：{row['stock']}\n"
                f"状态：{status}"
            )

        await query.message.reply_text(
            "\n".join(lines)
        )

        return


    if data == "admin_csv_help":

        if not is_admin(
            query.from_user.id
        ):
            return

        await query.message.reply_text(
            "📥 CSV导入\n\n"
            "把商品CSV直接发送给机器人即可。\n\n"
            "推荐字段：\n"
            "商品名称,编号,系列,价格,库存,描述\n\n"
            "系统会自动识别中文字段。"
        )

        return
