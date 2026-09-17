import os, sqlite3, logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip()}
DB = "bot.db"
logging.basicConfig(level=logging.INFO)

CATS = {
    "c1":"分类一","c2":"分类二","c3":"分类三",
    "c4":"分类四","c5":"分类五","c6":"分类六"
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
    for i in range(0,6,2):
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
            [InlineKeyboardButton("⬅️ 返回",callback_data=r["category"])]]
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
        [InlineKeyboardButton("➕ 添加商品",callback_data="a:add")]]
    await update.message.reply_text(f"管理后台\n商品 {p}｜上架 {a}\n待处理询价 {q}",reply_markup=InlineKeyboardMarkup(kb))

async def admin_button(update,ctx):
    q=update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer(); return
    await q.answer()
    if q.data=="a:products":
        c=db(); rows=c.execute("SELECT id,name,code,price,stock,active FROM products ORDER BY id DESC").fetchall(); c.close()
        text="📦 商品列表\n\n" + "\n".join(
            f"#{r['id']} {r['name']} [{r['code']}] | {r['price']} | 库存{r['stock']} | {'上架' if r['active'] else '下架'}"
            for r in rows)
        await q.message.reply_text(text[:4000])
    elif q.data=="a:inq":
        c=db(); rows=c.execute("SELECT id,username,product,message,status,created_at FROM inquiries ORDER BY id DESC LIMIT 30").fetchall(); c.close()
        if not rows:
            await q.message.reply_text("暂无询价"); return
        text="\n\n".join(f"#{r['id']} @{r['username'] or '未设置'}\n商品：{r['product']}\n{r['message']}\n[{r['status']}] {r['created_at']}" for r in rows)
        await q.message.reply_text(text[:4000])
    elif q.data=="a:add":
        ctx.user_data["admin_mode"]="add"
        await q.message.reply_text("按以下格式发送：\n名称|编号|分类(c1-c6)|价格|库存|描述")

async def admin_text(update,ctx):
    if update.effective_user.id not in ADMINS:return
    if ctx.user_data.get("admin_mode")!="add":return
    parts=[x.strip() for x in update.message.text.split("|")]
    if len(parts)!=6:
        await update.message.reply_text("格式错误，请按：名称|编号|分类|价格|库存|描述"); return
    name,code,cat,price,stock,desc=parts
    if cat not in CATS:
        await update.message.reply_text("分类必须是 c1-c6"); return
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router))
    if os.getenv("RENDER"):
    port = int(os.getenv("PORT", "10000"))
    base_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="telegram",
        webhook_url=f"{base_url}/telegram",
    )
else:
    app.run_polling()

if __name__=="__main__":
    main()
