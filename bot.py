import os, io, re, json, time, glob, base64, socket, asyncio, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urljoin
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import urllib3.util.connection as urllib3_cn

# Force IPv4 (avoids slow IPv6 fallback; our whitelisted IP is IPv4)
if os.environ.get("FORCE_IPV4", "1") == "1":
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

HTTP = requests.Session()   # keeps connections open = faster repeat calls

# ---------- Settings (.env) ----------
BOT_TOKEN      = os.environ["BOT_TOKEN"]
GATEWAY_TOKEN  = os.environ["GATEWAY_TOKEN"]
BOT_LINK       = os.environ.get("BOT_LINK", "https://t.me/YOUR_BOT_USERNAME")
PRICE          = int(os.environ.get("PRICE", "99"))
DEFAULT_MOBILE = os.environ.get("DEFAULT_MOBILE", "9999999999")
DATA_DIR       = os.environ.get("DATA_DIR", ".")
DEMO_DIR       = "demo"
DEMO_PDF       = "demo.pdf"
WELCOME_FILE   = "welcome.txt"
EBOOK_PATH     = "ebook.pdf"
GROUP_LINK     = os.environ.get("GROUP_LINK", "")
GROUP_CHAT_ID  = os.environ.get("GROUP_CHAT_ID", "")

# Support + reminders
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Jjanuji").lstrip("@")
REMINDER_HOURS   = float(os.environ.get("REMINDER_HOURS", "2"))   # gap between reminders
MAX_REMINDERS    = int(os.environ.get("MAX_REMINDERS", "3"))      # max reminders per user
SEND_FROM_HOUR   = int(os.environ.get("SEND_FROM_HOUR", "9"))     # India time, no night messages
SEND_TO_HOUR     = int(os.environ.get("SEND_TO_HOUR", "22"))
IST = timezone(timedelta(hours=5, minutes=30))

CREATE_URL   = "https://pay.digitalzonewala.in/api/create-order"
STATUS_URL   = "https://pay.digitalzonewala.in/api/check-order-status"
TIMEOUT_SEC  = 30 * 60
POLL_EVERY   = 5          # auto-check payment every 5 seconds
QR_VALID_MIN = 5

DELIVERED_FILE = os.path.join(DATA_DIR, "delivered.txt")
PENDING_FILE   = os.path.join(DATA_DIR, "pending.json")
DEMO_CACHE     = os.path.join(DATA_DIR, "demo_cache.json")
USERS_FILE     = os.path.join(DATA_DIR, "users.json")
ORDER_LOCKS = {}


# ---------- Small storage ----------
def already_delivered(order_id):
    if not os.path.exists(DELIVERED_FILE):
        return False
    with open(DELIVERED_FILE) as f:
        return order_id in f.read().split()

def mark_delivered(order_id):
    with open(DELIVERED_FILE, "a") as f:
        f.write(order_id + "\n")

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def add_pending(order_id, chat_id, msg_id=None):
    p = load_json(PENDING_FILE, {})
    p[order_id] = {"chat_id": chat_id, "created": time.time(), "msg_id": msg_id}
    save_json(PENDING_FILE, p)

def remove_pending(order_id):
    p = load_json(PENDING_FILE, {})
    p.pop(order_id, None)
    save_json(PENDING_FILE, p)


# ---------- Users (for reminders) ----------
def update_user(chat_id, **fields):
    users = load_json(USERS_FILE, {})
    rec = users.get(str(chat_id), {"reminders_sent": 0, "paid": False,
                                    "muted": False, "blocked": False})
    rec.update(fields)
    users[str(chat_id)] = rec
    save_json(USERS_FILE, users)

def get_user(chat_id):
    return load_json(USERS_FILE, {}).get(str(chat_id))

def touch_user(chat_id, returning=False):
    """Records activity. returning=True (user pressed /start) resets the reminder count."""
    fields = {"last_activity": time.time()}
    if returning:
        fields.update(reminders_sent=0, blocked=False)
    update_user(chat_id, **fields)


# ---------- Support text ----------
def support_line():
    return f"💬 Need help? Contact @{SUPPORT_USERNAME}"

def support_button():
    return InlineKeyboardButton("💬 Support", url=f"https://t.me/{SUPPORT_USERNAME}")


