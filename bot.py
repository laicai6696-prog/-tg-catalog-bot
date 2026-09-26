import os, sqlite3, logging, csv, re, zipfile, tempfile, shutil, asyncio
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB = "bot.db"
logging.basicConfig(level=logging.INFO)

CATS = {
    "c1": "和天下系列", "c2": "熊猫系列", "c3": "南京系列", "c4": "荷花系列",
    "c5": "芙蓉王系列", "c6": "牡丹系列", "c7": "黄金叶系列", "c8": "苏烟系列",
    "c9": "利群系列", "c10": "黄鹤楼系列", "c11": "中华系列", "c12": "白皮系列",
}
CAT_BY_NAME = {v: k for k, v in CATS.items()}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        price TEXT DEFAULT '询价',
        stock INTEGER DEFAULT 0,
        description TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        photo_id TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS inquiries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        product TEXT,
        message TEXT,
        status TEXT DEFAULT 'new',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Upgrade an older database without deleting existing products.
    cols = {r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    if "photo_id" not in cols:
        c.execute("ALTER TABLE products ADD COLUMN photo_id TEXT DEFAULT ''")
    c.commit()
    c.close()


def home():
    items = list(CATS.items())
    rows = []
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(CATS[items[i][0]], callback_data=items[i][0])]
        if i + 1 < len(items):
            row.append(InlineKeyboardButton(CATS[items[i + 1][0]], callback_data=items[i + 1][0]))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔎 搜索", callback_data="search"),
        InlineKeyboardButton("📩 询价", callback_data="inquiry")
    ])
    return InlineKeyboardMarkup(rows)


def admin_home(p, a, q):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 商品管理", callback_data="a:products")],
        [InlineKeyboardButton("📦 一键上传商品+图片", callback_data="a:importpkg")],
        [InlineKeyboardButton("📥 只导入CSV", callback_data="a:importcsv")],
        [InlineKeyboardButton("📩 询价", callback_data="a:inq")],
        [InlineKeyboardButton("➕ 添加商品", callback_data="a:add")],
    ])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("欢迎使用商品目录\n请选择分类：", reply_markup=home())


async def user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data in CATS:
        c = db()
        rows = c.execute("SELECT id,name,price FROM products WHERE category=? AND active=1 ORDER BY id", (q.data,)).fetchall()
        c.close()
        kb = [[InlineKeyboardButton(f"{r['name']} · ¥{r['price']}" if str(r['price']).isdigit() else f"{r['name']} · {r['price']}", callback_data=f"p:{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ 首页", callback_data="home")])
        await q.message.edit_text(f"【{CATS[q.data]}】", reply_markup=InlineKeyboardMarkup(kb))
    elif q.data == "home":
        await q.message.edit_text("请选择分类：", reply_markup=home())
    elif q.data == "search":
        ctx.user_data["mode"] = "search"
        await q.message.reply_text("请输入商品名称或编号：")
    elif q.data == "inquiry":
        ctx.user_data["mode"] = "inquiry"
        await q.message.reply_text("请输入商品、规格、数量或需求：")
    elif q.data.startswith("p:"):
        pid = int(q.data[2:])
        c = db(); r = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone(); c.close()
        if not r:
            return
        kb = [[InlineKeyboardButton("📩 提交询价", callback_data=f"iq:{pid}")],
              [InlineKeyboardButton("⬅️ 返回", callback_data=r["category"])]]
        text = f"【{r['name']}】\n编号：{r['code']}\n价格：{r['price']}\n库存：{r['stock']}\n\n{r['description']}"
        if r["photo_id"]:
            try:
                await q.message.reply_photo(photo=r["photo_id"], caption=text, reply_markup=InlineKeyboardMarkup(kb))
            except Exception:
                await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("iq:"):
        pid = int(q.data[3:]); c = db(); r = c.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone(); c.close()
        ctx.user_data["mode"] = "inquiry"
        ctx.user_data["product"] = r["name"] if r else ""
        await q.message.reply_text(f"商品：{ctx.user_data['product']}\n请发送需求：")


