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
CSV_FILE = os.getenv("CSV_FILE", "Telegram商品导入表_93个_可直接导入.csv").strip()
SERVICE_USERNAME = os.getenv("SERVICE_USERNAME", "niubanggu").strip().lstrip("@")
BOT_USERNAME = os.getenv("BOT_USERNAME", "shangpinmulu2026_bot").strip().lstrip("@")

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
    raise RuntimeError("缺少环境变量 BOT_TOKEN")
if not ADMIN_IDS:
    raise RuntimeError("缺少有效的 ADMIN_IDS。请填写 Telegram 数字用户ID，不要填写 @用户名")


# -------------------- 数据库 --------------------

def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def db_connect():
    if USE_POSTGRES:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as exc:
            raise RuntimeError("使用 PostgreSQL 时必须安装 psycopg2-binary") from exc
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
    """按商品编号批量更新库存。支持 UTF-8/UTF-8-SIG/GB18030。

    CSV 至少需要两列：code,stock。
    也兼容完整商品 CSV，只读取 code 和 stock，不修改其他字段。
    全部校验通过后才一次性提交，避免更新到一半失败。
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
        raise ValueError("无法识别 CSV 编码，请保存为 UTF-8 CSV")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")

    fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
    if "code" not in fields or "stock" not in fields:
        raise ValueError("库存 CSV 必须包含两列：code,stock")

    rows = []
    seen = set()
    for line_no, raw in enumerate(reader, start=2):
        code = str(raw.get(fields["code"], "") or "").strip().upper()
        stock_text = str(raw.get(fields["stock"], "") or "").strip()
        # 忽略完全空白行
        if not code and not stock_text:
            continue
        if not code:
            raise ValueError(f"第 {line_no} 行商品编号为空")
        if code in seen:
            raise ValueError(f"第 {line_no} 行商品编号重复：{code}")
        seen.add(code)
        try:
            if not re.fullmatch(r"\d+", stock_text):
                raise ValueError
            stock = int(stock_text)
        except ValueError:
            raise ValueError(f"第 {line_no} 行库存无效：{stock_text!r}，必须是 0 或以上整数")
        rows.append((code, stock))

    if not rows:
        raise ValueError("CSV 没有可更新的库存数据")

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
            raise ValueError("找不到商品编号：" + ", ".join(missing[:20]) + (" 等" if len(missing) > 20 else ""))

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
    """支持 c1-c12 和 CSV 中的中文分类名称。"""
    raw = str(value or "").strip()
    if raw in CATEGORIES:
        return raw
    for code, name in CATEGORIES.items():
        if raw == name or raw == name.replace("系列", ""):
            return code
    old_map = {
        "分类一": "c1", "分类二": "c2", "分类三": "c3", "分类四": "c4",
        "分类五": "c5", "分类六": "c6", "分类七": "c7", "分类八": "c8",
        "分类九": "c9", "分类十": "c10", "分类十一": "c11", "分类十二": "c12",
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
        raise ValueError("CSV 中存在空商品名称")
    if category not in CATEGORIES:
        raise ValueError(f"分类 {category} 无效，应为 c1-c12")
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
# -------------------- /upload 一键修改库存命令 --------------------

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员通过 /upload 进入一键批量修改库存。"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return

    clear_state(context)

    context.user_data["state"] = "bulk_stock_import"

    await update.message.reply_text(
        "📊 一键修改库存\n\n"
        "请发送库存 CSV 文件。\n\n"
        "CSV 格式：\n"
        "code,stock\n"
        "P001,20\n"
        "P002,15\n"
        "P003,30\n\n"
        "说明：\n"
        "• code = 商品编号\n"
        "• stock = 新库存数量\n"
        "• 只修改库存\n"
        "• 不修改商品名称\n"
        "• 不修改价格\n"
        "• 不修改分类\n"
        "• 不修改图片\n"
        "• 不修改上下架状态\n\n"
        "例如：\n"
        "P001,100\n"
        "P002,50\n"
        "P003,0\n\n"
        "商品编号必须已经存在。\n"
        "发送 /cancel 可取消。"
    )

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
        raise ValueError(f"无法读取 CSV：{last_error}")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
    required = ["name", "code", "category", "price", "stock"]
    missing = [x for x in required if x not in fields]
    if missing:
        raise ValueError("CSV 缺少字段：" + ", ".join(missing))
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


# -------------------- Telegram 辅助 --------------------

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def cat_name(code):
    return CATEGORIES.get(code, code or "未分类")


def product_text(product, admin=False):
    status = "🟢 上架" if int(product.get("active", 0) or 0) else "🔴 下架"
    photo = "有图片" if product.get("photo_id") else "无图片"
    text = (
        f"📦 {product.get('name', '')}\n"
        f"编号：{product.get('code') or '-'}\n"
        f"分类：{cat_name(product.get('category'))}\n"
        f"价格：{product.get('price') or '面议'}\n"
        f"库存：{product.get('stock', 0)}\n"
        f"状态：{status}\n"
        f"图片：{photo}\n"
    )
    if product.get("description"):
        text += f"描述：{product['description']}\n"
    if admin:
        text += f"ID：{product.get('id')}"
    return text


def main_menu(user_id):
    rows = [
        [InlineKeyboardButton("📚 商品目录", callback_data="catalog")],
        [InlineKeyboardButton("🔎 搜索商品", callback_data="search")],
        [InlineKeyboardButton("🌐 在线商品目录", web_app=WebAppInfo(url=WEB_URL))],
        [InlineKeyboardButton("💬 客服直达", url=f"https://t.me/{SERVICE_USERNAME}")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("⚙️ 管理后台", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def catalog_keyboard():
    rows = []
    keys = list(CATEGORIES.items())
    for i in range(0, len(keys), 2):
        pair = keys[i:i + 2]
        rows.append([InlineKeyboardButton(v, callback_data=f"cat:{k}") for k, v in pair])
    rows.append([InlineKeyboardButton("📦 全部商品", callback_data="all_products")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def products_keyboard(products, prefix="prod:", back="catalog"):
    rows = []
    for p in products:
        label = f"{p['name']} [{p.get('code') or '-'}]"
        if len(label) > 55:
            label = label[:52] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"{prefix}{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 商品管理", callback_data="admin_products:0"), InlineKeyboardButton("➕ 添加商品", callback_data="admin_add")],
        [InlineKeyboardButton("📨 询价记录", callback_data="admin_inquiries:0"), InlineKeyboardButton("📊 系统状态", callback_data="admin_status")],
        [InlineKeyboardButton("📦 一键上传商品+图片", callback_data="admin_zip"), InlineKeyboardButton("📥 只导入CSV", callback_data="admin_csv")],
        [InlineKeyboardButton("📊 一键批量改库存", callback_data="bulk_stock" )],
        [InlineKeyboardButton("🟢 全部上架", callback_data="bulk_on"), InlineKeyboardButton("🔴 全部下架", callback_data="bulk_off")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="home")],
    ])


def product_share_url(product):
    pid = int(product["id"])
    share_url = f"https://t.me/{BOT_USERNAME}?start=product_{pid}"
    share_text = f"📦 {product.get('name', '')}\n编号：{product.get('code') or '-'}\n点击查看商品详情"
    return (
        "https://t.me/share/url?url="
        + quote(share_url, safe="")
        + "&text="
        + quote(share_text, safe="")
    )


def admin_product_keyboard(product):
    pid = product["id"]
    active = int(product.get("active", 0) or 0)
    toggle_text = "🔴 下架" if active else "🟢 上架"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 编辑商品", callback_data=f"edit:{pid}"), InlineKeyboardButton("📦 修改库存", callback_data=f"stock:{pid}")],
        [InlineKeyboardButton("🖼️ 更换图片", callback_data=f"photo:{pid}"), InlineKeyboardButton(toggle_text, callback_data=f"toggle:{pid}")],
        [InlineKeyboardButton("📤 分享商品", url=product_share_url(product))],
        [InlineKeyboardButton("🗑️ 删除商品", callback_data=f"delete:{pid}")],
        [InlineKeyboardButton("⬅️ 商品管理", callback_data="admin_products:0")],
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
    """发送商品详情；target 可以是 Update 或 Message。"""
    text = product_text(product)
    pid = int(product["id"])
    chat = target.effective_chat if hasattr(target, "effective_chat") else getattr(target, "chat", None)
    if chat is None:
        raise ValueError("无法确定商品发送目标聊天")

    share_link = product_share_url(product)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 我要询价", callback_data=f"inq:{pid}")],
        [InlineKeyboardButton("📤 分享商品", url=share_link)],
        [
            InlineKeyboardButton("⬅️ 返回分类", callback_data=f"cat:{product.get('category')}"),
            InlineKeyboardButton("🏠 首页", callback_data="home"),
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


# -------------------- 通知 --------------------

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


# -------------------- 用户命令 --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)

    # 商品分享深链接：/start product_123
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
                "商品不存在或已下架。",
                reply_markup=main_menu(update.effective_user.id),
            )
            return

    await update.message.reply_text(
        "👋 欢迎使用商品目录\n\n请选择功能：",
        reply_markup=main_menu(update.effective_user.id),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    await update.message.reply_text("已取消当前操作。", reply_markup=main_menu(update.effective_user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("无权限。")
        return
    clear_state(context)
    await update.message.reply_text("⚙️ 管理后台", reply_markup=admin_menu_keyboard())


# -------------------- Callback --------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    if data == "home":
        clear_state(context)
        await safe_edit(query, "👋 欢迎使用商品目录\n\n请选择功能：", main_menu(user_id))
        return

    if data == "catalog":
        clear_state(context)
        await safe_edit(query, "📚 商品分类\n\n请选择分类：", catalog_keyboard())
        return

    if data == "search":
        clear_state(context)
        context.user_data["state"] = "search"
        await safe_edit(query, "🔎 请输入商品名称、编号或关键词：\n\n发送 /cancel 可取消")
        return

    if data.startswith("cat:"):
        category = data.split(":", 1)[1]
        products = fetchall(
            "SELECT * FROM products WHERE category=? AND active=1 ORDER BY id DESC" if not USE_POSTGRES
            else "SELECT * FROM products WHERE category=%s AND active=1 ORDER BY id DESC",
            (category,),
        )
        await safe_edit(query, f"📚 {cat_name(category)}\n\n共 {len(products)} 个商品", products_keyboard(products, "prod:", "catalog"))
        return

    if data == "all_products":
        products = fetchall("SELECT * FROM products WHERE active=1 ORDER BY id DESC")
        await safe_edit(query, f"📦 全部商品\n\n共 {len(products)} 个商品", products_keyboard(products, "prod:", "catalog"))
        return

    if data.startswith("prod:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product or not int(product.get("active", 0) or 0):
            await safe_edit(query, "商品不存在或已下架。", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data="catalog")]]))
            return
        await send_product_to_user(update, product)
        return

    if data == "admin":
        if not is_admin(user_id):
            return
        clear_state(context)
        await safe_edit(query, "⚙️ 管理后台", admin_menu_keyboard())
        return

    if not is_admin(user_id):
        await safe_edit(query, "无权限。")
        return

    if data.startswith("admin_products:"):
        page = max(0, int(data.split(":", 1)[1]))
        await show_admin_products(query, page)
        return

    if data.startswith("admin_prod:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product:
            await safe_edit(query, "商品不存在。", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 商品管理", callback_data="admin_products:0")]]))
            return
        await safe_edit(query, product_text(product, True), admin_product_keyboard(product))
        return

    if data == "admin_add":
        clear_state(context)
        context.user_data["state"] = "add_product"
        await safe_edit(query, "➕ 添加商品\n\n请发送：\n名称|编号|分类(c1-c12)|价格|库存|描述\n\n例如：\n商品A|P001|c1|100|20|测试商品")
        return

    if data.startswith("edit:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "商品不存在。")
            return
        clear_state(context)
        context.user_data["state"] = "edit_product"
        context.user_data["edit_product_id"] = pid
        await safe_edit(query, "✏️ 编辑商品\n\n请重新发送完整信息：\n名称|编号|分类(c1-c12)|价格|库存|描述")
        return

    if data.startswith("stock:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "商品不存在。")
            return
        clear_state(context)
        context.user_data["state"] = "stock"
        context.user_data["stock_product_id"] = pid
        await safe_edit(query, "📦 修改库存\n\n请输入新的库存数量，例如：100")
        return

    if data.startswith("photo:"):
        pid = int(data.split(":", 1)[1])
        if not product_get(pid):
            await safe_edit(query, "商品不存在。")
            return
        clear_state(context)
        context.user_data["state"] = "replace_photo"
        context.user_data["photo_product_id"] = pid
        await safe_edit(query, "🖼️ 更换图片\n\n请发送商品图片。\n如果不需要更换，请发送：跳过")
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
            await safe_edit(query, "商品不存在。")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ 确认删除", callback_data=f"confirm_delete:{pid}")],
            [InlineKeyboardButton("取消", callback_data=f"admin_prod:{pid}")],
        ])
        await safe_edit(query, f"确定删除商品？\n\n{product.get('name')} [{product.get('code') or '-'}]", kb)
        return

    if data.startswith("confirm_delete:"):
        pid = int(data.split(":", 1)[1])
        product_delete(pid)
        await safe_edit(query, "🗑️ 商品已删除。", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 商品管理", callback_data="admin_products:0")]]))
        return

    if data == "bulk_on" or data == "bulk_off":
        active = 1 if data == "bulk_on" else 0
        execute("UPDATE products SET active=1" if active else "UPDATE products SET active=0")
        await safe_edit(query, "已完成全部商品状态更新。", admin_menu_keyboard())
        return

    if data == "bulk_stock":
        clear_state(context)
        context.user_data["state"] = "bulk_stock_import"
        await safe_edit(
            query,
            "📊 一键批量修改库存\n\n"
            "请发送一个 CSV 文件。\n\n"
            "格式：\n"
            "code,stock\n"
            "P001,20\n"
            "P002,15\n"
            "P003,30\n\n"
            "只修改库存，不会修改商品名称、价格、图片、分类和上下架状态。\n"
            "商品编号必须与现有商品一致。\n\n"
            "也支持你原来的完整商品 CSV，但只读取 code 和 stock 两列。\n"
            "发送 /cancel 可取消。"
        )
        return

    if data == "admin_csv":
        clear_state(context)
        context.user_data["state"] = "csv_import"
        await safe_edit(query, "📥 只导入CSV\n\n请把 CSV 文件发送到这里。\n字段：name,code,category,price,stock,description")
        return

    if data == "admin_zip":
        clear_state(context)
        context.user_data["state"] = "zip_import"
        await safe_edit(query, "📦 一键上传商品+图片\n\n请发送一个 ZIP 文件。\n\n要求：\n• 1 个 CSV\n• 93 行商品\n• 93 张图片\n• 图片命名 01.jpg ～ 93.jpg（支持 jpg/jpeg/png/webp）\n• CSV 第1行对应图片01，第93行对应图片93")
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
            await safe_edit(query, "询价记录不存在。")
            return
        status = "已处理" if inquiry.get("status") == "handled" else "待处理"
        text = (
            f"📨 询价 #{iid}\n"
            f"状态：{status}\n"
            f"商品：{inquiry.get('product') or '-'}\n"
            f"用户ID：{inquiry.get('user_id') or '-'}\n"
            f"用户名：@{inquiry.get('username') or '-'}\n"
            f"内容：{inquiry.get('message') or '-'}\n"
            f"时间：{inquiry.get('created_at') or '-'}"
        )
        kb = []
        if inquiry.get("status") != "handled":
            kb.append([InlineKeyboardButton("✅ 标记已处理", callback_data=f"handled:{iid}")])
        kb.append([InlineKeyboardButton("⬅️ 询价记录", callback_data="admin_inquiries:0")])
        await safe_edit(query, text, InlineKeyboardMarkup(kb))
        return

    if data.startswith("handled:"):
        iid = int(data.split(":", 1)[1])
        p = db_placeholder()
        execute(f"UPDATE inquiries SET status='handled' WHERE id={p}", (iid,))
        await safe_edit(query, "✅ 已标记为已处理。", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 询价记录", callback_data="admin_inquiries:0")]]))
        return

    if data.startswith("inq:"):
        pid = int(data.split(":", 1)[1])
        product = product_get(pid)
        if not product or not int(product.get("active", 0) or 0):
            await safe_edit(query, "商品不存在或已下架。")
            return
        clear_state(context)
        context.user_data["state"] = "inquiry"
        context.user_data["inquiry_product_id"] = pid
        await safe_edit(query, f"💬 询价：{product['name']}\n\n请输入数量、价格要求或其他需求。\n\n发送 /cancel 可取消")
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
        status = "🟢" if int(p.get("active", 0) or 0) else "🔴"
        rows.append([InlineKeyboardButton(f"{status} {p['name']} [{p.get('code') or '-'}]", callback_data=f"admin_prod:{p['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"admin_products:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"admin_products:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ 管理后台", callback_data="admin")])
    await safe_edit(query, f"📦 商品管理\n\n共 {total} 个商品\n第 {page + 1} 页", InlineKeyboardMarkup(rows))


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
        status = "✅" if x.get("status") == "handled" else "🟠"
        label = f"{status} #{x['id']} {x.get('product') or '商品'}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"inqadmin:{x['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"admin_inquiries:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"admin_inquiries:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ 管理后台", callback_data="admin")])
    await safe_edit(query, f"📨 询价记录\n\n共 {total} 条\n第 {page + 1} 页", InlineKeyboardMarkup(rows))


async def show_status(query):
    total = product_count()
    active = fetchone("SELECT COUNT(*) AS n FROM products WHERE active=1")
    photos = fetchone("SELECT COUNT(*) AS n FROM products WHERE photo_id IS NOT NULL AND photo_id <> ''")
    inquiries = fetchone("SELECT COUNT(*) AS n FROM inquiries")
    csv_path = BASE_DIR / CSV_FILE
    text = (
        "📊 系统状态\n\n"
        f"数据库：{'PostgreSQL' if USE_POSTGRES else 'SQLite'}\n"
        f"商品总数：{total}\n"
        f"上架商品：{int(active['n'] or 0)}\n"
        f"有图片商品：{int(photos['n'] or 0)}\n"
        f"询价总数：{int(inquiries['n'] or 0)}\n"
        f"CSV：{CSV_FILE}\n"
        f"CSV存在：{'是' if csv_path.exists() else '否'}\n"
        f"Web：{WEB_URL}\n"
        f"PORT：{PORT}"
    )
    await safe_edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 管理后台", callback_data="admin")]]))


# -------------------- 文本输入 --------------------

def parse_product_line(text):
    parts = [x.strip() for x in text.split("|")]
    if len(parts) < 6:
        raise ValueError("格式不正确，必须是：名称|编号|分类|价格|库存|描述")
    name, code, category, price, stock_text, description = parts[:6]
    if not name:
        raise ValueError("商品名称不能为空")
    if category not in CATEGORIES:
        raise ValueError("分类必须是 c1-c12")
    try:
        stock = int(stock_text)
    except ValueError:
        raise ValueError("库存必须是整数")
    if stock < 0:
        raise ValueError("库存不能小于0")
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
            await update.message.reply_text("没有找到商品。", reply_markup=main_menu(user_id))
        else:
            await update.message.reply_text(f"🔎 找到 {len(products)} 个商品：", reply_markup=products_keyboard(products, "prod:", "home"))
        return

    if not is_admin(user_id):
        if state == "inquiry":
            pid = context.user_data.get("inquiry_product_id")
            product = product_get(pid) if pid else None
            if not product:
                clear_state(context)
                await update.message.reply_text("商品不存在。", reply_markup=main_menu(user_id))
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
                f"📨 新询价 #{inquiry_id}\n"
                f"商品：{product['name']} [{product.get('code') or '-'}]\n"
                f"用户ID：{user_id}\n"
                f"用户名：@{username or '-'}\n"
                f"内容：{text}"
            )
            await notify_admins(context, notify_text)
            clear_state(context)
            await update.message.reply_text(
                f"✅ 询价已提交。\n客服：@{SERVICE_USERNAME}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 客服直达", url=f"https://t.me/{SERVICE_USERNAME}")],
                    [InlineKeyboardButton("🏠 返回主菜单", callback_data="home")],
                ]),
            )
        else:
            await update.message.reply_text("请选择菜单功能。", reply_markup=main_menu(user_id))
        return

    # 管理员状态
    if state == "add_product":
        try:
            name, code, category, price, stock, description = parse_product_line(text)
            pid = product_create(name, code, category, price, stock, description)
            clear_state(context)
            context.user_data["state"] = "add_photo"
            context.user_data["pending_product_id"] = pid
            await update.message.reply_text("商品已创建。\n\n请发送商品图片。\n如果不需要图片，请发送：跳过")
        except Exception as exc:
            await update.message.reply_text(f"❌ {exc}\n\n请重新发送正确格式。")
        return

    if state == "add_photo":
        if text == "跳过":
            pid = context.user_data.get("pending_product_id")
            clear_state(context)
            product = product_get(pid) if pid else None
            await update.message.reply_text("已跳过图片。", reply_markup=admin_product_keyboard(product) if product else admin_menu_keyboard())
            return
        await update.message.reply_text("请发送商品图片，或者发送：跳过")
        return

    if state == "edit_product":
        pid = context.user_data.get("edit_product_id")
        if not pid or not product_get(pid):
            clear_state(context)
            await update.message.reply_text("商品不存在。", reply_markup=admin_menu_keyboard())
            return
        try:
            name, code, category, price, stock, description = parse_product_line(text)
            product_update(pid, name, code, category, price, stock, description)
            product = product_get(pid)
            clear_state(context)
            await update.message.reply_text("✅ 商品已更新。", reply_markup=admin_product_keyboard(product))
        except Exception as exc:
            await update.message.reply_text(f"❌ {exc}\n\n请重新发送完整信息。")
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
            await update.message.reply_text("✅ 库存已修改。", reply_markup=admin_product_keyboard(product))
        except Exception:
            await update.message.reply_text("❌ 库存必须是 0 或以上的整数，请重新输入。")
        return

    if state == "replace_photo":
        if text == "跳过":
            pid = context.user_data.get("photo_product_id")
            product = product_get(pid) if pid else None
            clear_state(context)
            await update.message.reply_text("已取消更换图片。", reply_markup=admin_product_keyboard(product) if product else admin_menu_keyboard())
        else:
            await update.message.reply_text("请发送商品图片，或者发送：跳过")
        return

    if state == "inquiry":
        # 管理员也允许正常提交询价
        pid = context.user_data.get("inquiry_product_id")
        product = product_get(pid) if pid else None
        if not product:
            clear_state(context)
            await update.message.reply_text("商品不存在。", reply_markup=admin_menu_keyboard())
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
        await notify_admins(context, f"📨 新询价 #{inquiry_id}\n商品：{product['name']}\n用户ID：{user_id}\n内容：{text}")
        clear_state(context)
        await update.message.reply_text("✅ 询价已提交。", reply_markup=main_menu(user_id))
        return

    await update.message.reply_text("请选择菜单功能。", reply_markup=admin_menu_keyboard())


# -------------------- 图片 / 文件 --------------------

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
        await update.effective_chat.send_message("✅ 图片已保存，上传图片消息已尝试自动删除。", reply_markup=admin_product_keyboard(product))


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
            await update.message.reply_text("请先在管理后台点击“📦 一键上传商品+图片”，再发送 ZIP。")
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
            await update.effective_chat.send_message(f"✅ CSV 导入完成，共处理 {count} 行。", reply_markup=admin_menu_keyboard())
        except Exception as exc:
            await update.message.reply_text(f"❌ CSV 导入失败：{exc}")
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
                f"✅ 批量修改库存完成！\n\n成功更新：{count} 个商品\n\n"
                "商品名称、价格、图片、分类和上下架状态均未修改。",
                reply_markup=admin_menu_keyboard(),
            )
        except Exception as exc:
            print(f"批量修改库存失败：{type(exc).__name__}: {exc}")
            traceback.print_exc()
            await update.message.reply_text(f"❌ 批量修改库存失败：\n{type(exc).__name__}: {exc}")
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
            await update.effective_chat.send_message("✅ 图片已保存，上传图片消息已尝试自动删除。", reply_markup=admin_product_keyboard(product))


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
                raise ValueError(f"ZIP 必须且只能有 1 个 CSV，目前有 {len(csv_members)} 个")
            if len(image_members) != 93:
                raise ValueError(f"ZIP 必须有 93 张命名为 01-93 的图片，目前检测到 {len(image_members)} 张")
            image_map = {}
            for m in image_members:
                num = int(Path(m).stem)
                if not 1 <= num <= 93:
                    raise ValueError(f"图片编号超出范围：{m}")
                if num in image_map:
                    raise ValueError(f"图片编号重复：{num:02d}")
                image_map[num] = m
            if len(image_map) != 93:
                raise ValueError("图片必须完整覆盖 01-93")
            csv_data = z.read(csv_members[0])
            csv_text = None
            for enc in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    csv_text = csv_data.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            if csv_text is None:
                raise ValueError("无法识别 CSV 编码")
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames:
                raise ValueError("CSV 没有表头")
            fields = {str(x).strip().lower(): x for x in reader.fieldnames if x}
            required = ["name", "code", "category", "price", "stock"]
            missing = [x for x in required if x not in fields]
            if missing:
                raise ValueError("CSV 缺少字段：" + ", ".join(missing))
            rows = []
            for raw in reader:
                row = {}
                for key, original in fields.items():
                    row[key] = raw.get(original, "")
                rows.append(row)
            if len(rows) != 93:
                raise ValueError(f"CSV 必须正好 93 行，目前是 {len(rows)} 行")

            # 严格校验 CSV 第1~93行必须对应 P001~P093，避免图片错配。
            for index, row in enumerate(rows, start=1):
                expected_code = f"P{index:03d}"
                actual_code = str(row.get("code", "")).strip().upper()
                if actual_code != expected_code:
                    raise ValueError(
                        f"CSV 第 {index} 行编号应为 {expected_code}，实际为 {actual_code or '空'}"
                    )
                if not str(row.get("name", "")).strip():
                    raise ValueError(f"CSV 第 {index} 行商品名称为空")
                normalized_category = normalize_category(row.get("category", ""))
                if normalized_category not in CATEGORIES:
                    raise ValueError(
                        f"CSV 第 {index} 行分类无效：{row.get('category', '')}；支持 c1-c12 或中文分类名称"
                    )
                row["category"] = normalized_category

            await update.effective_chat.send_message("⏳ 已验证 ZIP：1个CSV + 93张图片。开始导入，请稍候…")
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
                    raise ValueError(f"第 {index} 张图片无法取得 Telegram file_id")
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
                    await update.effective_chat.send_message(f"⏳ 已完成 {index}/93")

        clear_state(context)
        await update.effective_chat.send_message("✅ 93个商品 + 93张图片全部导入完成。", reply_markup=admin_menu_keyboard())
    except Exception as exc:
        clear_state(context)
        error_text = f"{type(exc).__name__}: {exc}"
        print(f"ZIP 导入失败：{error_text}")
        traceback.print_exc()
        await update.effective_chat.send_message(
            f"❌ ZIP 导入失败：\n{error_text}",
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
            self.send_json({"ok": False, "error": "商品不存在"}, 404)
            return
        message = str(payload.get("message", "")).strip()
        if not message:
            self.send_json({"ok": False, "error": "请填写询价内容"}, 400)
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
            f"📨 Web新询价 #{inquiry_id}\n"
            f"商品：{product_name}\n"
            f"用户：{username}\n"
            f"用户ID：{user_id}\n"
            f"联系方式：{contact or '-'}\n"
            f"内容：{message}"
        )
        threading.Thread(target=notify_admins_sync, args=(notify,), daemon=True).start()
        self.send_json({"ok": True, "inquiry_id": inquiry_id})


def start_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), CatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Web server listening on 0.0.0.0:{PORT}")
    return server


# -------------------- 启动 --------------------

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
    print(f"Webhook URL: {WEB_URL}/telegram")

    # 创建 asyncio 事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def startup():
        await application.initialize()
        await application.start()

        # 启动现有 Web 服务
        start_web_server(application, loop)

        # 设置 Telegram Webhook
        await application.bot.set_webhook(
            url=f"{WEB_URL}/telegram",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )

        print("Telegram Webhook 已设置")
        print(f"Webhook: {WEB_URL}/telegram")

    try:
        loop.run_until_complete(startup())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        async def shutdown():
            try:
                await application.bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass

            try:
                await application.stop()
            except Exception:
                pass

            try:
                await application.shutdown()
            except Exception:
                pass

        try:
            loop.run_until_complete(shutdown())
        finally:
            loop.close()


if __name__ == "__main__":
    main()
