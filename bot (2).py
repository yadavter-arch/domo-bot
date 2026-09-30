import os, json, time, glob, asyncio, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------- Settings (Railway > Variables mein daalein) ----------
BOT_TOKEN      = os.environ["BOT_TOKEN"]
GATEWAY_TOKEN  = os.environ["GATEWAY_TOKEN"]
BOT_LINK       = os.environ.get("BOT_LINK", "https://t.me/AAPKA_BOT_USERNAME")
PRICE          = int(os.environ.get("PRICE", "99"))
DEFAULT_MOBILE = os.environ.get("DEFAULT_MOBILE", "9999999999")
DATA_DIR       = os.environ.get("DATA_DIR", ".")      # Railway Volume ho to /data
DEMO_DIR       = "demo"          # is folder mein demo screenshots (jpg/png) rakhein
DEMO_PDF       = "demo.pdf"      # optional: sample pages ki PDF
WELCOME_FILE   = "welcome.txt"   # welcome message, GitHub par edit kar sakte hain
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

def welcome_text():
    if os.path.exists(WELCOME_FILE):
        with open(WELCOME_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return f"📚 Hamari ebook/group ka demo neeche dekhein.\n\nKeemat: ₹{PRICE}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # 1) Demo screenshots (max 10 ek saath)
    photos = sorted(glob.glob(os.path.join(DEMO_DIR, "*.jpg")) +
                    glob.glob(os.path.join(DEMO_DIR, "*.jpeg")) +
                    glob.glob(os.path.join(DEMO_DIR, "*.png")))[:10]
    if photos:
        files = [open(p, "rb") for p in photos]
        try:
            media = [InputMediaPhoto(f) for f in files]
            await context.bot.send_media_group(chat_id, media)
        except Exception as e:
            print("demo photos error:", e)
        finally:
            for f in files:
                f.close()

    # 2) Optional demo PDF
    if os.path.exists(DEMO_PDF):
        with open(DEMO_PDF, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📖 Free demo (sample pages)")

    # 3) Description + Buy button
    await context.bot.send_message(
        chat_id, welcome_text(),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"💳 Buy Now ₹{PRICE}", callback_data="buy")]]))

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Order ban raha hai...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    try:
        order_id, pay_url = await asyncio.to_thread(create_order, user_id)
    except Exception as e:
        print("create_order error:", e)
        await context.bot.send_message(chat_id, "⚠️ Abhi order nahi ban paya, thodi der baad try karein.")
        return

    add_pending(order_id, chat_id)
    await context.bot.send_message(
        chat_id,
        f"✅ Order ban gaya. Keemat: ₹{PRICE}\n\n"
        "Neeche button se payment karein. Payment hote hi access yahin mil jayega.\n"
        "(Order 30 minute tak valid hai.)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("💳 Pay Now", url=pay_url)]]))
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
app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
app.run_polling()