async def user_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = ctx.user_data.get("mode")
    if mode == "search":
        ctx.user_data.clear(); c = db()
        rows = c.execute("SELECT id,name,price FROM products WHERE active=1 AND (name LIKE ? OR code LIKE ?) LIMIT 30", (f"%{text}%", f"%{text}%")).fetchall(); c.close()
        if not rows:
            await update.message.reply_text("没有找到结果。", reply_markup=home()); return
        kb = [[InlineKeyboardButton(f"{r['name']} · {r['price']}", callback_data=f"p:{r['id']}")] for r in rows]
        await update.message.reply_text("搜索结果：", reply_markup=InlineKeyboardMarkup(kb))
    elif mode == "inquiry":
        product = ctx.user_data.get("product", "")
        u = update.effective_user; c = db()
        cur = c.execute("INSERT INTO inquiries(user_id,username,product,message) VALUES(?,?,?,?)", (u.id, u.username or "", product, text)); iid = cur.lastrowid
        c.commit(); c.close(); ctx.user_data.clear()
        await update.message.reply_text(f"询价已提交 #{iid}")
        for aid in ADMINS:
            try:
                await ctx.bot.send_message(aid, f"📩 新询价 #{iid}\n用户：@{u.username or '未设置'}\nID：{u.id}\n商品：{product}\n内容：{text}")
            except Exception:
                pass


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    c = db(); p = c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]; a = c.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]; q = c.execute("SELECT COUNT(*) n FROM inquiries WHERE status='new'").fetchone()["n"]; c.close()
    await update.message.reply_text(f"管理后台\n商品 {p}｜上架 {a}\n待处理询价 {q}", reply_markup=admin_home(p, a, q))


async def admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer(); return
    await q.answer()
    if q.data == "a:products":
        c = db(); rows = c.execute("SELECT id,name,code,price,stock,active,photo_id FROM products ORDER BY id").fetchall(); c.close()
        # Split into category buttons so the list stays manageable.
        kb = [[InlineKeyboardButton(CATS[k], callback_data=f"a:cat:{k}")] for k in CATS]
        kb.append([InlineKeyboardButton("⬅️ 后台", callback_data="a:back")])
        await q.message.reply_text(f"📦 商品管理\n共 {len(rows)} 个商品\n请选择系列：", reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("a:cat:"):
        cat = q.data.split(":", 2)[2]
        c = db(); rows = c.execute("SELECT id,name,price,photo_id FROM products WHERE category=? ORDER BY id", (cat,)).fetchall(); c.close()
        kb = [[InlineKeyboardButton(("🖼️ " if r["photo_id"] else "▫️ ") + r["name"], callback_data=f"a:prod:{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ 商品管理", callback_data="a:products")])
        await q.message.reply_text(f"【{CATS.get(cat,cat)}】\n点击商品可管理图片：", reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("a:prod:"):
        pid = int(q.data.split(":")[2]); c = db(); r = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone(); c.close()
        if not r: return
        buttons = [[InlineKeyboardButton("🖼️ 添加/更换图片", callback_data=f"a:setphoto:{pid}")]]
        if r["photo_id"]:
            buttons.append([InlineKeyboardButton("👁️ 查看图片", callback_data=f"a:viewphoto:{pid}"), InlineKeyboardButton("🗑️ 删除图片", callback_data=f"a:delphoto:{pid}")])
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"a:cat:{r['category']}")])
        await q.message.reply_text(f"商品：{r['name']}\n编号：{r['code']}\n图片：{'已绑定' if r['photo_id'] else '未绑定'}", reply_markup=InlineKeyboardMarkup(buttons))
    elif q.data.startswith("a:setphoto:"):
        pid = int(q.data.split(":")[2]); ctx.user_data["admin_mode"] = "set_photo"; ctx.user_data["photo_product_id"] = pid
        await q.message.reply_text("请发送这件商品的图片。\n支持 JPG、PNG、WEBP。")
    elif q.data.startswith("a:viewphoto:"):
        pid = int(q.data.split(":")[2]); c = db(); r = c.execute("SELECT name,photo_id FROM products WHERE id=?", (pid,)).fetchone(); c.close()
        if r and r["photo_id"]:
            await q.message.reply_photo(r["photo_id"], caption=r["name"])
    elif q.data.startswith("a:delphoto:"):
        pid = int(q.data.split(":")[2]); c = db(); c.execute("UPDATE products SET photo_id='' WHERE id=?", (pid,)); c.commit(); c.close(); await q.message.reply_text("✅ 图片已删除。")
    elif q.data == "a:importpkg":
        ctx.user_data["admin_mode"] = "import_package"
        await q.message.reply_text("📦 一键上传商品+图片\n\n请把【商品表CSV + 01~93图片】放进同一个ZIP，然后把ZIP文件发送给我。\n\n对应关系：01 → P001，02 → P002……93 → P093。\n\n我会自动新增/更新商品并绑定图片，无需一张张上传。")
    elif q.data == "a:importcsv":
        ctx.user_data["admin_mode"] = "import_csv"
        await q.message.reply_text("请发送93个商品的CSV文件。")
    elif q.data == "a:inq":
        c = db(); rows = c.execute("SELECT id,username,product,message,status,created_at FROM inquiries ORDER BY id DESC LIMIT 30").fetchall(); c.close()
        if not rows: await q.message.reply_text("暂无询价"); return
        text = "\n\n".join(f"#{r['id']} @{r['username'] or '未设置'}\n商品：{r['product']}\n{r['message']}\n[{r['status']}] {r['created_at']}" for r in rows)
        await q.message.reply_text(text[:4000])
    elif q.data == "a:add":
        ctx.user_data["admin_mode"] = "add_photo"
        await q.message.reply_text("第一步：请先发送商品图片。\n如果暂时没有图片，请发送：跳过")
    elif q.data == "a:back":
        await admin(update, ctx)


