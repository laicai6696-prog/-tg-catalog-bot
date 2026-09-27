import os
import csv
import json
import re
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
    "https://tg-catalog-bot-10.onrender.com"
).strip().rstrip("/")

PORT = int(os.getenv("PORT", "10000"))

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    ""
).strip()

CSV_FILE = BASE_DIR / os.getenv(
    "CSV_FILE",
    "Telegram商品导入表_93个_可直接导入.csv"
)

# 人工客服
SERVICE_USERNAME = "niubanggu"

# 分类
CATEGORIES = {
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


# ============================================================
# 管理员
# ============================================================

def parse_admin_ids():
    raw = os.getenv("ADMIN_IDS", "").strip()

    if not raw:
        return set()

    result = set()

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))
        except ValueError:
            print(
                f"警告：ADMIN_IDS 中的 {item} 不是数字，已忽略"
            )

    return result


ADMIN_IDS = parse_admin_ids()


def is_admin(user_id):
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False


# ============================================================
# 数据库
# ============================================================

USE_POSTGRES = DATABASE_URL.lower().startswith("postgres")


def db_connect():
    if USE_POSTGRES:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        return conn

    conn = sqlite3.connect(
        str(BASE_DIR / "bot.db"),
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn


def placeholder():
    return "%s" if USE_POSTGRES else "?"


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
                    code TEXT NOT NULL,
                    category TEXT NOT NULL,
                    price TEXT DEFAULT '',
                    stock INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    photo_id TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    status TEXT DEFAULT '待处理',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE products
                ADD COLUMN IF NOT EXISTS photo_id TEXT DEFAULT ''
                """
            )

        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT NOT NULL,
                    category TEXT NOT NULL,
                    price TEXT DEFAULT '',
                    stock INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    active INTEGER DEFAULT 1,
                    photo_id TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    status TEXT DEFAULT '待处理',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # 兼容旧数据库
            try:
                cur.execute(
                    "ALTER TABLE products ADD COLUMN photo_id TEXT DEFAULT ''"
                )
            except Exception:
                pass

        conn.commit()

    finally:
        conn.close()


def fetch_all(sql, params=()):
    conn = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

        if USE_POSTGRES:
            columns = [
                desc[0]
                for desc in cur.description
            ]

            return [
                dict(zip(columns, row))
                for row in rows
            ]

        return [dict(row) for row in rows]

    finally:
        conn.close()


def fetch_one(sql, params=()):
    conn = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()

        if not row:
            return None

        if USE_POSTGRES:
            columns = [
                desc[0]
                for desc in cur.description
            ]

            return dict(zip(columns, row))

        return dict(row)

    finally:
        conn.close()


def execute(sql, params=()):
    conn = db_connect()

    try:
        cur = conn.cursor()
        cur.execute(sql, params)

        try:
            last_id = cur.lastrowid
        except Exception:
            last_id = None

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
    photo_id="",
):
    p = placeholder()

    sql = f"""
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
        VALUES ({p},{p},{p},{p},{p},{p},{p},{p})
    """

    return execute(
        sql,
        (
            name,
            code,
            category,
            price,
            stock,
            description,
            active,
            photo_id,
        ),
    )


def get_product(product_id):
    p = placeholder()

    return fetch_one(
        f"""
        SELECT *
        FROM products
        WHERE id = {p}
        """,
        (product_id,),
    )


def get_products(
    active_only=False,
    category="",
    keyword="",
):
    sql = "SELECT * FROM products WHERE 1=1"
    params = []

    p = placeholder()

    if active_only:
        sql += " AND active = 1"

    if category:
        sql += f" AND category = {p}"
        params.append(category)

    if keyword:
        if USE_POSTGRES:
            sql += """
                AND (
                    name ILIKE %s
                    OR code ILIKE %s
                    OR description ILIKE %s
                )
            """

            q = f"%{keyword}%"
            params.extend([q, q, q])

        else:
            sql += """
                AND (
                    name LIKE ?
                    OR code LIKE ?
                    OR description LIKE ?
                )
            """

            q = f"%{keyword}%"
            params.extend([q, q, q])

    sql += " ORDER BY id DESC"

    return fetch_all(sql, tuple(params))


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

    execute(
        f"""
        UPDATE products
        SET
            name = {p},
            code = {p},
            category = {p},
            price = {p},
            stock = {p},
            description = {p}
        WHERE id = {p}
        """,
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
    p = placeholder()

    execute(
        f"""
        UPDATE products
        SET stock = {p}
        WHERE id = {p}
        """,
        (
            stock,
            product_id,
        ),
    )


def set_product_photo(product_id, photo_id):
    p = placeholder()

    execute(
        f"""
        UPDATE products
        SET photo_id = {p}
        WHERE id = {p}
        """,
        (
            photo_id,
            product_id,
        ),
    )


def set_product_active(product_id, active):
    p = placeholder()

    execute(
        f"""
        UPDATE products
        SET active = {p}
        WHERE id = {p}
        """,
        (
            int(active),
            product_id,
        ),
    )


def delete_product(product_id):
    p = placeholder()

    execute(
        f"""
        DELETE FROM products
        WHERE id = {p}
        """,
        (product_id,),
    )


# ============================================================
# 辅助函数
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_category(value):
    value = normalize_text(value)

    if value in CATEGORIES:
        return value

    for key, name in CATEGORIES.items():
        if value == name:
            return key

    return value


def category_name(category):
    return CATEGORIES.get(
        category,
        category or "未分类",
    )


def safe_stock(value):
    value = normalize_text(value)

    if not value:
        return 0

    try:
        return max(0, int(value))
    except Exception:
        return 0


def product_text(product):
    stock = product.get("stock", 0)

    return (
        f"📦 {product.get('name', '')}\n"
        f"编号：{product.get('code', '')}\n"
        f"分类：{category_name(product.get('category', ''))}\n"
        f"价格：{product.get('price', '') or '面议'}\n"
        f"库存：{stock}\n"
        f"状态：{'上架' if product.get('active') else '下架'}\n"
        f"描述：{product.get('description', '') or '无'}"
    )


def product_keyboard(product, admin=False):
    product_id = int(product["id"])

    buttons = [
        [
            InlineKeyboardButton(
                "💬 询价",
                callback_data=f"inquiry:{product_id}",
            )
        ]
    ]

    if admin:
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        "✏️ 编辑商品",
                        callback_data=f"edit:{product_id}",
                    ),
                    InlineKeyboardButton(
                        "📦 修改库存",
                        callback_data=f"stock:{product_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼️ 更换图片",
                        callback_data=f"photo:{product_id}",
                    ),
                    InlineKeyboardButton(
                        "👁️ 上/下架",
                        callback_data=f"toggle:{product_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑️ 删除商品",
                        callback_data=f"delete:{product_id}",
                    )
                ],
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data="admin_products"
                if admin
                else "catalog",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def main_menu(user_id=None):
    buttons = [
        [
            InlineKeyboardButton(
                "🛍️ 商品目录",
                callback_data="catalog",
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 搜索商品",
                callback_data="search",
            ),
            InlineKeyboardButton(
                "💬 询价",
                callback_data="inquiry_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                "🌐 打开商品小程序",
                web_app=WebAppInfo(url=WEB_URL),
            )
        ],
    ]

    if user_id is not None and is_admin(user_id):
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        "⚙️ 管理后台",
                        callback_data="admin_menu",
                    )
                ]
            ]
        )

    return InlineKeyboardMarkup(buttons)


# ============================================================
# CSV 导入
# ============================================================

def import_csv_file(csv_path):
    if not csv_path.exists():
        return {
            "ok": False,
            "message": f"CSV 文件不存在：{csv_path.name}",
        }

    rows = []

    encodings = [
        "utf-8-sig",
        "utf-8",
        "gb18030",
    ]

    last_error = None

    for encoding in encodings:
        try:
            with open(
                csv_path,
                "r",
                encoding=encoding,
                newline="",
            ) as f:
                reader = csv.DictReader(f)

                for row in reader:
                    rows.append(row)

            break

        except Exception as e:
            last_error = e
            rows = []

    if not rows and last_error:
        return {
            "ok": False,
            "message": f"CSV 读取失败：{last_error}",
        }

    if not rows:
        return {
            "ok": False,
            "message": "CSV 没有商品数据",
        }

    imported = 0

    for row in rows:
        name = normalize_text(
            row.get("name")
            or row.get("商品名称")
            or row.get("名称")
        )

        code = normalize_text(
            row.get("code")
            or row.get("编号")
            or row.get("商品编号")
        )

        category = normalize_category(
            row.get("category")
            or row.get("分类")
            or ""
        )

        price = normalize_text(
            row.get("price")
            or row.get("价格")
            or ""
        )

        stock = safe_stock(
            row.get("stock")
            or row.get("库存")
            or 0
        )

        description = normalize_text(
            row.get("description")
            or row.get("描述")
            or ""
        )

        if not name or not code:
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

    return {
        "ok": True,
        "total": len(rows),
        "imported": imported,
    }


def product_count():
    row = fetch_one(
        "SELECT COUNT(*) AS total FROM products"
    )

    return int(row["total"]) if row else 0


def import_csv_if_empty():
    if product_count() > 0:
        return

    if CSV_FILE.exists():
        result = import_csv_file(CSV_FILE)

        print(
            "自动导入 CSV：",
            result,
        )


# ============================================================
# 93 商品 ZIP 导入
# ============================================================

def validate_zip_path(base_dir, member_name):
    target = (base_dir / member_name).resolve()

    try:
        common = os.path.commonpath(
            [
                str(base_dir.resolve()),
                str(target),
            ]
        )
    except Exception:
        return False

    return common == str(base_dir.resolve())


def find_zip_csv(names):
    csv_names = [
        name
        for name in names
        if name.lower().endswith(".csv")
    ]

    if len(csv_names) != 1:
        return None

    return csv_names[0]


def numbered_image_map(names):
    result = {}

    for name in names:
        lower = name.lower()

        if not lower.endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            )
        ):
            continue

        filename = Path(name).name

        match = re.match(
            r"^(\d{2})\.(jpg|jpeg|png|webp)$",
            filename,
            re.IGNORECASE,
        )

        if not match:
            continue

        number = int(match.group(1))

        if 1 <= number <= 93:
            result[number] = name

    return result


async def process_zip_import(
    zip_path,
    chat_id,
    context,
):
    work_dir = Path(
        tempfile.mkdtemp(
            prefix="catalog_zip_"
        )
    )

    try:
        with zipfile.ZipFile(
            zip_path,
            "r"
        ) as z:

            names = z.namelist()

            csv_name = find_zip_csv(names)

            if not csv_name:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ ZIP 导入失败\n\n"
                        "ZIP 内必须且只能有 1 个 CSV 文件。"
                    ),
                )
                return

            image_map = numbered_image_map(names)

            missing = [
                i
                for i in range(1, 94)
                if i not in image_map
            ]

            if missing:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ ZIP 导入失败\n\n"
                        "缺少图片：\n"
                        +
                        "、".join(
                            f"{i:02d}"
                            for i in missing
                        )
                    ),
                )
                return

            if len(image_map) != 93:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ ZIP 导入失败\n\n"
                        "必须正好包含 01～93 共 93 张图片。"
                    ),
                )
                return

            # 安全解压
            for member in names:
                if not validate_zip_path(
                    work_dir,
                    member,
                ):
                    raise RuntimeError(
                        "ZIP 内存在非法路径"
                    )

                z.extract(
                    member,
                    work_dir,
                )

        csv_path = (
            work_dir /
            csv_name
        )

        rows = []

        with open(
            csv_path,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:
            reader = csv.DictReader(f)

            for row in reader:
                rows.append(row)

        if len(rows) != 93:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ ZIP 导入失败\n\n"
                    f"CSV 商品数量：{len(rows)}\n"
                    "要求必须正好 93 个商品。"
                ),
            )
            return

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "✅ ZIP 文件检查通过\n\n"
                "CSV：93 个商品\n"
                "图片：93 张\n\n"
                "开始导入，请稍候……"
            ),
        )

        imported = 0

        for index, row in enumerate(
            rows,
            start=1,
        ):
            code = normalize_text(
                row.get("code")
                or row.get("编号")
                or f"P{index:03d}"
            )

            name = normalize_text(
                row.get("name")
                or row.get("商品名称")
                or row.get("名称")
                or f"商品{index}"
            )

            category = normalize_category(
                row.get("category")
                or row.get("分类")
                or ""
            )

            price = normalize_text(
                row.get("price")
                or row.get("价格")
                or ""
            )

            stock = safe_stock(
                row.get("stock")
                or row.get("库存")
                or 0
            )

            description = normalize_text(
                row.get("description")
                or row.get("描述")
                or ""
            )

            image_number = index

            image_name = image_map[
                image_number
            ]

            image_path = (
                work_dir /
                image_name
            )

            # 先创建商品
            product_id = insert_product(
                name=name,
                code=code,
                category=category,
                price=price,
                stock=stock,
                description=description,
                active=1,
                photo_id="",
            )

            # 如果 PostgreSQL，lastrowid 可能为空
            if not product_id:
                product = fetch_one(
                    "SELECT * FROM products ORDER BY id DESC LIMIT 1"
                )

                if product:
                    product_id = product["id"]

            # 将图片发送给管理员账号，
            # Telegram 返回的 file_id 可以长期保存。
            for admin_id in ADMIN_IDS:
                try:
                    with open(
                        image_path,
                        "rb",
                    ) as photo_file:

                        message = await context.bot.send_photo(
                            chat_id=admin_id,
                            photo=photo_file,
                            caption=(
                                f"商品图片导入：{name}\n"
                                f"编号：{code}\n"
                                f"序号：{image_number:02d}"
                            ),
                        )

                    photo_id = ""

                    if message.photo:
                        photo_id = (
                            message.photo[-1].file_id
                        )

                    if photo_id and product_id:
                        set_product_photo(
                            product_id,
                            photo_id,
                        )

                    # 自动删除管理员聊天中的图片
                    try:
                        await context.bot.delete_message(
                            chat_id=admin_id,
                            message_id=message.message_id,
                        )
                    except Exception:
                        pass

                except Exception:
                    traceback.print_exc()

            imported += 1

            if imported % 10 == 0:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"📥 已导入 {imported}/93"
                    ),
                )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🎉 ZIP 导入完成\n\n"
                f"成功导入：{imported}/93\n"
                "图片已经绑定到对应商品。"
            ),
            reply_markup=main_menu(chat_id),
        )

    except Exception as e:
        traceback.print_exc()

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ ZIP 导入发生错误\n\n"
                f"{e}"
            ),
        )

    finally:
        try:
            import shutil
            shutil.rmtree(
                work_dir,
                ignore_errors=True,
            )
        except Exception:
            pass


# ============================================================
# Telegram /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    user = update.effective_user

    text = (
        "👋 欢迎使用商品目录\n\n"
        "请选择需要的功能："
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(
            user.id if user else None
        ),
    )


# ============================================================
# /cancel
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.clear()

    await update.message.reply_text(
        "操作已取消。",
        reply_markup=main_menu(
            update.effective_user.id
        ),
    )


# ============================================================
# 商品目录
# ============================================================

async def show_catalog(
    update,
    context,
):
    query = update.callback_query

    if query:
        await query.answer()

        message = query.message

        text = "🛍️ 商品目录\n\n请选择分类："

        buttons = []

        row = []

        for code, name in CATEGORIES.items():
            row.append(
                InlineKeyboardButton(
                    name,
                    callback_data=f"cat:{code}",
                )
            )

            if len(row) == 2:
                buttons.append(row)
                row = []

        if row:
            buttons.append(row)

        buttons.append(
            [
                InlineKeyboardButton(
                    "📋 全部商品",
                    callback_data="all_products",
                )
            ]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    "⬅️ 返回主菜单",
                    callback_data="home",
                )
            ]
        )

        await message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
        )

    else:
        await update.message.reply_text(
            "🛍️ 商品目录\n\n请选择分类：",
            reply_markup=main_menu(
                update.effective_user.id
            ),
        )


async def show_category(
    update,
    context,
    category,
):
    query = update.callback_query

    await query.answer()

    products = get_products(
        active_only=True,
        category=category,
    )

    buttons = []

    for product in products:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{product['name']} "
                        f"[{product['code']}]"
                    ),
                    callback_data=(
                        f"product:{product['id']}"
                    ),
                )
            ]
        )

    if not products:
        text = (
            f"📂 {category_name(category)}\n\n"
            "暂无商品。"
        )

    else:
        text = (
            f"📂 {category_name(category)}\n\n"
            f"共 {len(products)} 个商品"
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回分类",
                callback_data="catalog",
            )
        ]
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def show_all_products(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    products = get_products(
        active_only=True
    )

    buttons = []

    for product in products[:100]:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{product['name']} "
                        f"[{product['code']}]"
                    ),
                    callback_data=(
                        f"product:{product['id']}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回分类",
                callback_data="catalog",
            )
        ]
    )

    await query.message.edit_text(
        (
            f"📋 全部商品\n\n"
            f"共 {len(products)} 个商品"
        ),
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


# ============================================================
# 商品详情
# ============================================================

async def show_product(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    product = get_product(product_id)

    if not product:
        await query.message.edit_text(
            "商品不存在或已经删除。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ 返回",
                            callback_data="catalog",
                        )
                    ]
                ]
            ),
        )
        return

    text = product_text(product)

    buttons = [
        [
            InlineKeyboardButton(
                "💬 一键询价",
                callback_data=f"inquiry:{product_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data=(
                    f"cat:{product['category']}"
                ),
            )
        ],
    ]

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


# ============================================================
# 询价
# ============================================================

async def create_inquiry(
    user,
    product,
    message_text="客户点击一键询价",
):
    product_name = product.get(
        "name",
        "",
    )

    code = product.get(
        "code",
        "",
    )

    inquiry_text = (
        f"{message_text}\n\n"
        f"商品：{product_name}\n"
        f"编号：{code}\n"
        f"分类："
        f"{category_name(product.get('category', ''))}\n"
        f"价格："
        f"{product.get('price') or '面议'}\n"
        f"库存："
        f"{product.get('stock', 0)}"
    )

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
        VALUES ({p},{p},{p},{p},{p})
        """,
        (
            user.id,
            user.username or "",
            (
                f"{product_name} "
                f"[{code}]"
            ),
            inquiry_text,
            "待处理",
        ),
    )

    return inquiry_text


