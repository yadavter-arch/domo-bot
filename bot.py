import os, io, re, json, time, glob, base64, asyncio, requests
from urllib.parse import unquote, urljoin
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

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

CREATE_URL   = "https://pay.digitalzonewala.in/api/create-order"
STATUS_URL   = "https://pay.digitalzonewala.in/api/check-order-status"
TIMEOUT_SEC  = 30 * 60
POLL_EVERY   = 5          # auto-check payment every 5 seconds
QR_VALID_MIN = 5

DELIVERED_FILE = os.path.join(DATA_DIR, "delivered.txt")
PENDING_FILE   = os.path.join(DATA_DIR, "pending.json")
DEMO_CACHE     = os.path.join(DATA_DIR, "demo_cache.json")
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
    print("gateway response:", r.status_code, r.text[:200])
    d = r.json()
    if not d.get("status"):
        raise Exception(d.get("message", "Order creation failed"))
    return order_id, d["result"]["payment_url"]

def check_status(order_id):
    r = requests.post(STATUS_URL, data={
        "user_token": GATEWAY_TOKEN,
        "order_id": order_id,
    }, timeout=20)
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
        r = requests.get(pay_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
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
                ir = requests.get(urljoin(pay_url, src), timeout=15)
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
            "✅ Payment is detected automatically — you can also tap Check Payment anytime.")

def pay_keyboard(order_id, pay_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check Payment", callback_data=f"chk:{order_id}")],
        [InlineKeyboardButton("💳 Pay with UPI App", url=pay_url)],
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
            return f.read().strip()
    return f"📚 Check out the demo above.\n\nPrice: ₹{PRICE}"

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await send_demo(context, chat_id)

    if os.path.exists(DEMO_PDF):
        with open(DEMO_PDF, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📖 Free demo (sample pages)")

    await context.bot.send_message(
        chat_id, welcome_text(),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"💳 Buy Now ₹{PRICE}", callback_data="buy")]]))

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Creating your order...")
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    try:
        order_id, pay_url = await asyncio.to_thread(create_order, user_id)
    except Exception as e:
        print("create_order error:", e)
        await context.bot.send_message(
            chat_id, "⚠️ Could not create the order right now. Please try again in a moment.")
        return

    qr = await asyncio.to_thread(get_qr_image, pay_url)
    kb = pay_keyboard(order_id, pay_url)
    if qr:
        msg = await context.bot.send_photo(
            chat_id, qr, caption=payment_text(order_id, True), reply_markup=kb)
    else:
        msg = await context.bot.send_message(
            chat_id, payment_text(order_id, False), reply_markup=kb)

    add_pending(order_id, chat_id, msg.message_id)
    schedule_poll(context.job_queue, order_id, chat_id, time.time())

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
        await q.answer("This order has expired. Send /start to begin again.", show_alert=True)
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
            f"{link}\n\nThank you!")

    if os.path.exists(EBOOK_PATH):
        with open(EBOOK_PATH, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📚 Here is your ebook.")

async def on_startup(application: Application):
    for order_id, info in load_json(PENDING_FILE, {}).items():
        if time.time() - info["created"] < TIMEOUT_SEC + 60:
            schedule_poll(application.job_queue, order_id, info["chat_id"], info["created"])
    print("Bot started.")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern="^chk:"))
    app.run_polling()

if __name__ == "__main__":
    main()