# ---------- Gateway API ----------
def create_order(user_id):
    order_id = f"{user_id}{int(time.time())}"
    r = HTTP.post(CREATE_URL, data={
        "customer_mobile": DEFAULT_MOBILE,
        "user_token": GATEWAY_TOKEN,
        "amount": str(PRICE),
        "order_id": order_id,
        "redirect_url": BOT_LINK,
        "remark1": f"tg_{user_id}",
        "remark2": "ebook",
    }, timeout=15)
    print("gateway response:", r.status_code, r.text[:200])
    d = r.json()
    if not d.get("status"):
        raise Exception(d.get("message", "Order creation failed"))
    return order_id, d["result"]["payment_url"]

def check_status(order_id):
    r = HTTP.post(STATUS_URL, data={
        "user_token": GATEWAY_TOKEN,
        "order_id": order_id,
    }, timeout=10)
    return r.json()


# ---------- QR inside Telegram ----------
def make_qr_png(text):
    img = qrcode.make(text)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

def get_qr_image(pay_url):
    """Extracts the QR from the gateway payment page. Returns None if not found."""
    try:
        r = HTTP.get(pay_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text.replace("&amp;", "&").replace("\\/", "/")
    except Exception as e:
        print("payment page error:", e)
        return None

    m = re.search(r'upi://pay\?[^\s"\'<>\\]+', html)
    if m:
        return make_qr_png(m.group(0))
    m = re.search(r'upi%3A%2F%2Fpay%3F[^\s"\'<>\\]+', html)
    if m:
        return make_qr_png(unquote(m.group(0)))

    m = re.search(r'data:image/(?:png|jpeg|jpg);base64,([A-Za-z0-9+/=]{800,})', html)
    if m:
        try:
            return io.BytesIO(base64.b64decode(m.group(1)))
        except Exception:
            pass

    for src in re.findall(r'<img[^>]+src=["\']([^"\']+)', html):
        if "qr" in src.lower():
            try:
                ir = HTTP.get(urljoin(pay_url, src), timeout=8)
                if ir.ok and ir.headers.get("content-type", "").startswith("image"):
                    return io.BytesIO(ir.content)
            except Exception:
                pass

    imgs = re.findall(r'<img[^>]+src=["\']([^"\']{0,80})', html)[:8]
    print("QR not found. page length:", len(html), "| img src:", imgs)
    return None


# ---------- Payment message ----------
def payment_text(order_id, has_qr):
    how = ("📲 Scan the QR above with any UPI app (Paytm, GPay, PhonePe...)."
           if has_qr else
           "📲 Tap “Pay with UPI App” below to pay with Paytm, GPay, PhonePe...")
    return ("💳 Complete Your Payment\n\n"
            f"💰 Amount: ₹{PRICE}\n"
            f"🆔 Order ID: {order_id}\n\n"
            f"How to pay:\n{how}\n\n"
            f"⏳ Valid for {QR_VALID_MIN} minutes.\n"
            "✅ Payment is detected automatically — you can also tap Check Payment anytime.\n\n"
            f"Paid but didn't get the link? Contact @{SUPPORT_USERNAME} with your Order ID.")

def pay_keyboard(order_id, pay_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check Payment", callback_data=f"chk:{order_id}")],
        [InlineKeyboardButton("💳 Pay with UPI App", url=pay_url)],
        [support_button()],
    ])


# ---------- Bot ----------
def schedule_poll(job_queue, order_id, chat_id, created):
    job_queue.run_repeating(
        poll, interval=POLL_EVERY, first=3,
        data={"order_id": order_id, "chat_id": chat_id, "created": created},
        name=order_id,
    )

def welcome_text():
    if os.path.exists(WELCOME_FILE):
        with open(WELCOME_FILE, encoding="utf-8") as f:
            base = f.read().strip()
    else:
        base = f"📚 Check out the demo above.\n\nPrice: ₹{PRICE}"
    return base + "\n\n" + support_line()

def demo_photos():
    files = []
    for ext in ("jpg", "jpeg", "png"):
        files += glob.glob(f"demo*.{ext}")
        files += glob.glob(os.path.join(DEMO_DIR, f"*.{ext}"))
    return sorted(set(files))[:10]