async def notify_admins(
    context,
    user,
    product,
    inquiry_text,
):
    username = (
        f"@{user.username}"
        if user.username
        else "无用户名"
    )

    text = (
        "🔔 新询价\n\n"
        f"客户：{user.first_name or ''}\n"
        f"用户名：{username}\n"
        f"用户ID：{user.id}\n\n"
        f"{inquiry_text}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
            )
        except Exception:
            traceback.print_exc()


async def inquiry_from_product(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer(
        "正在记录询价……"
    )

    product = get_product(product_id)

    if not product:
        await query.message.reply_text(
            "商品不存在。",
            reply_markup=main_menu(
                query.from_user.id
            ),
        )
        return

    inquiry_text = await create_inquiry(
        query.from_user,
        product,
    )

    await notify_admins(
        context,
        query.from_user,
        product,
        inquiry_text,
    )

    service_url = (
        f"https://t.me/{SERVICE_USERNAME}"
    )

    await query.message.reply_text(
        (
            "✅ 询价已记录\n\n"
            "正在为你打开人工客服。\n"
            "如没有自动打开，请点击下面按钮。"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "👤 联系人工客服",
                        url=service_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 返回商品目录",
                        callback_data="catalog",
                    )
                ],
            ]
        ),
    )


async def inquiry_menu(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    await query.message.edit_text(
        (
            "💬 商品询价\n\n"
            "请先选择商品："
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🛍️ 商品目录",
                        callback_data="catalog",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👤 直接联系人工客服",
                        url=f"https://t.me/{SERVICE_USERNAME}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 返回主菜单",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# 添加商品
# ============================================================

async def add_product_start(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text(
            "没有管理员权限。"
        )
        return

    context.user_data.clear()

    context.user_data[
        "admin_add_step"
    ] = "text"

    await query.message.reply_text(
        (
            "➕ 添加商品\n\n"
            "请按以下格式发送：\n\n"
            "名称|编号|分类|价格|库存|描述\n\n"
            "例如：\n"
            "示例商品|P001|c1|100|50|商品说明\n\n"
            "分类支持：c1-c12\n"
            "发送 /cancel 取消。"
        )
    )


def parse_product_text(text):
    parts = [
        item.strip()
        for item in text.split("|")
    ]

    if len(parts) < 5:
        return None, (
            "格式错误。\n\n"
            "正确格式：\n"
            "名称|编号|分类|价格|库存|描述"
        )

    name = parts[0]
    code = parts[1]
    category = normalize_category(parts[2])
    price = parts[3]
    stock_text = parts[4]
    description = (
        "|".join(parts[5:]).strip()
        if len(parts) >= 6
        else ""
    )

    if not name:
        return None, "商品名称不能为空。"

    if not code:
        return None, "商品编号不能为空。"

    if category not in CATEGORIES:
        return None, (
            "分类错误。\n"
            "请填写 c1-c12。"
        )

    if not stock_text.isdigit():
        return None, (
            "库存必须是数字，例如：50"
        )

    stock = int(stock_text)

    return {
        "name": name,
        "code": code,
        "category": category,
        "price": price,
        "stock": stock,
        "description": description,
    }, None


# ============================================================
# 编辑商品
# ============================================================

async def admin_edit_start(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        await query.message.reply_text(
            "商品不存在。"
        )
        return

    context.user_data.clear()

    context.user_data[
        "admin_edit_product_id"
    ] = int(product_id)

    context.user_data[
        "admin_edit_step"
    ] = "text"

    text = (
        "✏️ 编辑商品\n\n"
        "当前商品：\n\n"
        f"名称：{product['name']}\n"
        f"编号：{product['code']}\n"
        f"分类：{category_name(product['category'])}\n"
        f"价格：{product['price'] or '面议'}\n"
        f"库存：{product['stock']}\n"
        f"描述：{product['description'] or '无'}\n\n"
        "请重新发送完整商品资料：\n\n"
        "名称|编号|分类|价格|库存|描述\n\n"
        "例如：\n"
        "新商品|P001|c1|100|50|商品说明\n\n"
        "发送 /cancel 取消。"
    )

    await query.message.reply_text(
        text
    )


async def admin_replace_photo_start(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        await query.message.reply_text(
            "商品不存在。"
        )
        return

    context.user_data.clear()

    context.user_data[
        "admin_photo_product_id"
    ] = int(product_id)

    context.user_data[
        "admin_edit_step"
    ] = "photo"

    await query.message.reply_text(
        (
            "🖼️ 更换商品图片\n\n"
            f"商品：{product['name']}\n"
            f"编号：{product['code']}\n\n"
            "请现在发送新的商品图片。\n\n"
            "发送 /cancel 取消。"
        )
    )


async def admin_stock_start(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        await query.message.reply_text(
            "商品不存在。"
        )
        return

    context.user_data.clear()

    context.user_data[
        "admin_stock_product_id"
    ] = int(product_id)

    context.user_data[
        "admin_edit_step"
    ] = "stock"

    await query.message.reply_text(
        (
            "📦 修改库存\n\n"
            f"商品：{product['name']}\n"
            f"编号：{product['code']}\n"
            f"当前库存：{product['stock']}\n\n"
            "请输入新的库存数字。\n"
            "例如：50\n\n"
            "发送 /cancel 取消。"
        )
    )


# ============================================================
# 商品编辑处理
# ============================================================

async def handle_admin_text(
    update,
    context,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return False

    step = context.user_data.get(
        "admin_edit_step"
    )

    # ----------------------------
    # 修改库存
    # ----------------------------
    if step == "stock":
        product_id = context.user_data.get(
            "admin_stock_product_id"
        )

        if not product_id:
            context.user_data.clear()
            return True

        product = get_product(product_id)

        if not product:
            context.user_data.clear()

            await update.message.reply_text(
                "商品不存在。",
                reply_markup=main_menu(
                    user.id
                ),
            )

            return True

        value = normalize_text(
            update.message.text
        )

        if not value.isdigit():
            await update.message.reply_text(
                "❌ 库存必须是数字。\n\n例如：50"
            )
            return True

        stock = int(value)

        set_product_stock(
            product_id,
            stock,
        )

        context.user_data.clear()

        await update.message.reply_text(
            (
                "✅ 库存修改成功\n\n"
                f"商品：{product['name']}\n"
                f"新库存：{stock}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ 继续管理商品",
                            callback_data="admin_products",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ],
                ]
            ),
        )

        return True

    # ----------------------------
    # 编辑商品资料
    # ----------------------------
    if step == "text":
        product_id = context.user_data.get(
            "admin_edit_product_id"
        )

        if not product_id:
            context.user_data.clear()
            return True

        data, error = parse_product_text(
            update.message.text
        )

        if error:
            await update.message.reply_text(
                "❌ " + error
            )
            return True

        update_product(
            product_id=product_id,
            name=data["name"],
            code=data["code"],
            category=data["category"],
            price=data["price"],
            stock=data["stock"],
            description=data["description"],
        )

        product = get_product(product_id)

        context.user_data.clear()

        await update.message.reply_text(
            (
                "✅ 商品资料修改成功\n\n"
                + product_text(product)
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🖼️ 更换图片",
                            callback_data=f"photo:{product_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⚙️ 继续管理商品",
                            callback_data="admin_products",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ],
                ]
            ),
        )

        return True

    return False


# ============================================================
# 图片处理
# ============================================================

async def handle_photo(
    update,
    context,
):
    user = update.effective_user

    if not user:
        return

    if not is_admin(user.id):
        return

    step = context.user_data.get(
        "admin_edit_step"
    )

    # ----------------------------
    # 添加商品图片
    # ----------------------------
    if context.user_data.get(
        "admin_add_step"
    ) == "photo":

        product_id = context.user_data.get(
            "admin_new_product_id"
        )

        if not product_id:
            context.user_data.clear()
            return

        photo = update.message.photo[-1]

        set_product_photo(
            product_id,
            photo.file_id,
        )

        # 自动删除管理员发送的图片
        try:
            await update.message.delete()
        except Exception:
            pass

        product = get_product(product_id)

        context.user_data.clear()

        await update.effective_chat.send_message(
            (
                "✅ 商品添加完成\n\n"
                + product_text(product)
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ 商品管理",
                            callback_data="admin_products",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ],
                ]
            ),
        )

        return

    # ----------------------------
    # 更换商品图片
    # ----------------------------
    if step == "photo":
        product_id = context.user_data.get(
            "admin_photo_product_id"
        )

        if not product_id:
            context.user_data.clear()
            return

        product = get_product(product_id)

        if not product:
            context.user_data.clear()

            try:
                await update.message.delete()
            except Exception:
                pass

            await update.effective_chat.send_message(
                "商品不存在。"
            )

            return

        photo = update.message.photo[-1]

        set_product_photo(
            product_id,
            photo.file_id,
        )

        # 自动删除管理员刚发的图片
        try:
            await update.message.delete()
        except Exception:
            pass

        context.user_data.clear()

        await update.effective_chat.send_message(
            (
                "✅ 商品图片更换成功\n\n"
                f"商品：{product['name']}\n"
                f"编号：{product['code']}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ 商品管理",
                            callback_data="admin_products",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ],
                ]
            ),
        )

        return