async def save_photo_file_id(bot, admin_id, path):
    # Telegram file_id is obtained by sending the image once. Delete the temporary admin message afterward.
    with open(path, "rb") as f:
        msg = await bot.send_photo(chat_id=admin_id, photo=f)
    photo_id = msg.photo[-1].file_id
    try:
        await bot.delete_message(chat_id=admin_id, message_id=msg.message_id)
    except Exception:
        pass
    return photo_id


async def process_package(update: Update, ctx: ContextTypes.DEFAULT_TYPE, zip_path: str):
    admin_id = update.effective_user.id
    temp = Path(tempfile.mkdtemp(prefix="tg_import_"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = [n for n in z.namelist() if Path(n).is_absolute() or ".." in Path(n).parts]
            if bad:
                await update.message.reply_text("❌ ZIP文件包含不安全路径，已停止导入。"); return
            z.extractall(temp)
        csvs = list(temp.rglob("*.csv"))
        if not csvs:
            await update.message.reply_text("❌ ZIP里没有CSV商品表。"); return
        csv_file = csvs[0]
        image_map = {}
        for p in temp.rglob("*"):
            if not p.is_file(): continue
            m = re.match(r"^(\d{1,3})(?:\.(?:jpg|jpeg|png|webp))+$", p.name, re.I)
            if m:
                n = int(m.group(1))
                if 1 <= n <= 999:
                    image_map[n] = p
        with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        required = {"name", "code", "category", "price", "stock"}
        if not rows or not required.issubset(set(rows[0].keys())):
            await update.message.reply_text("❌ CSV格式不正确，需要：name,code,category,price,stock"); return
        if len(rows) != 93:
            await update.message.reply_text(f"⚠️ CSV当前有 {len(rows)} 个商品，不是预期的93个。为避免误导入，已停止。")
            return
        missing = [n for n in range(1, 94) if n not in image_map]
        if missing:
            await update.message.reply_text("❌ 图片不完整，缺少：" + ", ".join(f"{n:02d}" for n in missing)); return
        c = db(); inserted = updated = 0
        try:
            for row in rows:
                code = row["code"].strip(); name = row["name"].strip(); cat_raw = row["category"].strip(); cat = CAT_BY_NAME.get(cat_raw, cat_raw)
                if cat not in CATS: raise ValueError(f"商品 {code} 的分类无效：{cat_raw}")
                price = row["price"].strip(); stock = int(float(row["stock"].strip() or 0)); desc = row.get("description", "").strip()
                exists = c.execute("SELECT id FROM products WHERE code=?", (code,)).fetchone()
                if exists:
                    c.execute("UPDATE products SET name=?,category=?,price=?,stock=?,description=?,active=1 WHERE code=?", (name,cat,price,stock,desc,code)); updated += 1
                else:
                    c.execute("INSERT INTO products(name,code,category,price,stock,description,active,photo_id) VALUES(?,?,?,?,?,?,1,?)", (name,code,cat,price,stock,desc,"")); inserted += 1
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            c.close()
        await update.message.reply_text(f"📥 商品表已导入：新增 {inserted}，更新 {updated}\n🖼️ 开始绑定93张图片，请稍候……")
        photo_ok = 0
        photo_fail = []
        for n in range(1, 94):
            code = f"P{n:03d}"
            path = image_map[n]
            try:
                file_id = await save_photo_file_id(ctx.bot, admin_id, path)
                c = db(); c.execute("UPDATE products SET photo_id=? WHERE code=?", (file_id, code)); c.commit(); c.close()
                photo_ok += 1
                await asyncio.sleep(0.15)
            except Exception as e:
                logging.exception("photo import failed %s", code)
                photo_fail.append(code)
        result = f"✅ 一键上传完成！\n\n商品：新增 {inserted}｜更新 {updated}\n图片：成功 {photo_ok}｜失败 {len(photo_fail)}"
        if photo_fail:
            result += "\n失败图片：" + ", ".join(photo_fail)
        await update.message.reply_text(result)
    except Exception as e:
        logging.exception("package import failed")
        await update.message.reply_text(f"❌ 导入失败：{e}")
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        try: os.remove(zip_path)
        except Exception: pass
        ctx.user_data.clear()


async def admin_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS: return
    mode = ctx.user_data.get("admin_mode")
    if mode not in {"import_package", "import_csv"}: return
    doc = update.message.document
    name = (doc.file_name or "").lower()
    if mode == "import_package":
        if not name.endswith(".zip"):
            await update.message.reply_text("请发送ZIP压缩包。")
            return
        f = await doc.get_file()
        path = os.path.join(tempfile.gettempdir(), f"tg_package_{update.effective_user.id}.zip")
        await f.download_to_drive(path)
        await process_package(update, ctx, path)
    else:
        if not name.endswith(".csv"):
            await update.message.reply_text("请发送CSV文件。")
            return
        f = await doc.get_file(); path = os.path.join(tempfile.gettempdir(), f"tg_csv_{update.effective_user.id}.csv"); await f.download_to_drive(path)
        # Reuse package logic only when a ZIP is supplied; simple CSV update here.
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fp: rows = list(csv.DictReader(fp))
            c = db(); new = upd = 0
            for row in rows:
                cat = CAT_BY_NAME.get(row["category"].strip(), row["category"].strip()); stock = int(float(row.get("stock",0) or 0)); code=row["code"].strip()
                ex=c.execute("SELECT id FROM products WHERE code=?",(code,)).fetchone()
                if ex:
                    c.execute("UPDATE products SET name=?,category=?,price=?,stock=?,description=?,active=1 WHERE code=?",(row["name"].strip(),cat,row["price"].strip(),stock,row.get("description","").strip(),code)); upd+=1
                else:
                    c.execute("INSERT INTO products(name,code,category,price,stock,description,active,photo_id) VALUES(?,?,?,?,?,?,1,'')",(row["name"].strip(),code,cat,row["price"].strip(),stock,row.get("description","").strip())); new+=1
            c.commit(); c.close(); await update.message.reply_text(f"✅ CSV导入完成：新增 {new}｜更新 {upd}\n已有图片不会被覆盖。")
        except Exception as e:
            await update.message.reply_text(f"❌ CSV导入失败：{e}")
        finally:
            try: os.remove(path)
            except Exception: pass
            ctx.user_data.clear()


async def admin_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS: return
    mode = ctx.user_data.get("admin_mode")
    if mode == "set_photo":
        pid = ctx.user_data.get("photo_product_id")
        if not pid: return
        file_id = update.message.photo[-1].file_id
        c = db(); c.execute("UPDATE products SET photo_id=? WHERE id=?", (file_id,pid)); c.commit(); r=c.execute("SELECT name FROM products WHERE id=?",(pid,)).fetchone(); c.close()
        ctx.user_data.clear(); await update.message.reply_text(f"✅ 已绑定图片：{r['name'] if r else pid}")
    elif mode == "add_photo":
        ctx.user_data["new_photo_id"] = update.message.photo[-1].file_id
        ctx.user_data["admin_mode"] = "add"
        await update.message.reply_text("图片已收到。\n第二步请发送：\n名称|编号|分类(c1-c12)|价格|库存|描述")


async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS: return
    mode = ctx.user_data.get("admin_mode")
    if mode == "add_photo" and update.message.text.strip() == "跳过":
        ctx.user_data["new_photo_id"] = ""; ctx.user_data["admin_mode"] = "add"
        await update.message.reply_text("已跳过图片。\n请发送：名称|编号|分类(c1-c12)|价格|库存|描述"); return
    if mode != "add": return
    parts = [x.strip() for x in update.message.text.split("|")]
    if len(parts) != 6:
        await update.message.reply_text("格式错误，请按：名称|编号|分类|价格|库存|描述"); return
    name, code, cat, price, stock, desc = parts
    if cat not in CATS:
        await update.message.reply_text("分类必须是 c1-c12"); return
    try: stock = int(stock)
    except: await update.message.reply_text("库存必须是数字"); return
    photo_id = ctx.user_data.get("new_photo_id", "")
    c = db()
    try:
        c.execute("INSERT INTO products(name,code,category,price,stock,description,photo_id) VALUES(?,?,?,?,?,?,?)", (name,code,cat,price,stock,desc,photo_id)); c.commit(); await update.message.reply_text("✅ 商品已添加。")
    except sqlite3.IntegrityError: await update.message.reply_text("编号已存在。")
    finally: c.close()
    ctx.user_data.clear()


async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMINS and ctx.user_data.get("admin_mode"):
        await admin_text(update, ctx)
    else:
        await user_text(update, ctx)


def main():
    if not TOKEN: raise RuntimeError("请在 Render Environment 设置 BOT_TOKEN")
    init()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(admin_button, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(user_button))
    app.add_handler(MessageHandler(filters.Document.ALL, admin_document))
    app.add_handler(MessageHandler(filters.PHOTO, admin_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    # Render: use webhook when deployed on Render; otherwise polling.
    if os.getenv("RENDER"):
        port = int(os.getenv("PORT", "10000"))
        base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("Render环境缺少 RENDER_EXTERNAL_URL")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{base_url}/telegram"
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
