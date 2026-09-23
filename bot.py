import os
import sqlite3
import logging
import csv
import io

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

logging.basicConfig(level=logging.INFO)

# =========================
# 12个商品系列
# =========================

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
            active INTEGER DEFAULT 1
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

    # 只有数据库完全没有商品时，才添加示例商品
    if c.execute(
        "SELECT COUNT(*) n FROM products"
    ).fetchone()["n"] == 0:

        c.executemany(
            """
            INSERT INTO products
            (name, code, category, price, stock, description)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "示例商品A",
                    "A001",
                    "c1",
                    "询价",
                    10,
                    "商品介绍",
                ),
                (
                    "示例商品B",
                    "B001",
                    "c2",
                    "询价",
                    20,
                    "商品介绍",
                ),
            ],
        )

    c.commit()
    c.close()


# =========================
# 用户首页
# =========================

def home():
    ks = list(CATS.items())

    rows = []

    for i in range(0, 12, 2):
        rows.append([
            InlineKeyboardButton(
                CATS[ks[i][0]],
                callback_data=ks[i][0]
            ),
            InlineKeyboardButton(
                CATS[ks[i + 1][0]],
                callback_data=ks[i + 1][0]
            ),
        ])

    rows += [
        [
            InlineKeyboardButton(
                "🔎 搜索",
                callback_data="search"
            ),
            InlineKeyboardButton(
                "📩 询价",
                callback_data="inquiry"
            ),
        ]
    ]

    return InlineKeyboardMarkup(rows)


# =========================
# /start
# =========================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    ctx.user_data.clear()

    await update.message.reply_text(
        "🛒 商品报价目录\n\n"
        "欢迎使用商品报价目录\n"
        "请选择商品系列：",
        reply_markup=home(),
    )


# =========================
# 用户按钮
# =========================

async def user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    # -------------------------
    # 商品分类
    # -------------------------

    if q.data in CATS:

        c = db()

        rows = c.execute(
            """
            SELECT id, name, price
            FROM products
            WHERE category = ?
            AND active = 1
            ORDER BY id
            """,
            (q.data,),
        ).fetchall()

        c.close()

        kb = []

        for r in rows:

            kb.append([
                InlineKeyboardButton(
                    f"{r['name']} · {r['price']}",
                    callback_data=f"p:{r['id']}",
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
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 首页
    # -------------------------

    elif q.data == "home":

        await q.message.edit_text(
            "🛒 商品报价目录\n\n"
            "请选择商品系列：",
            reply_markup=home(),
        )

    # -------------------------
    # 搜索
    # -------------------------

    elif q.data == "search":

        ctx.user_data["mode"] = "search"

        await q.message.reply_text(
            "请输入商品名称或编号："
        )

    # -------------------------
    # 询价
    # -------------------------

    elif q.data == "inquiry":

        ctx.user_data["mode"] = "inquiry"
        ctx.user_data["product"] = ""

        await q.message.reply_text(
            "请输入商品、规格、数量或需求："
        )

    # -------------------------
    # 商品详情
    # -------------------------

    elif q.data.startswith("p:"):

        pid = int(q.data[2:])

        c = db()

        r = c.execute(
            """
            SELECT *
            FROM products
            WHERE id = ?
            AND active = 1
            """,
            (pid,),
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
            ],
        ]

        await q.message.reply_text(
            f"【{r['name']}】\n"
            f"编号：{r['code']}\n"
            f"价格：{r['price']}\n"
            f"库存：{r['stock']}\n\n"
            f"{r['description']}",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 商品询价
    # -------------------------

    elif q.data.startswith("iq:"):

        pid = int(q.data[3:])

        c = db()

        r = c.execute(
            """
            SELECT name
            FROM products
            WHERE id = ?
            """,
            (pid,),
        ).fetchone()

        c.close()

        ctx.user_data["mode"] = "inquiry"

        ctx.user_data["product"] = (
            r["name"] if r else ""
        )

        await q.message.reply_text(
            f"商品：{ctx.user_data['product']}\n\n"
            "请发送你的需求，例如：\n"
            "数量、规格、联系方式等"
        )


# =========================
# 用户文字消息
# =========================

async def user_text(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()

    mode = ctx.user_data.get("mode")

    # -------------------------
    # 搜索
    # -------------------------

    if mode == "search":

        ctx.user_data.clear()

        c = db()

        rows = c.execute(
            """
            SELECT id, name, price
            FROM products
            WHERE active = 1
            AND (
                name LIKE ?
                OR code LIKE ?
            )
            LIMIT 30
            """,
            (
                f"%{text}%",
                f"%{text}%",
            ),
        ).fetchall()

        c.close()

        if not rows:

            await update.message.reply_text(
                "没有找到结果。",
                reply_markup=home(),
            )

            return

        kb = []

        for r in rows:

            kb.append([
                InlineKeyboardButton(
                    f"{r['name']} · {r['price']}",
                    callback_data=f"p:{r['id']}",
                )
            ])

        await update.message.reply_text(
            "搜索结果：",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 询价
    # -------------------------

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
            (user_id, username, product, message)
            VALUES (?, ?, ?, ?)
            """,
            (
                u.id,
                u.username or "",
                product,
                text,
            ),
        )

        iid = cur.lastrowid

        c.commit()
        c.close()

        ctx.user_data.clear()

        await update.message.reply_text(
            f"✅ 询价已提交 #{iid}\n\n"
            "我们会尽快处理。"
        )

        # 通知管理员

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
# 管理后台
# =========================