# ============================================================
# 添加商品文本
# ============================================================

async def handle_add_product_text(
    update,
    context,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return False

    if context.user_data.get(
        "admin_add_step"
    ) != "text":
        return False

    data, error = parse_product_text(
        update.message.text
    )

    if error:
        await update.message.reply_text(
            "❌ " + error
        )
        return True

    product_id = insert_product(
        name=data["name"],
        code=data["code"],
        category=data["category"],
        price=data["price"],
        stock=data["stock"],
        description=data["description"],
        active=1,
        photo_id="",
    )

    if not product_id:
        product = fetch_one(
            "SELECT * FROM products ORDER BY id DESC LIMIT 1"
        )

        product_id = (
            product["id"]
            if product
            else None
        )

    context.user_data[
        "admin_new_product_id"
    ] = product_id

    context.user_data[
        "admin_add_step"
    ] = "photo"

    await update.message.reply_text(
        (
            "✅ 商品资料已保存\n\n"
            "现在请发送商品图片。\n\n"
            "如果不需要图片，请发送：\n"
            "跳过"
        )
    )

    return True


# ============================================================
# 跳过商品图片
# ============================================================

async def handle_skip_photo(
    update,
    context,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return False

    if context.user_data.get(
        "admin_add_step"
    ) == "photo":

        product_id = context.user_data.get(
            "admin_new_product_id"
        )

        product = get_product(product_id)

        context.user_data.clear()

        await update.message.reply_text(
            (
                "✅ 商品添加完成\n\n"
                + product_text(product)
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ 商品管理",
                            callback_data="admin_products",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ],
                ]
            ),
        )

        return True

    return False


