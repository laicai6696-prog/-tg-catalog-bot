import os
import sqlite3
import logging
import csv
import re
import zipfile
import tempfile
import shutil
import asyncio
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

DB = "bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

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

CAT_BY_NAME = {v: k for k, v in CATS.items()}


# =========================
# 数据库
# =========================

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()

    c.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            price TEXT DEFAULT '询价',
            stock INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            photo_id TEXT DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS inquiries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            product TEXT,
            message TEXT,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cols = {
        r[1]
        for r in c.execute("PRAGMA table_info(products)").fetchall()
    }

    if "photo_id" not in cols:
        c.execute(
            "ALTER TABLE products ADD COLUMN photo_id TEXT DEFAULT ''"
        )

    c.commit()
    c.close()


# =========================
# 普通用户首页
# =========================

def home():
    items = list(CATS.items())
    rows = []

    for i in range(0, len(items), 2):
        row = [
            InlineKeyboardButton(
                CATS[items[i][0]],
                callback_data=items[i][0]
            )
        ]

        if i + 1 < len(items):
            row.append(
                InlineKeyboardButton(
                    CATS[items[i + 1][0]],
                    callback_data=items[i + 1][0]
                )
            )

        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            "🔎 搜索",
            callback_data="search"
        ),
        InlineKeyboardButton(
            "📩 询价",
            callback_data="inquiry"
        )
    ])

    return InlineKeyboardMarkup(rows)


# =========================
# 管理后台首页
# =========================

def admin_home(p, a, q):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📦 商品管理",
                callback_data="a:products"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 一键上传商品+图片",
                callback_data="a:importpkg"
            )
        ],
        [
            InlineKeyboardButton(
                "📥 只导入CSV",
                callback_data="a:importcsv"
            )
        ],
        [
            InlineKeyboardButton(
                "📩 询价",
                callback_data="a:inq"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ 添加商品",
                callback_data="a:add"
            )
        ],
    ])


# =========================
# /start
# =========================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()

    await update.message.reply_text(
        "欢迎使用商品目录\n请选择分类：",
        reply_markup=home()
    )


# =========================
# 用户按钮
# =========================

async def user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data in CATS:

        c = db()

        rows = c.execute(
            """
            SELECT id,name,price
            FROM products
            WHERE category=? AND active=1
            ORDER BY id
            """,
            (q.data,)
        ).fetchall()

        c.close()

        kb = []

        for r in rows:

            if str(r["price"]).isdigit():
                label = f"{r['name']} · ¥{r['price']}"
            else:
                label = f"{r['name']} · {r['price']}"

            kb.append([
                InlineKeyboardButton(
                    label,
                    callback_data=f"p:{r['id']}"
                )
            ])

        kb.append([
            InlineKeyboardButton(
                "⬅️ 首页",
                callback_data="home"
            )
        ])

        await q.message.edit_text(
            f"【{CATS[q.data]}】",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif q.data == "home":

        await q.message.edit_text(
            "请选择分类：",
            reply_markup=home()
        )

    elif q.data == "search":

        ctx.user_data["mode"] = "search"

        await q.message.reply_text(
            "请输入商品名称或编号："
        )

    elif q.data == "inquiry":

        ctx.user_data["mode"] = "inquiry"

        await q.message.reply_text(
            "请输入商品、规格、数量或需求："
        )

    elif q.data.startswith("p:"):

        pid = int(q.data[2:])

        c = db()

        r = c.execute(
            """
            SELECT *
            FROM products
            WHERE id=? AND active=1
            """,
            (pid,)
        ).fetchone()

        c.close()

        if not r:
            return

        kb = [
            [
                InlineKeyboardButton(
                    "📩 提交询价",
                    callback_data=f"iq:{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回",
                    callback_data=r["category"]
                )
            ]
        ]

        text = (
            f"【{r['name']}】\n"
            f"编号：{r['code']}\n"
            f"价格：{r['price']}\n"
            f"库存：{r['stock']}\n\n"
            f"{r['description']}"
        )

        if r["photo_id"]:

            try:

                await q.message.reply_photo(
                    photo=r["photo_id"],
                    caption=text,
                    reply_markup=InlineKeyboardMarkup(kb)
                )

            except Exception:

                await q.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(kb)
                )

        else:

            await q.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(kb)
            )

    elif q.data.startswith("iq:"):

        pid = int(q.data[3:])

        c = db()

        r = c.execute(
            "SELECT name FROM products WHERE id=?",
            (pid,)
        ).fetchone()

        c.close()

        ctx.user_data["mode"] = "inquiry"

        ctx.user_data["product"] = (
            r["name"] if r else ""
        )

        await q.message.reply_text(
            f"商品：{ctx.user_data['product']}\n"
            f"请发送需求："
        )