async def send_demo(context, chat_id):
    photos = demo_photos()
    if not photos:
        return
    sig = [[os.path.basename(p), os.path.getsize(p)] for p in photos]
    cache = load_json(DEMO_CACHE, {})
    if cache.get("sig") == sig and cache.get("ids"):
        try:
            await context.bot.send_media_group(
                chat_id, [InputMediaPhoto(i) for i in cache["ids"]])
            return
        except Exception as e:
            print("cached demo error:", e)

    files = [open(p, "rb") for p in photos]
    try:
        msgs = await context.bot.send_media_group(
            chat_id, [InputMediaPhoto(f) for f in files])
        save_json(DEMO_CACHE, {"sig": sig, "ids": [m.photo[-1].file_id for m in msgs]})
    except Exception as e:
        print("demo photos error:", e)
    finally:
        for f in files:
            f.close()

def buy_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Buy Now ₹{PRICE}", callback_data="buy")],
        [support_button()],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    touch_user(chat_id, returning=True)
    await send_demo(context, chat_id)

    if os.path.exists(DEMO_PDF):
        with open(DEMO_PDF, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📖 Free demo (sample pages)")

    await context.bot.send_message(chat_id, welcome_text(), reply_markup=buy_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{support_line()}\n\n"
        "If you already paid but did not receive the group link, "
        f"message @{SUPPORT_USERNAME} with your Order ID and a payment screenshot.\n\n"
        "Send /start to see the demo and buy.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user(update.effective_chat.id, muted=True)
    await update.message.reply_text("🔕 Reminders turned off. Send /start anytime to see the offer again.")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    update_user(q.message.chat_id, muted=True)
    await q.answer("🔕 Reminders turned off.", show_alert=True)

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    touch_user(chat_id)

    # Instant feedback so the user knows something is happening
    wait_msg = await context.bot.send_message(chat_id, "⏳ Creating your order, please wait...")

    t0 = time.time()
    try:
        order_id, pay_url = await asyncio.to_thread(create_order, user_id)
    except Exception as e:
        print("create_order error:", e)
        await wait_msg.edit_text(
            "⚠️ Could not create the order right now. Please try again in a moment.\n\n"
            + support_line())
        return
    t1 = time.time()

    qr = await asyncio.to_thread(get_qr_image, pay_url)
    t2 = time.time()

    kb = pay_keyboard(order_id, pay_url)
    if qr:
        msg = await context.bot.send_photo(
            chat_id, qr, caption=payment_text(order_id, True), reply_markup=kb)
    else:
        msg = await context.bot.send_message(
            chat_id, payment_text(order_id, False), reply_markup=kb)
    t3 = time.time()

    add_pending(order_id, chat_id, msg.message_id)
    schedule_poll(context.job_queue, order_id, chat_id, time.time())
    try:
        await wait_msg.delete()
    except Exception:
        pass
    print(f"timing: create_order={t1-t0:.1f}s qr={t2-t1:.1f}s telegram_send={t3-t2:.1f}s")

async def process_order(context, order_id, chat_id):
    """Checks the gateway once. Returns: paid / already / pending / error."""
    lock = ORDER_LOCKS.setdefault(order_id, asyncio.Lock())
    async with lock:
        if already_delivered(order_id):
            return "already"
        try:
            d = await asyncio.to_thread(check_status, order_id)
        except Exception as e:
            print("status error:", e)
            return "error"

        result = d.get("result") or {}
        try:
            amount_ok = float(result.get("amount", 0)) == float(PRICE)
        except (TypeError, ValueError):
            amount_ok = False

        if d.get("status") == "COMPLETED" and result.get("status") == "SUCCESS" and amount_ok:
            mark_delivered(order_id)
            update_user(chat_id, paid=True)          # no more reminders for this user
            info = load_json(PENDING_FILE, {}).get(order_id, {})
            remove_pending(order_id)
            await deliver(context, chat_id)
            if info.get("msg_id"):
                try:
                    await context.bot.delete_message(chat_id, info["msg_id"])
                except Exception as e:
                    print("delete message error:", e)
            return "paid"
        return "pending"

async def poll(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    order_id = job.data["order_id"]
    chat_id = job.data["chat_id"]

    if time.time() - job.data["created"] > TIMEOUT_SEC + 60:
        remove_pending(order_id)
        job.schedule_removal()
        return

    status = await process_order(context, order_id, chat_id)
    if status in ("paid", "already"):
        job.schedule_removal()

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    order_id = q.data.split(":", 1)[1]
    chat_id = q.message.chat_id

    if already_delivered(order_id):
        await q.answer("Payment already received ✅", show_alert=True)
        return
    info = load_json(PENDING_FILE, {}).get(order_id)
    if info is None or info.get("chat_id") != chat_id:
        await q.answer(f"This order has expired. If you already paid, contact @{SUPPORT_USERNAME} "
                       "with your Order ID. Otherwise send /start.", show_alert=True)
        return

    status = await process_order(context, order_id, chat_id)
    if status == "paid":
        await q.answer("Payment received ✅", show_alert=True)
    elif status == "already":
        await q.answer("Payment already received ✅", show_alert=True)
    else:
        await q.answer("Payment not received yet. Please complete the payment and tap again.",
                       show_alert=True)

async def deliver(context, chat_id):
    link = GROUP_LINK
    if GROUP_CHAT_ID:
        try:
            inv = await context.bot.create_chat_invite_link(
                chat_id=int(GROUP_CHAT_ID), member_limit=1,
                expire_date=int(time.time()) + 24 * 3600)
            link = inv.invite_link
        except Exception as e:
            print("invite link error:", e)

    if link:
        await context.bot.send_message(
            chat_id,
            "🎉 Payment received! Join the group using the link below:\n\n"
            f"{link}\n\nThank you!\n\n"
            f"Any problem with the link? Contact @{SUPPORT_USERNAME}")

    if os.path.exists(EBOOK_PATH):
        with open(EBOOK_PATH, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📚 Here is your ebook.")


# ---------- Reminders ----------
def now_hour():
    return datetime.now(IST).hour

def reminder_text():
    return ("⏰ Reminder\n\n"
            "You didn't complete your payment. 😔\n"
            "Try again to get access — tap Buy Now below! 👇\n\n"
            + support_line())

def reminder_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛒 Buy Now ₹{PRICE}", callback_data="buy")],
        [support_button()],
        [InlineKeyboardButton("🔕 Stop reminders", callback_data="mute")],
    ])

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 10 minutes. Sends reminders only to users who have not paid."""
    if not (SEND_FROM_HOUR <= now_hour() < SEND_TO_HOUR):
        return                                   # no messages at night (India time)
    now = time.time()
    for key in list(load_json(USERS_FILE, {}).keys()):
        rec = get_user(key) or {}
        if rec.get("paid") or rec.get("muted") or rec.get("blocked"):
            continue
        if rec.get("reminders_sent", 0) >= MAX_REMINDERS:
            continue
        last = max(rec.get("last_activity", 0), rec.get("last_reminder", 0))
        if now - last < REMINDER_HOURS * 3600:
            continue
        chat_id = int(key)
        pending = load_json(PENDING_FILE, {})
        if any(i.get("chat_id") == chat_id and now - i["created"] < TIMEOUT_SEC
               for i in pending.values()):
            continue                             # user is in the middle of paying
        try:
            await context.bot.send_message(
                chat_id, reminder_text(), reply_markup=reminder_keyboard())
            update_user(chat_id, reminders_sent=rec.get("reminders_sent", 0) + 1,
                        last_reminder=now)
        except Forbidden:
            update_user(chat_id, blocked=True)   # user blocked the bot
        except Exception as e:
            print("reminder error:", chat_id, e)
        await asyncio.sleep(0.1)                 # stay well under Telegram rate limits


async def on_startup(application: Application):
    for order_id, info in load_json(PENDING_FILE, {}).items():
        if time.time() - info["created"] < TIMEOUT_SEC + 60:
            schedule_poll(application.job_queue, order_id, info["chat_id"], info["created"])
    application.job_queue.run_repeating(reminder_job, interval=600, first=60)
    print("Bot started.")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(mute, pattern="^mute$"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern="^chk:"))
    app.run_polling()

if __name__ == "__main__":
    main()