# ============================================================
# 文本总处理
# ============================================================

async def handle_text(
    update,
    context,
):
    if not update.message:
        return

    text = normalize_text(
        update.message.text
    )

    if text == "/cancel":
        await cancel(
            update,
            context,
        )
        return

    if text == "跳过":
        if await handle_skip_photo(
            update,
            context,
        ):
            return

    if await handle_admin_text(
        update,
        context,
    ):
        return

    if await handle_add_product_text(
        update,
        context,
    ):
        return

    # 普通文本
    await update.message.reply_text(
        "请选择功能：",
        reply_markup=main_menu(
            update.effective_user.id
        ),
    )


# ============================================================
# 管理后台
# ============================================================

async def admin_command(
    update,
    context,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "没有管理员权限。"
        )
        return

    await send_admin_menu(
        update.effective_chat.id,
        context,
    )


async def send_admin_menu(
    chat_id,
    context,
    message_id=None,
):
    total_row = fetch_one(
        "SELECT COUNT(*) AS total FROM products"
    )

    active_row = fetch_one(
        "SELECT COUNT(*) AS total FROM products WHERE active = 1"
    )

    pending_row = fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM inquiries
        WHERE status = '待处理'
        """
    )

    total = (
        int(total_row["total"])
        if total_row
        else 0
    )

    active = (
        int(active_row["total"])
        if active_row
        else 0
    )

    pending = (
        int(pending_row["total"])
        if pending_row
        else 0
    )

    text = (
        "⚙️ 管理后台\n\n"
        f"商品：{total}\n"
        f"上架：{active}\n"
        f"待处理询价：{pending}\n\n"
        "请选择管理功能："
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📦 商品管理",
                    callback_data="admin_products",
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ 添加商品",
                    callback_data="admin_add",
                ),
                InlineKeyboardButton(
                    "📥 CSV导入",
                    callback_data="admin_csv",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📦 93商品ZIP导入",
                    callback_data="admin_zip",
                )
            ],
            [
                InlineKeyboardButton(
                    "📩 询价记录",
                    callback_data="admin_inquiries",
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 系统状态",
                    callback_data="admin_status",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 主菜单",
                    callback_data="home",
                )
            ],
        ]
    )

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
    )


# ============================================================
# 商品管理列表
# ============================================================

async def admin_list(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    products = get_products()

    buttons = []

    for product in products[:100]:
        status = (
            "🟢"
            if product["active"]
            else "⚪"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{status} "
                        f"{product['name']} "
                        f"[{product['code']}]"
                    ),
                    callback_data=(
                        f"admin_product:{product['id']}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "➕ 添加商品",
                callback_data="admin_add",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ 管理后台",
                callback_data="admin_menu",
            )
        ]
    )

    await query.message.edit_text(
        (
            "📦 商品管理\n\n"
            f"共 {len(products)} 个商品\n\n"
            "点击商品进行编辑："
        ),
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def admin_product_detail(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        await query.message.edit_text(
            "商品不存在。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ 商品管理",
                            callback_data="admin_products",
                        )
                    ]
                ]
            ),
        )
        return

    text = (
        "⚙️ 商品管理\n\n"
        + product_text(product)
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ 编辑商品",
                    callback_data=f"edit:{product_id}",
                ),
                InlineKeyboardButton(
                    "📦 修改库存",
                    callback_data=f"stock:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼️ 更换图片",
                    callback_data=f"photo:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "🔴 下架商品"
                        if product["active"]
                        else "🟢 上架商品"
                    ),
                    callback_data=f"toggle:{product_id}",
                ),
                InlineKeyboardButton(
                    "🗑️ 删除商品",
                    callback_data=f"delete:{product_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 商品管理",
                    callback_data="admin_products",
                )
            ],
        ]
    )

    await query.message.edit_text(
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
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        return

    new_active = (
        0
        if product["active"]
        else 1
    )

    set_product_active(
        product_id,
        new_active,
    )

    product = get_product(product_id)

    await admin_product_detail(
        update,
        context,
        product_id,
    )


# ============================================================
# 删除商品
# ============================================================

async def admin_delete_confirm(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        return

    await query.message.edit_text(
        (
            "⚠️ 确定删除商品吗？\n\n"
            f"商品：{product['name']}\n"
            f"编号：{product['code']}\n\n"
            "删除后无法恢复。"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ 确定删除",
                        callback_data=(
                            f"delete_yes:{product_id}"
                        ),
                    ),
                    InlineKeyboardButton(
                        "取消",
                        callback_data=(
                            f"admin_product:{product_id}"
                        ),
                    ),
                ]
            ]
        ),
    )


async def admin_delete_product(
    update,
    context,
    product_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    product = get_product(product_id)

    if not product:
        return

    delete_product(product_id)

    await query.message.edit_text(
        (
            "✅ 商品已删除\n\n"
            f"{product['name']} [{product['code']}]"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📦 商品管理",
                        callback_data="admin_products",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 管理后台",
                        callback_data="admin_menu",
                    )
                ],
            ]
        ),
    )


# ============================================================
# 系统状态
# ============================================================

async def admin_status(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    total = product_count()

    active_row = fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM products
        WHERE active = 1
        """
    )

    photos_row = fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM products
        WHERE photo_id IS NOT NULL
        AND photo_id != ''
        """
    )

    active = (
        int(active_row["total"])
        if active_row
        else 0
    )

    photos = (
        int(photos_row["total"])
        if photos_row
        else 0
    )

    db_type = (
        "PostgreSQL"
        if USE_POSTGRES
        else "SQLite"
    )

    text = (
        "📊 系统状态\n\n"
        f"数据库：{db_type}\n"
        f"商品总数：{total}\n"
        f"上架商品：{active}\n"
        f"已有图片：{photos}\n"
        f"CSV：{CSV_FILE.name}\n"
        f"CSV存在：{'是' if CSV_FILE.exists() else '否'}\n"
        f"网页：{WEB_URL}"
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 刷新",
                        callback_data="admin_status",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 管理后台",
                        callback_data="admin_menu",
                    )
                ],
            ]
        ),
    )


# ============================================================
# CSV 导入
# ============================================================

async def admin_csv(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    if not CSV_FILE.exists():
        await query.message.edit_text(
            (
                "❌ 找不到 CSV 文件。\n\n"
                f"文件名：{CSV_FILE.name}\n\n"
                "请把 CSV 放到 bot.py 同级目录，"
                "或者使用下面的上传方式。"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ 管理后台",
                            callback_data="admin_menu",
                        )
                    ]
                ]
            ),
        )
        return

    result = import_csv_file(
        CSV_FILE
    )

    if result.get("ok"):
        text = (
            "✅ CSV 导入完成\n\n"
            f"CSV 行数：{result.get('total', 0)}\n"
            f"成功导入：{result.get('imported', 0)}"
        )
    else:
        text = (
            "❌ CSV 导入失败\n\n"
            f"{result.get('message', '')}"
        )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📦 商品管理",
                        callback_data="admin_products",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 管理后台",
                        callback_data="admin_menu",
                    )
                ],
            ]
        ),
    )


# ============================================================
# ZIP 导入入口
# ============================================================

async def admin_zip(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    context.user_data.clear()

    context.user_data[
        "admin_zip_upload"
    ] = True

    await query.message.reply_text(
        (
            "📦 93 商品 ZIP 导入\n\n"
            "请发送 ZIP 文件。\n\n"
            "ZIP 必须包含：\n"
            "① 1 个 CSV\n"
            "② 01.jpg ～ 93.jpg\n"
            "③ 一共 93 个商品\n"
            "④ 一共 93 张图片\n\n"
            "图片映射：\n"
            "01 → 第1个商品\n"
            "02 → 第2个商品\n"
            "……\n"
            "93 → 第93个商品\n\n"
            "发送 /cancel 取消。"
        )
    )


async def handle_document(
    update,
    context,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    document = update.message.document

    if not document:
        return

    filename = document.file_name or ""

    # ZIP
    if context.user_data.get(
        "admin_zip_upload"
    ):
        if not filename.lower().endswith(
            ".zip"
        ):
            await update.message.reply_text(
                "❌ 请发送 ZIP 文件。"
            )
            return

        target = (
            UPLOAD_DIR /
            "catalog_import.zip"
        )

        tg_file = await document.get_file()

        await tg_file.download_to_drive(
            custom_path=str(target)
        )

        try:
            await update.message.delete()
        except Exception:
            pass

        context.user_data.clear()

        await process_zip_import(
            target,
            update.effective_chat.id,
            context,
        )

        try:
            target.unlink()
        except Exception:
            pass

        return

    # CSV 上传
    if filename.lower().endswith(
        ".csv"
    ):
        target = (
            UPLOAD_DIR /
            "uploaded_catalog.csv"
        )

        tg_file = await document.get_file()

        await tg_file.download_to_drive(
            custom_path=str(target)
        )

        try:
            await update.message.delete()
        except Exception:
            pass

        result = import_csv_file(
            target
        )

        try:
            target.unlink()
        except Exception:
            pass

        if result.get("ok"):
            await update.effective_chat.send_message(
                (
                    "✅ CSV 导入完成\n\n"
                    f"CSV 商品：{result.get('total', 0)}\n"
                    f"成功导入：{result.get('imported', 0)}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📦 商品管理",
                                callback_data="admin_products",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ 管理后台",
                                callback_data="admin_menu",
                            )
                        ],
                    ]
                ),
            )

        else:
            await update.effective_chat.send_message(
                (
                    "❌ CSV 导入失败\n\n"
                    f"{result.get('message', '')}"
                )
            )

        return

    await update.message.reply_text(
        "❌ 只支持 CSV 或 ZIP 文件。"
    )


# ============================================================
# 询价记录后台
# ============================================================

async def admin_inquiries(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    rows = fetch_all(
        """
        SELECT *
        FROM inquiries
        ORDER BY id DESC
        LIMIT 50
        """
    )

    buttons = []

    for row in rows:
        status = row.get(
            "status",
            "待处理",
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{'🟡' if status == '待处理' else '🟢'} "
                        f"#{row['id']} "
                        f"{row.get('product', '')[:25]}"
                    ),
                    callback_data=(
                        f"inquiry_detail:{row['id']}"
                    ),
                )
            ]
        )

    if not rows:
        text = "📩 暂无询价记录。"
    else:
        text = (
            "📩 询价记录\n\n"
            f"最近 {len(rows)} 条"
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ 管理后台",
                callback_data="admin_menu",
            )
        ]
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )


async def admin_inquiry_detail(
    update,
    context,
    inquiry_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    p = placeholder()

    row = fetch_one(
        f"""
        SELECT *
        FROM inquiries
        WHERE id = {p}
        """,
        (inquiry_id,),
    )

    if not row:
        return

    username = row.get(
        "username",
        ""
    )

    text = (
        "📩 询价详情\n\n"
        f"编号：#{row['id']}\n"
        f"客户：@{username if username else '无'}\n"
        f"用户ID：{row.get('user_id', '')}\n"
        f"商品：{row.get('product', '')}\n"
        f"状态：{row.get('status', '')}\n"
        f"时间：{row.get('created_at', '')}\n\n"
        f"{row.get('message', '')}"
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🟢 标记已处理",
                        callback_data=(
                            f"inquiry_done:{inquiry_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ 询价列表",
                        callback_data="admin_inquiries",
                    )
                ],
            ]
        ),
    )


async def admin_mark_inquiry(
    update,
    context,
    inquiry_id,
):
    query = update.callback_query

    await query.answer(
        "已标记"
    )

    if not is_admin(query.from_user.id):
        return

    p = placeholder()

    execute(
        f"""
        UPDATE inquiries
        SET status = '已处理'
        WHERE id = {p}
        """,
        (inquiry_id,),
    )

    await admin_inquiry_detail(
        update,
        context,
        inquiry_id,
    )


# ============================================================
# 按钮总处理
# ============================================================

async def button_handler(
    update,
    context,
):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    try:
        # ----------------------------
        # 首页
        # ----------------------------
        if data == "home":
            await query.answer()

            context.user_data.clear()

            await query.message.edit_text(
                (
                    "👋 商品目录\n\n"
                    "请选择功能："
                ),
                reply_markup=main_menu(
                    query.from_user.id
                ),
            )

            return

        # ----------------------------
        # 商品目录
        # ----------------------------
        if data == "catalog":
            await show_catalog(
                update,
                context,
            )
            return

        if data.startswith("cat:"):
            category = data.split(
                ":",
                1
            )[1]

            await show_category(
                update,
                context,
                category,
            )

            return

        if data == "all_products":
            await show_all_products(
                update,
                context,
            )
            return

        if data.startswith("product:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await show_product(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 询价
        # ----------------------------
        if data == "inquiry_menu":
            await inquiry_menu(
                update,
                context,
            )
            return

        if data.startswith("inquiry:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await inquiry_from_product(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 管理后台
        # ----------------------------
        if data == "admin_menu":
            await query.answer()

            if not is_admin(
                query.from_user.id
            ):
                return

            await send_admin_menu(
                query.message.chat_id,
                context,
                query.message.message_id,
            )

            return

        if data == "admin_products":
            await admin_list(
                update,
                context,
            )
            return

        if data.startswith("admin_product:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_product_detail(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 添加商品
        # ----------------------------
        if data == "admin_add":
            await add_product_start(
                update,
                context,
            )
            return

        # ----------------------------
        # 编辑商品
        # ----------------------------
        if data.startswith("edit:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_edit_start(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 修改库存
        # ----------------------------
        if data.startswith("stock:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_stock_start(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 更换图片
        # ----------------------------
        if data.startswith("photo:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_replace_photo_start(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 上下架
        # ----------------------------
        if data.startswith("toggle:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_toggle_product(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # 删除确认
        # ----------------------------
        if data.startswith("delete:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_delete_confirm(
                update,
                context,
                product_id,
            )

            return

        if data.startswith("delete_yes:"):
            product_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_delete_product(
                update,
                context,
                product_id,
            )

            return

        # ----------------------------
        # CSV
        # ----------------------------
        if data == "admin_csv":
            await admin_csv(
                update,
                context,
            )
            return

        # ----------------------------
        # ZIP
        # ----------------------------
        if data == "admin_zip":
            await admin_zip(
                update,
                context,
            )
            return

        # ----------------------------
        # 系统状态
        # ----------------------------
        if data == "admin_status":
            await admin_status(
                update,
                context,
            )
            return

        # ----------------------------
        # 询价记录
        # ----------------------------
        if data == "admin_inquiries":
            await admin_inquiries(
                update,
                context,
            )
            return

        if data.startswith(
            "inquiry_detail:"
        ):
            inquiry_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_inquiry_detail(
                update,
                context,
                inquiry_id,
            )

            return

        if data.startswith(
            "inquiry_done:"
        ):
            inquiry_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )

            await admin_mark_inquiry(
                update,
                context,
                inquiry_id,
            )

            return

        # ----------------------------
        # 搜索
        # ----------------------------
        if data == "search":
            await query.answer()

            await query.message.reply_text(
                (
                    "🔎 搜索商品\n\n"
                    "目前网页商品目录支持直接搜索。\n"
                    "点击下面按钮打开商品小程序："
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🌐 打开商品小程序",
                                web_app=WebAppInfo(
                                    url=WEB_URL
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ 返回",
                                callback_data="home",
                            )
                        ],
                    ]
                ),
            )

            return

        await query.answer()

    except Exception as e:
        traceback.print_exc()

        try:
            await query.answer(
                "操作失败，请重试。",
                show_alert=True,
            )
        except Exception:
            pass


# ============================================================
# Web API
# ============================================================

def json_response(handler, data, status=200):
    raw = json.dumps(
        data,
        ensure_ascii=False,
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8",
    )

    handler.send_header(
        "Content-Length",
        str(len(raw)),
    )

    handler.send_header(
        "Cache-Control",
        "no-store",
    )

    handler.end_headers()

    handler.wfile.write(raw)


def read_json(handler):
    length = int(
        handler.headers.get(
            "Content-Length",
            "0"
        )
    )

    if length <= 0:
        return {}

    raw = handler.rfile.read(
        length
    )

    return json.loads(
        raw.decode("utf-8")
    )


class WebHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args,
    ):
        return

    def do_GET(self):
        try:
            parsed = urlparse(
                self.path
            )

            path = parsed.path

            query = parse_qs(
                parsed.query
            )

            # 首页
            if path == "/":
                file_path = (
                    WEB_DIR /
                    "index.html"
                )

                if not file_path.exists():
                    self.send_error(
                        404,
                        "index.html not found",
                    )
                    return

                data = file_path.read_bytes()

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )

                self.send_header(
                    "Content-Length",
                    str(len(data)),
                )

                self.end_headers()

                self.wfile.write(data)

                return

            # 状态
            if path == "/api/status":
                total = product_count()

                active_row = fetch_one(
                    """
                    SELECT COUNT(*) AS total
                    FROM products
                    WHERE active = 1
                    """
                )

                photo_row = fetch_one(
                    """
                    SELECT COUNT(*) AS total
                    FROM products
                    WHERE photo_id IS NOT NULL
                    AND photo_id != ''
                    """
                )

                json_response(
                    self,
                    {
                        "ok": True,
                        "database": (
                            "postgresql"
                            if USE_POSTGRES
                            else "sqlite"
                        ),
                        "total_products": total,
                        "active_products": (
                            int(
                                active_row["total"]
                            )
                            if active_row
                            else 0
                        ),
                        "products_with_photos": (
                            int(
                                photo_row["total"]
                            )
                            if photo_row
                            else 0
                        ),
                        "csv_file": CSV_FILE.name,
                        "csv_exists": CSV_FILE.exists(),
                        "web_url": WEB_URL,
                    },
                )

                return

            # 分类
            if path == "/api/categories":
                json_response(
                    self,
                    {
                        "ok": True,
                        "categories": [
                            {
                                "id": key,
                                "name": name,
                            }
                            for key, name
                            in CATEGORIES.items()
                        ],
                    },
                )

                return

            # 商品
            if path == "/api/products":
                active_value = (
                    query.get(
                        "active",
                        ["1"],
                    )[0]
                )

                category = (
                    query.get(
                        "category",
                        [""],
                    )[0]
                )

                keyword = (
                    query.get(
                        "keyword",
                        [""],
                    )[0]
                )

                products = get_products(
                    active_only=(
                        active_value == "1"
                    ),
                    category=category,
                    keyword=keyword,
                )

                result = []

                for product in products:
                    item = dict(product)

                    if item.get(
                        "created_at"
                    ):
                        item["created_at"] = str(
                            item["created_at"]
                        )

                    result.append(item)

                json_response(
                    self,
                    {
                        "ok": True,
                        "products": result,
                    },
                )

                return

            # Telegram 商品图片
            if path == "/api/photo":
                ids = query.get(
                    "id",
                    []
                )

                if not ids:
                    self.send_error(
                        400,
                        "missing id",
                    )
                    return

                product_id = ids[0]

                product = get_product(
                    product_id
                )

                if not product:
                    self.send_error(
                        404,
                        "product not found",
                    )
                    return

                photo_id = product.get(
                    "photo_id"
                )

                if not photo_id:
                    self.send_error(
                        404,
                        "photo not found",
                    )
                    return

                url = (
                    "https://api.telegram.org/"
                    f"bot{BOT_TOKEN}/getFile"
                    f"?file_id={photo_id}"
                )

                req = Request(
                    url,
                    headers={
                        "User-Agent":
                            "Mozilla/5.0"
                    },
                )

                with urlopen(
                    req,
                    timeout=20,
                ) as response:
                    data = json.loads(
                        response.read()
                    )

                if not data.get("ok"):
                    self.send_error(
                        404,
                        "telegram file not found",
                    )
                    return

                file_path = (
                    data["result"]
                    .get("file_path")
                )

                if not file_path:
                    self.send_error(
                        404,
                        "file path missing",
                    )
                    return

                file_url = (
                    "https://api.telegram.org/"
                    f"file/bot{BOT_TOKEN}/"
                    f"{file_path}"
                )

                req2 = Request(
                    file_url,
                    headers={
                        "User-Agent":
                            "Mozilla/5.0"
                    },
                )

                with urlopen(
                    req2,
                    timeout=30,
                ) as response:
                    image_data = response.read()

                    content_type = (
                        response.headers.get(
                            "Content-Type",
                            "image/jpeg",
                        )
                    )

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    content_type,
                )

                self.send_header(
                    "Cache-Control",
                    "public, max-age=300",
                )

                self.send_header(
                    "Content-Length",
                    str(len(image_data)),
                )

                self.end_headers()

                self.wfile.write(
                    image_data
                )

                return

            # favicon
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            # web 静态文件
            if path.startswith("/web/"):
                relative = path[
                    len("/web/"):
                ]

                file_path = (
                    WEB_DIR /
                    relative
                ).resolve()

                if not str(
                    file_path
                ).startswith(
                    str(
                        WEB_DIR.resolve()
                    )
                ):
                    self.send_error(
                        403
                    )
                    return

                if not file_path.exists():
                    self.send_error(
                        404
                    )
                    return

                data = file_path.read_bytes()

                content_type = (
                    mimetypes.guess_type(
                        str(file_path)
                    )[0]
                    or "application/octet-stream"
                )

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    content_type,
                )

                self.send_header(
                    "Content-Length",
                    str(len(data)),
                )

                self.end_headers()

                self.wfile.write(data)

                return

            self.send_error(
                404,
                "Not Found",
            )

        except Exception:
            traceback.print_exc()

            try:
                self.send_error(
                    500,
                    "Internal Server Error",
                )
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(
                self.path
            )

            if parsed.path != "/api/inquiry":
                self.send_error(
                    404,
                    "Not Found",
                )
                return

            data = read_json(
                self
            )

            user_id = data.get(
                "user_id"
            )

            username = normalize_text(
                data.get(
                    "username",
                    ""
                )
            )

            product = normalize_text(
                data.get(
                    "product",
                    ""
                )
            )

            message = normalize_text(
                data.get(
                    "message",
                    ""
                )
            )

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
                VALUES ({p},{p},{p},{p},{p})
                """,
                (
                    user_id,
                    username,
                    product,
                    message,
                    "待处理",
                ),
            )

            # Web 请求无法直接使用 Telegram Application，
            # 所以异步通知由后台线程处理。
            threading.Thread(
                target=notify_admins_sync,
                args=(
                    user_id,
                    username,
                    product,
                    message,
                ),
                daemon=True,
            ).start()

            json_response(
                self,
                {
                    "ok": True,
                    "message": "询价已记录",
                },
            )

        except Exception as e:
            traceback.print_exc()

            json_response(
                self,
                {
                    "ok": False,
                    "message": str(e),
                },
                status=500,
            )