async def admin(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    c = db()

    p = c.execute(
        "SELECT COUNT(*) n FROM products"
    ).fetchone()["n"]

    a = c.execute(
        """
        SELECT COUNT(*) n
        FROM products
        WHERE active = 1
        """
    ).fetchone()["n"]

    q = c.execute(
        """
        SELECT COUNT(*) n
        FROM inquiries
        WHERE status = 'new'
        """
    ).fetchone()["n"]

    c.close()

    kb = [
        [
            InlineKeyboardButton(
                "📦 商品",
                callback_data="a:products"
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
        [
            InlineKeyboardButton(
                "📥 批量导入CSV",
                callback_data="a:import"
            )
        ],
    ]

    await update.message.reply_text(
        f"⚙️ 管理后台\n\n"
        f"商品：{p}\n"
        f"上架：{a}\n"
        f"待处理询价：{q}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# =========================
# 管理员按钮
# =========================

async def admin_button(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    q = update.callback_query

    if q.from_user.id not in ADMINS:

        await q.answer()

        return

    await q.answer()

    # -------------------------
    # 商品管理
    # -------------------------

    if q.data == "a:products":

        c = db()

        counts = {}

        for cat, name in CATS.items():

            counts[cat] = c.execute(
                """
                SELECT COUNT(*) n
                FROM products
                WHERE category = ?
                """,
                (cat,),
            ).fetchone()["n"]

        c.close()

        ks = list(CATS.items())

        kb = []

        for i in range(0, 12, 2):

            kb.append([
                InlineKeyboardButton(
                    f"{CATS[ks[i][0]]}"
                    f"（{counts[ks[i][0]]}）",
                    callback_data=f"a:cat:{ks[i][0]}",
                ),
                InlineKeyboardButton(
                    f"{CATS[ks[i + 1][0]]}"
                    f"（{counts[ks[i + 1][0]]}）",
                    callback_data=f"a:cat:{ks[i + 1][0]}",
                ),
            ])

        kb.append([
            InlineKeyboardButton(
                "⬅️ 返回管理后台",
                callback_data="a:back"
            )
        ])

        await q.message.edit_text(
            "📦 商品管理\n\n"
            "请选择要查看的系列：",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 查看某个系列
    # -------------------------

    elif q.data.startswith("a:cat:"):

        cat = q.data.split(":", 2)[2]

        if cat not in CATS:
            return

        c = db()

        rows = c.execute(
            """
            SELECT id, name, code, price, stock, active
            FROM products
            WHERE category = ?
            ORDER BY id
            """,
            (cat,),
        ).fetchall()

        c.close()

        if not rows:

            text = (
                f"📦 {CATS[cat]}\n\n"
                "暂无商品。"
            )

        else:

            text = (
                f"📦 {CATS[cat]}"
                f"（{len(rows)}个）\n\n"
            )

            text += "\n".join(
                f"#{r['id']} {r['name']}\n"
                f"　{r['code']}｜"
                f"¥{r['price']}｜"
                f"库存{r['stock']}｜"
                f"{'上架' if r['active'] else '下架'}"
                for r in rows
            )

        kb = [[
            InlineKeyboardButton(
                "⬅️ 返回系列列表",
                callback_data="a:products"
            )
        ]]

        await q.message.edit_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 返回管理后台
    # -------------------------

    elif q.data == "a:back":

        c = db()

        p = c.execute(
            "SELECT COUNT(*) n FROM products"
        ).fetchone()["n"]

        a = c.execute(
            """
            SELECT COUNT(*) n
            FROM products
            WHERE active = 1
            """
        ).fetchone()["n"]

        nq = c.execute(
            """
            SELECT COUNT(*) n
            FROM inquiries
            WHERE status = 'new'
            """
        ).fetchone()["n"]

        c.close()

        kb = [
            [
                InlineKeyboardButton(
                    "📦 商品",
                    callback_data="a:products"
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
            [
                InlineKeyboardButton(
                    "📥 批量导入CSV",
                    callback_data="a:import"
                )
            ],
        ]

        await q.message.edit_text(
            f"⚙️ 管理后台\n\n"
            f"商品：{p}\n"
            f"上架：{a}\n"
            f"待处理询价：{nq}",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    # -------------------------
    # 询价管理
    # -------------------------

    elif q.data == "a:inq":

        c = db()

        rows = c.execute(
            """
            SELECT
                id,
                username,
                product,
                message,
                status,
                created_at
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
            f"#{r['id']} "
            f"@{r['username'] or '未设置'}\n"
            f"商品：{r['product']}\n"
            f"{r['message']}\n"
            f"[{r['status']}] "
            f"{r['created_at']}"
            for r in rows
        )

        await q.message.reply_text(
            text[:4000]
        )

    # -------------------------
    # 添加商品
    # -------------------------

    elif q.data == "a:add":

        ctx.user_data["admin_mode"] = "add"

        await q.message.reply_text(
            "按以下格式发送：\n\n"
            "名称|编号|分类(c1-c12)|价格|库存|描述\n\n"
            "例如：\n"
            "测试商品|P001|c1|350|10|商品介绍"
        )

    # -------------------------
    # CSV导入
    # -------------------------

    elif q.data == "a:import":

        ctx.user_data["admin_mode"] = "import_csv"

        await q.message.reply_text(
            "📥 请直接发送商品CSV文件。\n\n"
            "支持字段：\n"
            "id,name,code,category,price,"
            "price_type,stock,description,active\n\n"
            "导入时按 code 更新已有商品，"
            "不会重复创建同编号商品。"
        )


# =========================
# 管理员添加商品
# =========================

async def admin_text(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    if ctx.user_data.get("admin_mode") != "add":
        return

    parts = [
        x.strip()
        for x in update.message.text.split("|")
    ]

    if len(parts) != 6:

        await update.message.reply_text(
            "格式错误，请按：\n"
            "名称|编号|分类|价格|库存|描述"
        )

        return

    name, code, cat, price, stock, desc = parts

    # 支持分类编号
    # 也支持直接填写系列名称

    cat_by_name = {
        v: k
        for k, v in CATS.items()
    }

    if cat not in CATS:

        cat = cat_by_name.get(cat, "")

    if not cat:

        await update.message.reply_text(
            "分类必须填写 c1-c12，"
            "或者直接填写系列名称。"
        )

        return

    try:

        stock = int(stock)

    except:

        await update.message.reply_text(
            "库存必须是数字。"
        )

        return

    c = db()

    try:

        c.execute(
            """
            INSERT INTO products
            (name, code, category, price, stock, description)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                code,
                cat,
                price,
                stock,
                desc,
            ),
        )

        c.commit()

        await update.message.reply_text(
            "✅ 商品已添加。"
        )

    except sqlite3.IntegrityError:

        await update.message.reply_text(
            "❌ 编号已存在。"
        )

    finally:

        c.close()

    ctx.user_data.clear()


# =========================
# CSV导入
# =========================

async def admin_document(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id not in ADMINS:
        return

    if ctx.user_data.get("admin_mode") != "import_csv":
        return

    doc = update.message.document

    if not doc:
        return

    if not doc.file_name.lower().endswith(".csv"):

        await update.message.reply_text(
            "请发送 .csv 文件。"
        )

        return

    f = await doc.get_file()

    data = await f.download_as_bytearray()

    try:

        text = data.decode("utf-8-sig")

    except UnicodeDecodeError:

        text = data.decode(
            "gb18030",
            errors="replace"
        )

    reader = csv.DictReader(
        io.StringIO(text)
    )

    required = {
        "name",
        "code",
        "category",
        "price",
        "stock",
    }

    if not required.issubset(
        set(reader.fieldnames or [])
    ):

        await update.message.reply_text(
            "CSV字段不完整，需要至少包含：\n"
            "name, code, category, price, stock"
        )

        return

    cat_by_name = {
        v: k
        for k, v in CATS.items()
    }

    c = db()

    added = 0
    updated = 0
    failed = 0

    # 删除以前的示例商品
    c.execute(
        """
        DELETE FROM products
        WHERE code IN ('A001','B001')
        AND name IN ('示例商品A','示例商品B')
        """
    )

    for row in reader:

        try:

            name = (
                row.get("name") or ""
            ).strip()

            code = (
                row.get("code") or ""
            ).strip()

            cat = (
                row.get("category") or ""
            ).strip()

            price = (
                row.get("price")
                or "询价"
            ).strip()

            stock = int(
                (
                    row.get("stock")
                    or "0"
                ).strip()
            )

            desc = (
                row.get("description")
                or ""
            ).strip()

            active = (
                1
                if str(
                    row.get("active")
                    or "1"
                ).strip()
                not in (
                    "0",
                    "下架",
                    "false",
                    "False",
                )
                else 0
            )

            # 如果填写系列名称，转换成c1-c12
            cat = (
                cat
                if cat in CATS
                else cat_by_name.get(cat, "")
            )

            if not name or not code or not cat:
                raise ValueError(
                    "名称、编号或分类为空"
                )

            old = c.execute(
                """
                SELECT id
                FROM products
                WHERE code = ?
                """,
                (code,),
            ).fetchone()

            if old:

                c.execute(
                    """
                    UPDATE products
                    SET
                        name = ?,
                        category = ?,
                        price = ?,
                        stock = ?,
                        description = ?,
                        active = ?
                    WHERE code = ?
                    """,
                    (
                        name,
                        cat,
                        price,
                        stock,
                        desc,
                        active,
                        code,
                    ),
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
                        active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        code,
                        cat,
                        price,
                        stock,
                        desc,
                        active,
                    ),
                )

                added += 1

        except Exception:

            failed += 1

    c.commit()
    c.close()

    ctx.user_data.clear()

    await update.message.reply_text(
        f"✅ 批量导入完成\n\n"
        f"新增：{added}\n"
        f"更新：{updated}\n"
        f"失败：{failed}"
    )


# =========================
# 群里发布商品目录按钮
# =========================

async def post_catalog(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE
):

    # 只有管理员可以发布
    if update.effective_user.id not in ADMINS:
        return

    keyboard = [[
        InlineKeyboardButton(
            "🛒 查看商品目录",
            url="https://t.me/shangpinmulu2026_bot?start=catalog"
        )
    ]]

    await update.message.reply_text(
        "🛒 商品报价目录\n\n"
        "点击下面按钮，进入商品目录查看全部商品：",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================
# 文字消息路由
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
# 主程序
# =========================

def main():

    if not TOKEN:

        raise RuntimeError(
            "请在 .env 设置 BOT_TOKEN"
        )

    init()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # 用户
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # 管理后台
    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    # 群里发布商品目录按钮
    app.add_handler(
        CommandHandler(
            "post",
            post_catalog
        )
    )

    # 管理员按钮
    app.add_handler(
        CallbackQueryHandler(
            admin_button,
            pattern=r"^a:"
        )
    )

    # 用户按钮
    app.add_handler(
        CallbackQueryHandler(
            user_button
        )
    )

    # CSV文件
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            admin_document
        )
    )

    # 普通文字
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    # Render
    if os.getenv("RENDER"):

        port = int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

        base_url = (
            os.getenv(
                "RENDER_EXTERNAL_URL",
                ""
            )
            .rstrip("/")
        )

        if not base_url:

            raise RuntimeError(
                "Render 环境缺少 "
                "RENDER_EXTERNAL_URL"
            )

        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=(
                f"{base_url}/telegram"
            ),
        )

    else:

        app.run_polling()


# =========================
# 启动
# =========================

if __name__ == "__main__":
    main()