# =========================
# 普通用户文字
# =========================

async def user_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    mode = ctx.user_data.get("mode")

    if mode == "search":

        ctx.user_data.clear()

        c = db()

        rows = c.execute(
            """
            SELECT id,name,price
            FROM products
            WHERE active=1
            AND (name LIKE ? OR code LIKE ?)
            LIMIT 30
            """,
            (
                f"%{text}%",
                f"%{text}%"
            )
        ).fetchall()

        c.close()

        if not rows:

            await update.message.reply_text(
                "没有找到结果。",
                reply_markup=home()
            )

            return

        kb = []

        for r in rows:

            kb.append([
                InlineKeyboardButton(
                    f"{r['name']} · {r['price']}",
                    callback_data=f"p:{r['id']}"
                )
            ])

        await update.message.reply_text(
            "搜索结果：",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif mode == "inquiry":

        product = ctx.user_data.get(
            "product",
            ""
        )

        u = update.effective_user

        c = db()

        cur = c.execute(
            """
            INSERT INTO inquiries
            (user_id,username,product,message)
            VALUES(?,?,?,?)
            """,
            (
                u.id,
                u.username or "",
                product,
                text
            )
        )

        iid = cur.lastrowid

        c.commit()
        c.close()

        ctx.user_data.clear()

        await update.message.reply_text(
            f"询价已提交 #{iid}"
        )

        for aid in ADMINS:

            try:

                await ctx.bot.send_message(
                    aid,
                    f"📩 新询价 #{iid}\n"
                    f"用户：@{u.username or '未设置'}\n"
                    f"ID：{u.id}\n"
                    f"商品：{product}\n"
                    f"内容：{text}"
                )

            except Exception:

                pass


# =========================
# 管理员后台
# =========================

async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id not in ADMINS:
        return

    c = db()

    p = c.execute(
        "SELECT COUNT(*) n FROM products"
    ).fetchone()["n"]

    a = c.execute(
        "SELECT COUNT(*) n FROM products WHERE active=1"
    ).fetchone()["n"]

    q = c.execute(
        """
        SELECT COUNT(*) n
        FROM inquiries
        WHERE status='new'
        """
    ).fetchone()["n"]

    c.close()

    await update.message.reply_text(
        f"管理后台\n"
        f"商品 {p}｜上架 {a}\n"
        f"待处理询价 {q}",
        reply_markup=admin_home(p, a, q)
    )


# =========================
# 管理员按钮
# =========================

async def admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    if q.from_user.id not in ADMINS:

        await q.answer()

        return

    await q.answer()

    if q.data == "a:products":

        c = db()

        rows = c.execute(
            """
            SELECT id,name,code,price,stock,active,photo_id
            FROM products
            ORDER BY id
            """
        ).fetchall()

        c.close()

        kb = [
            [
                InlineKeyboardButton(
                    CATS[k],
                    callback_data=f"a:cat:{k}"
                )
            ]
            for k in CATS
        ]

        kb.append([
            InlineKeyboardButton(
                "⬅️ 后台",
                callback_data="a:back"
            )
        ])

        await q.message.reply_text(
            f"📦 商品管理\n"
            f"共 {len(rows)} 个商品\n"
            f"请选择系列：",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif q.data.startswith("a:cat:"):

        cat = q.data.split(":", 2)[2]

        c = db()

        rows = c.execute(
            """
            SELECT id,name,price,photo_id
            FROM products
            WHERE category=?
            ORDER BY id
            """,
            (cat,)
        ).fetchall()

        c.close()

        kb = []

        for r in rows:

            prefix = "🖼️ " if r["photo_id"] else "▫️ "

            kb.append([
                InlineKeyboardButton(
                    prefix + r["name"],
                    callback_data=f"a:prod:{r['id']}"
                )
            ])

        kb.append([
            InlineKeyboardButton(
                "⬅️ 商品管理",
                callback_data="a:products"
            )
        ])

        await q.message.reply_text(
            f"【{CATS.get(cat, cat)}】\n"
            f"点击商品可管理图片：",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif q.data.startswith("a:prod:"):

        pid = int(
            q.data.split(":")[2]
        )

        c = db()

        r = c.execute(
            "SELECT * FROM products WHERE id=?",
            (pid,)
        ).fetchone()

        c.close()

        if not r:
            return

        buttons = [
            [
                InlineKeyboardButton(
                    "🖼️ 添加/更换图片",
                    callback_data=f"a:setphoto:{pid}"
                )
            ]
        ]

        if r["photo_id"]:

            buttons.append([
                InlineKeyboardButton(
                    "👁️ 查看图片",
                    callback_data=f"a:viewphoto:{pid}"
                ),
                InlineKeyboardButton(
                    "🗑️ 删除图片",
                    callback_data=f"a:delphoto:{pid}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data=f"a:cat:{r['category']}"
            )
        ])

        await q.message.reply_text(
            f"商品：{r['name']}\n"
            f"编号：{r['code']}\n"
            f"图片：{'已绑定' if r['photo_id'] else '未绑定'}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif q.data.startswith("a:setphoto:"):

        pid = int(
            q.data.split(":")[2]
        )

        ctx.user_data["admin_mode"] = "set_photo"
        ctx.user_data["photo_product_id"] = pid

        await q.message.reply_text(
            "请发送这件商品的图片。\n"
            "支持 JPG、PNG、WEBP。"
        )

    elif q.data.startswith("a:viewphoto:"):

        pid = int(
            q.data.split(":")[2]
        )

        c = db()

        r = c.execute(
            """
            SELECT name,photo_id
            FROM products
            WHERE id=?
            """,
            (pid,)
        ).fetchone()

        c.close()

        if r and r["photo_id"]:

            await q.message.reply_photo(
                r["photo_id"],
                caption=r["name"]
            )

    elif q.data.startswith("a:delphoto:"):

        pid = int(
            q.data.split(":")[2]
        )

        c = db()

        c.execute(
            """
            UPDATE products
            SET photo_id=''
            WHERE id=?
            """,
            (pid,)
        )

        c.commit()
        c.close()

        await q.message.reply_text(
            "✅ 图片已删除。"
        )

    elif q.data == "a:importpkg":

        ctx.user_data["admin_mode"] = "import_package"

        await q.message.reply_text(
            "📦 一键上传商品+图片\n\n"
            "请把【商品表CSV + 01~93图片】"
            "放进同一个ZIP，然后把ZIP文件发送给我。\n\n"
            "对应关系：01 → P001，"
            "02 → P002……93 → P093。\n\n"
            "我会自动新增/更新商品并绑定图片，"
            "无需一张张上传。"
        )

    elif q.data == "a:importcsv":

        ctx.user_data["admin_mode"] = "import_csv"

        await q.message.reply_text(
            "请发送93个商品的CSV文件。"
        )

    elif q.data == "a:inq":

        c = db()

        rows = c.execute(
            """
            SELECT id,username,product,message,status,created_at
            FROM inquiries
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

        c.close()

        if not rows:

            await q.message.reply_text(
                "暂无询价"
            )

            return

        text = "\n\n".join(
            f"#{r['id']} @{r['username'] or '未设置'}\n"
            f"商品：{r['product']}\n"
            f"{r['message']}\n"
            f"[{r['status']}] {r['created_at']}"
            for r in rows
        )

        await q.message.reply_text(
            text[:4000]
        )

    elif q.data == "a:add":

        ctx.user_data["admin_mode"] = "add_photo"

        await q.message.reply_text(
            "第一步：请先发送商品图片。\n"
            "如果暂时没有图片，请发送：跳过"
        )

    elif q.data == "a:back":

        await admin(update, ctx)


# =========================
# ZIP 导入
# =========================

async def process_package(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    zip_path: str
):

    admin_id = update.effective_user.id

    temp = Path(
        tempfile.mkdtemp(
            prefix="tg_import_"
        )
    )

    try:

        with zipfile.ZipFile(zip_path) as z:

            bad = [
                n
                for n in z.namelist()
                if Path(n).is_absolute()
                or ".." in Path(n).parts
            ]

            if bad:

                await update.message.reply_text(
                    "❌ ZIP文件包含不安全路径，已停止导入。"
                )

                return

            z.extractall(temp)

        csvs = list(
            temp.rglob("*.csv")
        )

        if not csvs:

            await update.message.reply_text(
                "❌ ZIP里没有CSV商品表。"
            )

            return

        csv_file = csvs[0]

        image_map = {}

        for p in temp.rglob("*"):

            if not p.is_file():
                continue

            m = re.match(
                r"^(\d{1,3})\.(jpg|jpeg|png|webp)$",
                p.name,
                re.I
            )

            if m:

                n = int(m.group(1))

                if 1 <= n <= 999:
                    image_map[n] = p

        with open(
            csv_file,
            "r",
            encoding="utf-8-sig",
            newline=""
        ) as f:

            rows = list(
                csv.DictReader(f)
            )

        required = {
            "name",
            "code",
            "category",
            "price",
            "stock"
        }

        if (
            not rows
            or not required.issubset(
                set(rows[0].keys())
            )
        ):

            await update.message.reply_text(
                "❌ CSV格式不正确，需要："
                "name,code,category,price,stock"
            )

            return

        if len(rows) != 93:

            await update.message.reply_text(
                f"⚠️ CSV当前有 {len(rows)} 个商品，"
                "不是预期的93个。"
                "为避免误导入，已停止。"
            )

            return

        missing = [
            n
            for n in range(1, 94)
            if n not in image_map
        ]

        if missing:

            await update.message.reply_text(
                "❌ 图片不完整，缺少："
                + ", ".join(
                    f"{n:02d}"
                    for n in missing
                )
            )

            return

        c = db()

        inserted = 0
        updated = 0

        try:

            for row in rows:

                code = row["code"].strip()
                name = row["name"].strip()

                cat_raw = row[
                    "category"
                ].strip()

                cat = CAT_BY_NAME.get(
                    cat_raw,
                    cat_raw
                )

                if cat not in CATS:

                    raise ValueError(
                        f"商品 {code} 的分类无效："
                        f"{cat_raw}"
                    )

                price = row[
                    "price"
                ].strip()

                stock = int(
                    float(
                        row["stock"].strip()
                        or 0
                    )
                )

                desc = row.get(
                    "description",
                    ""
                ).strip()

                exists = c.execute(
                    "SELECT id FROM products WHERE code=?",
                    (code,)
                ).fetchone()

                if exists:

                    c.execute(
                        """
                        UPDATE products
                        SET name=?,
                            category=?,
                            price=?,
                            stock=?,
                            description=?,
                            active=1
                        WHERE code=?
                        """,
                        (
                            name,
                            cat,
                            price,
                            stock,
                            desc,
                            code
                        )
                    )

                    updated += 1

                else:

                    c.execute(
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
                        VALUES(?,?,?,?,?,?,1,?)
                        """,
                        (
                            name,
                            code,
                            cat,
                            price,
                            stock,
                            desc,
                            ""
                        )
                    )

                    inserted += 1

            c.commit()

        except Exception:

            c.rollback()

            raise

        finally:

            c.close()

        await update.message.reply_text(
            f"📥 商品表已导入："
            f"新增 {inserted}，"
            f"更新 {updated}\n"
            f"🖼️ 开始绑定93张图片，请稍候……"
        )

        photo_ok = 0
        photo_fail = []

        for n in range(1, 94):

            code = f"P{n:03d}"

            path = image_map[n]

            try:

                # 直接上传图片到管理员聊天，
                # Telegram 会返回 file_id。
                with open(path, "rb") as f:

                    msg = await ctx.bot.send_photo(
                        chat_id=admin_id,
                        photo=f
                    )

                file_id = msg.photo[-1].file_id

                c = db()

                c.execute(
                    """
                    UPDATE products
                    SET photo_id=?
                    WHERE code=?
                    """,
                    (
                        file_id,
                        code
                    )
                )

                c.commit()
                c.close()

                try:

                    await ctx.bot.delete_message(
                        chat_id=admin_id,
                        message_id=msg.message_id
                    )

                except Exception:

                    pass

                photo_ok += 1

                await asyncio.sleep(0.3)

            except Exception:

                logging.exception(
                    "photo import failed %s",
                    code
                )

                photo_fail.append(code)

        result = (
            "✅ 一键上传完成！\n\n"
            f"商品：新增 {inserted}｜更新 {updated}\n"
            f"图片：成功 {photo_ok}｜失败 {len(photo_fail)}"
        )

        if photo_fail:

            result += (
                "\n失败图片："
                + ", ".join(photo_fail)
            )

        await update.message.reply_text(
            result
        )

    except Exception as e:

        logging.exception(
            "package import failed"
        )

        await update.message.reply_text(
            f"❌ 导入失败：{e}"
        )

    finally:

        shutil.rmtree(
            temp,
            ignore_errors=True
        )

        try:
            os.remove(zip_path)
        except Exception:
            pass

        ctx.user_data.clear()


# =========================
# 文件接收
# =========================

async def admin_document(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    mode = ctx.user_data.get(
        "admin_mode"
    )

    if mode not in {
        "import_package",
        "import_csv"
    }:

        return

    doc = update.message.document

    name = (
        doc.file_name or ""
    ).lower()

    if mode == "import_package":

        if not name.endswith(".zip"):

            await update.message.reply_text(
                "请发送ZIP压缩包。"
            )

            return

        await update.message.reply_text(
            "📥 已收到ZIP，正在下载并检查文件，请稍候……"
        )

        try:

            f = await doc.get_file()

            path = os.path.join(
                tempfile.gettempdir(),
                f"tg_package_"
                f"{update.effective_user.id}.zip"
            )

            await f.download_to_drive(path)

            await update.message.reply_text(
                "✅ ZIP下载完成，开始读取93个商品和图片……"
            )

            await process_package(
                update,
                ctx,
                path
            )

        except Exception as e:

            logging.exception(
                "ZIP download failed"
            )

            await update.message.reply_text(
                f"❌ ZIP处理失败：{e}"
            )

    else:

        if not name.endswith(".csv"):

            await update.message.reply_text(
                "请发送CSV文件。"
            )

            return

        try:

            f = await doc.get_file()

            path = os.path.join(
                tempfile.gettempdir(),
                f"tg_csv_"
                f"{update.effective_user.id}.csv"
            )

            await f.download_to_drive(path)

            with open(
                path,
                "r",
                encoding="utf-8-sig",
                newline=""
            ) as fp:

                rows = list(
                    csv.DictReader(fp)
                )

            c = db()

            new = 0
            upd = 0

            for row in rows:

                cat = CAT_BY_NAME.get(
                    row["category"].strip(),
                    row["category"].strip()
                )

                stock = int(
                    float(
                        row.get(
                            "stock",
                            0
                        )
                        or 0
                    )
                )

                code = row[
                    "code"
                ].strip()

                ex = c.execute(
                    """
                    SELECT id
                    FROM products
                    WHERE code=?
                    """,
                    (code,)
                ).fetchone()

                if ex:

                    c.execute(
                        """
                        UPDATE products
                        SET name=?,
                            category=?,
                            price=?,
                            stock=?,
                            description=?,
                            active=1
                        WHERE code=?
                        """,
                        (
                            row["name"].strip(),
                            cat,
                            row["price"].strip(),
                            stock,
                            row.get(
                                "description",
                                ""
                            ).strip(),
                            code
                        )
                    )

                    upd += 1

                else:

                    c.execute(
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
                        VALUES(?,?,?,?,?,?,1,'')
                        """,
                        (
                            row["name"].strip(),
                            code,
                            cat,
                            row["price"].strip(),
                            stock,
                            row.get(
                                "description",
                                ""
                            ).strip()
                        )
                    )

                    new += 1

            c.commit()
            c.close()

            await update.message.reply_text(
                f"✅ CSV导入完成："
                f"新增 {new}｜更新 {upd}\n"
                f"已有图片不会被覆盖。"
            )

        except Exception as e:

            await update.message.reply_text(
                f"❌ CSV导入失败：{e}"
            )

        finally:

            try:
                os.remove(path)
            except Exception:
                pass

            ctx.user_data.clear()


# =========================
# 图片处理
# =========================

async def admin_photo(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    mode = ctx.user_data.get(
        "admin_mode"
    )

    if mode == "set_photo":

        pid = ctx.user_data.get(
            "photo_product_id"
        )

        if not pid:
            return

        file_id = (
            update.message.photo[-1].file_id
        )

        c = db()

        c.execute(
            """
            UPDATE products
            SET photo_id=?
            WHERE id=?
            """,
            (
                file_id,
                pid
            )
        )

        c.commit()

        r = c.execute(
            """
            SELECT name
            FROM products
            WHERE id=?
            """,
            (pid,)
        ).fetchone()

        c.close()

        ctx.user_data.clear()

        await update.message.reply_text(
            f"✅ 已绑定图片："
            f"{r['name'] if r else pid}"
        )

    elif mode == "add_photo":

        ctx.user_data[
            "new_photo_id"
        ] = update.message.photo[-1].file_id

        ctx.user_data[
            "admin_mode"
        ] = "add"

        await update.message.reply_text(
            "图片已收到。\n"
            "第二步请发送：\n"
            "名称|编号|分类(c1-c12)|价格|库存|描述"
        )


# =========================
# 管理员文字
# =========================

async def admin_text(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    mode = ctx.user_data.get(
        "admin_mode"
    )

    if (
        mode == "add_photo"
        and update.message.text.strip() == "跳过"
    ):

        ctx.user_data[
            "new_photo_id"
        ] = ""

        ctx.user_data[
            "admin_mode"
        ] = "add"

        await update.message.reply_text(
            "已跳过图片。\n"
            "请发送：名称|编号|分类(c1-c12)|价格|库存|描述"
        )

        return

    if mode != "add":
        return

    parts = [
        x.strip()
        for x in update.message.text.split("|")
    ]

    if len(parts) != 6:

        await update.message.reply_text(
            "格式错误，请按："
            "名称|编号|分类|价格|库存|描述"
        )

        return

    name, code, cat, price, stock, desc = parts

    if cat not in CATS:

        await update.message.reply_text(
            "分类必须是 c1-c12"
        )

        return

    try:

        stock = int(stock)

    except:

        await update.message.reply_text(
            "库存必须是数字"
        )

        return

    photo_id = ctx.user_data.get(
        "new_photo_id",
        ""
    )

    c = db()

    try:

        c.execute(
            """
            INSERT INTO products
            (
                name,
                code,
                category,
                price,
                stock,
                description,
                photo_id
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                name,
                code,
                cat,
                price,
                stock,
                desc,
                photo_id
            )
        )

        c.commit()

        await update.message.reply_text(
            "✅ 商品已添加。"
        )

    except sqlite3.IntegrityError:

        await update.message.reply_text(
            "编号已存在。"
        )

    finally:

        c.close()

    ctx.user_data.clear()


# =========================
# 文字路由
# =========================

async def text_router(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if (
        update.effective_user.id in ADMINS
        and ctx.user_data.get("admin_mode")
    ):

        await admin_text(
            update,
            ctx
        )

    else:

        await user_text(
            update,
            ctx
        )


# =========================
# Render 健康检查网页
# =========================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"Telegram catalog bot is running."
        )

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    logging.info(
        "Health server started on port %s",
        port
    )

    server.serve_forever()


# =========================
# 主程序
# =========================

def main():

    if not TOKEN:

        raise RuntimeError(
            "请在 Render Environment 设置 BOT_TOKEN"
        )

    init()

    # Render需要一个网页端口保持服务正常
    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_button,
            pattern=r"^a:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            user_button
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            admin_document
        )
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            admin_photo
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    logging.info(
        "Telegram bot starting with polling..."
    )

    # 不再使用Webhook。
    # Render通过后台Polling主动接收Telegram消息。
    app.run_polling(
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
