import os, json, time, asyncio, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Settings (Railway > Variables mein daalein) ----------
BOT_TOKEN      = os.environ["BOT_TOKEN"]
GATEWAY_TOKEN  = os.environ["GATEWAY_TOKEN"]
BOT_LINK       = os.environ.get("BOT_LINK", "https://t.me/AAPKA_BOT_USERNAME")
PRICE          = int(os.environ.get("PRICE", "99"))
DEFAULT_MOBILE = os.environ.get("DEFAULT_MOBILE", "9999999999")
DATA_DIR       = os.environ.get("DATA_DIR", ".")      # Railway Volume ho to /data
EBOOK_PATH     = "ebook.pdf"                           # optional: file ho to woh bhi bhejega
GROUP_LINK     = os.environ.get("GROUP_LINK", "")      # simple tareeka: fixed group link
GROUP_CHAT_ID  = os.environ.get("GROUP_CHAT_ID", "")   # behtar tareeka: har user ko alag 1-baar wala link

CREATE_URL = "https://pay.digitalzonewala.in/api/create-order"
STATUS_URL = "https://pay.digitalzonewala.in/api/check-order-status"
TIMEOUT_SEC = 30 * 60

DELIVERED_FILE = os.path.join(DATA_DIR, "delivered.txt")
PENDING_FILE   = os.path.join(DATA_DIR, "pending.json")


# ---------- Chhota sa storage ----------
def already_delivered(order_id):
    if not os.path.exists(DELIVERED_FILE):
        return False
    with open(DELIVERED_FILE) as f:
        return order_id in f.read().split()

def mark_delivered(order_id):
    with open(DELIVERED_FILE, "a") as f:
        f.write(order_id + "\n")

def load_pending():
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_pending(p):
    with open(PENDING_FILE, "w") as f:
        json.dump(p, f)

def add_pending(order_id, chat_id):
    p = load_pending()
    p[order_id] = {"chat_id": chat_id, "created": time.time()}
    save_pending(p)

def remove_pending(order_id):
    p = load_pending()
    p.pop(order_id, None)
    save_pending(p)


# ---------- Gateway API ----------
def create_order(user_id):
    order_id = f"{user_id}{int(time.time())}"
    r = requests.post(CREATE_URL, data={
        "customer_mobile": DEFAULT_MOBILE,
        "user_token": GATEWAY_TOKEN,
        "amount": str(PRICE),
        "order_id": order_id,
        "redirect_url": BOT_LINK,
        "remark1": f"tg_{user_id}",
        "remark2": "ebook",
    }, timeout=20)
    d = r.json()
    if not d.get("status"):
        raise Exception(d.get("message", "Order create nahi hua"))
    return order_id, d["result"]["payment_url"]

def check_status(order_id):
    r = requests.post(STATUS_URL, data={
        "user_token": GATEWAY_TOKEN,
        "order_id": order_id,
    }, timeout=20)
    return r.json()


# ---------- Bot ----------
def schedule_poll(job_queue, order_id, chat_id, created):
    job_queue.run_repeating(
        poll, interval=10, first=10,
        data={"order_id": order_id, "chat_id": chat_id, "created": created},
        name=order_id,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        order_id, pay_url = await asyncio.to_thread(create_order, user_id)
    except Exception as e:
        print("create_order error:", e)
        await update.message.reply_text("⚠️ Abhi order nahi ban paya, thodi der baad try karein.")
        return

    add_pending(order_id, chat_id)
    await update.message.reply_text(
        f"📚 Ebook ki keemat: ₹{PRICE}\n\n"
        "Neeche button se payment karein. Payment hote hi access yahin mil jayega.\n"
        "(Order 30 minute tak valid hai.)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Pay Now", url=pay_url)]]),
    )
    schedule_poll(context.job_queue, order_id, chat_id, time.time())

async def poll(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    order_id = job.data["order_id"]
    chat_id = job.data["chat_id"]

    if already_delivered(order_id):
        remove_pending(order_id)
        job.schedule_removal()
        return

    if time.time() - job.data["created"] > TIMEOUT_SEC + 60:
        remove_pending(order_id)
        job.schedule_removal()
        return

    try:
        d = await asyncio.to_thread(check_status, order_id)
    except Exception as e:
        print("status error:", e)
        return

    result = d.get("result") or {}
    try:
        amount_ok = float(result.get("amount", 0)) == float(PRICE)
    except (TypeError, ValueError):
        amount_ok = False

    if d.get("status") == "COMPLETED" and result.get("status") == "SUCCESS" and amount_ok:
        mark_delivered(order_id)      # pehle mark, taaki dobara na jaye
        remove_pending(order_id)
        job.schedule_removal()
        await deliver(context, chat_id)

async def deliver(context, chat_id):
    link = GROUP_LINK
    if GROUP_CHAT_ID:
        try:
            # Sirf 1 banda use kar sakta hai, 24 ghante mein expire
            inv = await context.bot.create_chat_invite_link(
                chat_id=int(GROUP_CHAT_ID), member_limit=1,
                expire_date=int(time.time()) + 24 * 3600)
            link = inv.invite_link
        except Exception as e:
            print("invite link error:", e)   # fail hua to GROUP_LINK use hoga

    if link:
        await context.bot.send_message(
            chat_id,
            "🎉 Payment mil gaya! Neeche link se group join karein:\n\n"
            f"{link}\n\nDhanyavaad!")

    if os.path.exists(EBOOK_PATH):
        with open(EBOOK_PATH, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📚 Ye rahi aapki ebook.")

async def on_startup(application: Application):
    # Restart ke baad pending orders ki checking dobara shuru
    for order_id, info in load_pending().items():
        if time.time() - info["created"] < TIMEOUT_SEC + 60:
            schedule_poll(application.job_queue, order_id, info["chat_id"], info["created"])
    print("Bot chalu ho gaya.")

app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
app.add_handler(CommandHandler("start", start))
app.run_polling()
