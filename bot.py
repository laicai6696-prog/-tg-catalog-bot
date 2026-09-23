import os, sqlite3, logging, csv, io
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
DB = "bot.db"
logging.basicConfig(level=logging.INFO)

CATS = {
    "c1":"和天下系列", "c2":"熊猫系列", "c3":"南京系列",
    "c4":"荷花系列", "c5":"芙蓉王系列", "c6":"牡丹系列",
    "c7":"黄金叶系列", "c8":"苏烟系列", "c9":"利群系列",
    "c10":"黄鹤楼系列", "c11":"中华系列", "c12":"白皮系列"
}

def db():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init():
    c=db()
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        code TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        price TEXT DEFAULT '询价',
        stock INTEGER DEFAULT 0,
        description TEXT DEFAULT '',
        active INTEGER DEFAULT 1
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
    if c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]==0:
        c.executemany(
            "INSERT INTO products(name,code,category,price,stock,description) VALUES(?,?,?,?,?,?)",
            [("示例商品A","A001","c1","询价",10,"商品介绍"),
             ("示例商品B","B001","c2","询价",20,"商品介绍")]
        )
    c.commit(); c.close()

def home():
    ks=list(CATS.items())
    rows=[]
    for i in range(0,12,2):
        rows.append([InlineKeyboardButton(CATS[ks[i][0]],callback_data=ks[i][0]),
                     InlineKeyboardButton(CATS[ks[i+1][0]],callback_data=ks[i+1][0])])
    rows += [
        [InlineKeyboardButton("🔎 搜索",callback_data="search"),
         InlineKeyboardButton("📩 询价",callback_data="inquiry")],
    ]
    return InlineKeyboardMarkup(rows)

async def start(update,ctx):
    ctx.user_data.clear()
    await update.message.reply_text("欢迎使用商品目录\n请选择分类：",reply_markup=home())

async def user_button(update,ctx):
    q=update.callback_query; await q.answer()
    if q.data in CATS:
        c=db(); rows=c.execute(
            "SELECT id,name,price FROM products WHERE category=? AND active=1 ORDER BY id",
            (q.data,)).fetchall(); c.close()
        kb=[[InlineKeyboardButton(f"{r['name']} · {r['price']}",callback_data=f"p:{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ 首页",callback_data="home")])
        await q.message.edit_text(f"【{CATS[q.data]}】",reply_markup=InlineKeyboardMarkup(kb))
    elif q.data=="home":
        await q.message.edit_text("请选择分类：",reply_markup=home())
    elif q.data=="search":
        ctx.user_data["mode"]="search"
        await q.message.reply_text("请输入关键词：")
    elif q.data=="inquiry":
        ctx.user_data["mode"]="inquiry"
        await q.message.reply_text("请输入商品、规格、数量或需求：")
    elif q.data.startswith("p:"):
        pid=int(q.data[2:]); c=db()
        r=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone(); c.close()
        if not r:return
        kb=[[InlineKeyboardButton("📩 提交询价",callback_data=f"iq:{pid}")],
            [InlineKeyboardButton("⬅️ 返回",callback_data=r["category"])] ]
        await q.message.reply_text(
            f"【{r['name']}】\n编号：{r['code']}\n价格：{r['price']}\n库存：{r['stock']}\n\n{r['description']}",
            reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("iq:"):
        pid=int(q.data[3:]); c=db(); r=c.execute("SELECT name FROM products WHERE id=?",(pid,)).fetchone(); c.close()
        ctx.user_data["mode"]="inquiry"
        ctx.user_data["product"]=r["name"] if r else ""
        await q.message.reply_text(f"商品：{ctx.user_data['product']}\n请发送需求：")

async def user_text(update,ctx):
    text=update.message.text.strip()
    mode=ctx.user_data.get("mode")
    if mode=="search":
        ctx.user_data.clear(); c=db()
        rows=c.execute("SELECT id,name,price FROM products WHERE active=1 AND (name LIKE ? OR code LIKE ?) LIMIT 30",
                       (f"%{text}%",f"%{text}%")).fetchall(); c.close()
        if not rows:
            await update.message.reply_text("没有找到结果。",reply_markup=home()); return
        kb=[[InlineKeyboardButton(f"{r['name']} · {r['price']}",callback_data=f"p:{r['id']}")] for r in rows]
        await update.message.reply_text("搜索结果：",reply_markup=InlineKeyboardMarkup(kb))
    elif mode=="inquiry":
        product=ctx.user_data.get("product","")
        u=update.effective_user; c=db()
        cur=c.execute("INSERT INTO inquiries(user_id,username,product,message) VALUES(?,?,?,?)",
                      (u.id,u.username or "",product,text)); iid=cur.lastrowid
        c.commit(); c.close(); ctx.user_data.clear()
        await update.message.reply_text(f"询价已提交 #{iid}")
        for aid in ADMINS:
            try:
                await ctx.bot.send_message(aid,f"📩 新询价 #{iid}\n用户：@{u.username or '未设置'}\nID：{u.id}\n商品：{product}\n内容：{text}")
            except Exception: pass

async def admin(update,ctx):
    if update.effective_user.id not in ADMINS:return
    c=db()
    p=c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
    a=c.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]
    q=c.execute("SELECT COUNT(*) n FROM inquiries WHERE status='new'").fetchone()["n"]
    c.close()
    kb=[[InlineKeyboardButton("📦 商品",callback_data="a:products")],
        [InlineKeyboardButton("📩 询价",callback_data="a:inq")],
        [InlineKeyboardButton("➕ 添加商品",callback_data="a:add")],
        [InlineKeyboardButton("📥 批量导入CSV",callback_data="a:import")]]
    await update.message.reply_text(f"管理后台\n商品 {p}｜上架 {a}\n待处理询价 {q}",reply_markup=InlineKeyboardMarkup(kb))