# ============================================================
# Web 询价通知管理员
# ============================================================

def notify_admins_sync(
    user_id,
    username,
    product,
    message,
):
    """
    Web Mini App 询价通知。

    这里使用 Telegram Bot API，
    不依赖 Application 的 asyncio loop。
    """

    text = (
        "🔔 Web商品目录新询价\n\n"
        f"客户用户名："
        f"@{username if username else '无'}\n"
        f"用户ID：{user_id or '未知'}\n\n"
        f"商品：{product}\n\n"
        f"{message}"
    )

    for admin_id in ADMIN_IDS:
        try:
            url = (
                "https://api.telegram.org/"
                f"bot{BOT_TOKEN}/sendMessage"
            )

            payload = json.dumps(
                {
                    "chat_id": admin_id,
                    "text": text,
                },
                ensure_ascii=False,
            ).encode("utf-8")

            request = Request(
                url,
                data=payload,
                headers={
                    "Content-Type":
                        "application/json"
                },
                method="POST",
            )

            urlopen(
                request,
                timeout=15,
            ).read()

        except Exception:
            traceback.print_exc()


# ============================================================
# 启动 Web 服务
# ============================================================

def start_web_server():
    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        WebHandler,
    )

    print(
        f"Web server running on 0.0.0.0:{PORT}"
    )

    server.serve_forever()