async def admin_button(update,ctx):
    q=update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer(); return
    await q.answer()

    # 商品：先显示12个系列，不再一次性列出全部商品
    if q.data=="a:products":
        c=db()
        counts={}
        for cat,name in CATS.items():
            counts[cat]=c.execute(
                "SELECT COUNT(*) n FROM products WHERE category=?",
                (cat,)
            ).fetchone()["n"]
        c.close()

        ks=list(CATS.items())
        kb=[]
        for i in range(0,12,2):
            kb.append([
                InlineKeyboardButton(
                    f"{CATS[ks[i][0]]}（{counts[ks[i][0]]}）",
                    callback_data=f"a:cat:{ks[i][0]}"
                ),
                InlineKeyboardButton(
                    f"{CATS[ks[i+1][0]]}（{counts[ks[i+1][0]]}）",
                    callback_data=f"a:cat:{ks[i+1][0]}"
                )
            ])
        kb.append([InlineKeyboardButton("⬅️ 返回管理后台",callback_data="a:back")])
        await q.message.edit_text(
            "📦 商品管理\n\n请选择要查看的系列：",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    # 点击某个系列后，只显示该系列商品
    elif q.data.startswith("a:cat:"):
        cat=q.data.split(":",2)[2]
        if cat not in CATS:
            return
        c=db()
        rows=c.execute(
            "SELECT id,name,code,price,stock,active FROM products WHERE category=? ORDER BY id",
            (cat,)
        ).fetchall()
        c.close()

        if not rows:
            text=f"📦 {CATS[cat]}\n\n暂无商品。"
        else:
            text=f"📦 {CATS[cat]}（{len(rows)}个）\n\n"
            text += "\n".join(
                f"#{r['id']} {r['name']}\n"
                f"　{r['code']}｜¥{r['price']}｜库存{r['stock']}｜{'上架' if r['active'] else '下架'}"
                for r in rows
            )

        kb=[[InlineKeyboardButton("⬅️ 返回系列列表",callback_data="a:products")]]
        await q.message.edit_text(text[:4000],reply_markup=InlineKeyboardMarkup(kb))

    # 返回管理后台
    elif q.data=="a:back":
        c=db()
        p=c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
        a=c.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]
        nq=c.execute("SELECT COUNT(*) n FROM inquiries WHERE status='new'").fetchone()["n"]
        c.close()
        kb=[[InlineKeyboardButton("📦 商品",callback_data="a:products")],
            [InlineKeyboardButton("📩 询价",callback_data="a:inq")],
            [InlineKeyboardButton("➕ 添加商品",callback_data="a:add")],
            [InlineKeyboardButton("📥 批量导入CSV",callback_data="a:import")]]
        await q.message.edit_text(
            f"管理后台\n商品 {p}｜上架 {a}\n待处理询价 {nq}",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif q.data=="a:inq":
        c=db(); rows=c.execute("SELECT id,username,product,message,status,created_at FROM inquiries ORDER BY id DESC LIMIT 30").fetchall(); c.close()
        if not rows:
            await q.message.reply_text("暂无询价"); return
        text="\n\n".join(f"#{r['id']} @{r['username'] or '未设置'}\n商品：{r['product']}\n{r['message']}\n[{r['status']}] {r['created_at']}" for r in rows)
        await q.message.reply_text(text[:4000])
    elif q.data=="a:add":
        ctx.user_data["admin_mode"]="add"
        await q.message.reply_text("按以下格式发送：\n名称|编号|分类(c1-c12)|价格|库存|描述")
    elif q.data=="a:import":
        ctx.user_data["admin_mode"]="import_csv"
        await q.message.reply_text(
            "请直接发送商品CSV文件。\n\n支持字段：id,name,code,category,price,price_type,stock,description,active\n\n导入时按 code 更新已有商品，不会重复创建同编号商品。"
        )

async def admin_text(update,ctx):
    if update.effective_user.id not in ADMINS:return
    if ctx.user_data.get("admin_mode")!="add":return
    parts=[x.strip() for x in update.message.text.split("|")]
    if len(parts)!=6:
        await update.message.reply_text("格式错误，请按：名称|编号|分类|价格|库存|描述"); return
    name,code,cat,price,stock,desc=parts
    if cat not in CATS:
        await update.message.reply_text("分类必须是 c1-c12，或填写系列名称"); return
    try: stock=int(stock)
    except: await update.message.reply_text("库存必须是数字"); return
    c=db()
    try:
        c.execute("INSERT INTO products(name,code,category,price,stock,description) VALUES(?,?,?,?,?,?)",
                  (name,code,cat,price,stock,desc)); c.commit()
        await update.message.reply_text("商品已添加。")
    except sqlite3.IntegrityError:
        await update.message.reply_text("编号已存在。")
    finally:
        c.close()
    ctx.user_data.clear()

async def admin_document(update,ctx):
    if update.effective_user.id not in ADMINS:return
    if ctx.user_data.get("admin_mode")!="import_csv":return
    doc=update.message.document
    if not doc or not doc.file_name.lower().endswith(".csv"):
        await update.message.reply_text("请发送 .csv 文件。")
        return
    f=await doc.get_file()
    data=await f.download_as_bytearray()
    try:
        text=data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text=data.decode("gb18030",errors="replace")
    reader=csv.DictReader(io.StringIO(text))
    required={"name","code","category","price","stock"}
    if not required.issubset(set(reader.fieldnames or [])):
        await update.message.reply_text("CSV字段不完整，需要至少包含：name, code, category, price, stock")
        return
    cat_by_name={v:k for k,v in CATS.items()}
    c=db(); added=updated=failed=0
    # 清理原模板中的两个演示商品，避免首次导入后出现 95 个商品
    c.execute("DELETE FROM products WHERE code IN ('A001','B001') AND name IN ('示例商品A','示例商品B')")
    for row in reader:
        try:
            name=(row.get("name") or "").strip()
            code=(row.get("code") or "").strip()
            cat=(row.get("category") or "").strip()
            price=(row.get("price") or "询价").strip()
            stock=int((row.get("stock") or "0").strip())
            desc=(row.get("description") or "").strip()
            active=1 if str(row.get("active") or "1").strip() not in ("0","下架","false","False") else 0
            cat=cat if cat in CATS else cat_by_name.get(cat,"")
            if not name or not code or not cat:
                raise ValueError("名称、编号或分类为空")
            old=c.execute("SELECT id FROM products WHERE code=?",(code,)).fetchone()
            if old:
                c.execute(
                    "UPDATE products SET name=?,category=?,price=?,stock=?,description=?,active=? WHERE code=?",
                    (name,cat,price,stock,desc,active,code)
                )
                updated+=1
            else:
                c.execute(
                    "INSERT INTO products(name,code,category,price,stock,description,active) VALUES(?,?,?,?,?,?,?)",
                    (name,code,cat,price,stock,desc,active)
                )
                added+=1
        except Exception:
            failed+=1
    c.commit(); c.close(); ctx.user_data.clear()
    await update.message.reply_text(f"批量导入完成\n新增：{added}\n更新：{updated}\n失败：{failed}")

async def text_router(update,ctx):
    if update.effective_user.id in ADMINS and ctx.user_data.get("admin_mode"):
        await admin_text(update,ctx)
    else:
        await user_text(update,ctx)

def main():
    if not TOKEN: raise RuntimeError("请在 .env 设置 BOT_TOKEN")
    init()
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin))
    app.add_handler(CallbackQueryHandler(admin_button,pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(user_button))
    app.add_handler(MessageHandler(filters.Document.ALL,admin_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router))

    # Render Web Service 使用 Webhook；本地运行时继续使用 Polling
    if os.getenv("RENDER"):
        port = int(os.getenv("PORT", "10000"))
        base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
        if not base_url:
            raise RuntimeError("Render 环境缺少 RENDER_EXTERNAL_URL")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{base_url}/telegram"
        )
    else:
        app.run_polling()

if __name__=="__main__":
    main()