# ============================================================
# 错误处理
# ============================================================

async def error_handler(
    update,
    context,
):
    print(
        "Telegram error:",
        context.error,
    )

    traceback.print_exc()


# ============================================================
# 主程序
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "缺少 BOT_TOKEN 环境变量。"
        )

    if not ADMIN_IDS:
        print(
            "警告：ADMIN_IDS 没有设置，"
            "管理员功能将无法使用。"
        )

    init_db()

    import_csv_if_empty()

    # Web 服务
    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True,
    )

    web_thread.start()

    print(
        "================================"
    )

    print(
        "商品目录机器人启动"
    )

    print(
        f"人工客服：@{SERVICE_USERNAME}"
    )

    print(
        f"Web：{WEB_URL}"
    )

    print(
        f"数据库："
        f"{'PostgreSQL' if USE_POSTGRES else 'SQLite'}"
    )

    print(
        "================================"
    )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # 命令
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

    # 按钮
    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    # 图片
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    # 文件
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    # 文本
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text,
        )
    )

    application.add_error_handler(
        error_handler
    )

    # 清除旧 webhook，使用 polling
    async def post_init(app):
        try:
            await app.bot.delete_webhook(
                drop_pending_updates=True
            )

            print(
                "旧 Webhook 已清理"
            )

        except Exception:
            traceback.print_exc()

    application.post_init = post_init

    application.run_polling(
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
