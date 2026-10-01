import os, io, re, json, time, glob, base64, asyncio, requests
from urllib.parse import unquote, urljoin
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ---------- Settings (.env / Variables) ----------
BOT_TOKEN      = os.environ["BOT_TOKEN"]
GATEWAY_TOKEN  = os.environ["GATEWAY_TOKEN"]
BOT_LINK       = os.environ.get("BOT_LINK", "https://t.me/AAPKA_BOT_USERNAME")
PRICE          = int(os.environ.get("PRICE", "99"))
DEFAULT_MOBILE = os.environ.get("DEFAULT_MOBILE", "9999999999")
DATA_DIR       = os.environ.get("DATA_DIR", ".")
DEMO_DIR       = "demo"
DEMO_PDF       = "demo.pdf"
WELCOME_FILE   = "welcome.txt"
EBOOK_PATH     = "ebook.pdf"
GROUP_LINK     = os.environ.get("GROUP_LINK", "")
GROUP_CHAT_ID  = os.environ.get("GROUP_CHAT_ID", "")

CREATE_URL  = "https://pay.digitalzonewala.in/api/create-order"
STATUS_URL  = "https://pay.digitalzonewala.in/api/check-order-status"
TIMEOUT_SEC = 30 * 60
POLL_EVERY  = 5          # har 5 second mein payment check

DELIVERED_FILE = os.path.join(DATA_DIR, "delivered.txt")
PENDING_FILE   = os.path.join(DATA_DIR, "pending.json")
DEMO_CACHE     = os.path.join(DATA_DIR, "demo_cache.json")


# ---------- Chhota sa storage ----------
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

def add_pending(order_id, chat_id):
    p = load_json(PENDING_FILE, {})
    p[order_id] = {"chat_id": chat_id, "created": time.time()}
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
        raise Exception(d.get("message", "Order create nahi hua"))
    return order_id, d["result"]["payment_url"]

def check_status(order_id):
    r = requests.post(STATUS_URL, data={
        "user_token": GATEWAY_TOKEN,
        "order_id": order_id,
    }, timeout=20)
    return r.json()


# ---------- Telegram ke andar QR ----------
def make_qr_png(text):
    img = qrcode.make(text)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

def get_qr_image(pay_url):
    """Gateway ke payment page se QR nikalta hai. Nahi mila to None."""
    try:
        r = requests.get(pay_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text.replace("&amp;", "&").replace("\\/", "/")
    except Exception as e:
        print("payment page error:", e)
        return None

    # 1) page mein UPI link ho to usse apna QR banao
    m = re.search(r'upi://pay\?[^\s"\'<>\\]+', html)
    if m:
        return make_qr_png(m.group(0))
    m = re.search(r'upi%3A%2F%2Fpay%3F[^\s"\'<>\\]+', html)
    if m:
        return make_qr_png(unquote(m.group(0)))

    # 2) page mein embedded QR image (base64)
    m = re.search(r'data:image/(?:png|jpeg|jpg);base64,([A-Za-z0-9+/=]{800,})', html)
    if m:
        try:
            return io.BytesIO(base64.b64decode(m.group(1)))
        except Exception:
            pass

    # 3) QR image ka link
    for src in re.findall(r'<img[^>]+src=["\']([^"\']+)', html):
        if "qr" in src.lower():
            try:
                ir = requests.get(urljoin(pay_url, src), timeout=15)
                if ir.ok and ir.headers.get("content-type", "").startswith("image"):
                    return io.BytesIO(ir.content)
            except Exception:
                pass

    imgs = re.findall(r'<img[^>]+src=["\']([^"\']{0,80})', html)[:8]
    print("QR nahi mila. page length:", len(html), "| img src:", imgs)
    return None


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
    return f"📚 Demo neeche dekhein.\n\nKeemat: ₹{PRICE}"

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

    # Pehle se upload hui photos ki ID se turant bhejo (fast)
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
    schedule_poll(context.job_queue, order_id, chat_id, time.time())

    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("💳 UPI App se Pay karein", url=pay_url)]])
    qr = await asyncio.to_thread(get_qr_image, pay_url)
    if qr:
        await context.bot.send_photo(
            chat_id, qr,
            caption=(f"✅ Order ban gaya. Keemat: ₹{PRICE}\n\n"
                     "📲 Ye QR kisi doosre phone se scan karke pay karein, "
                     "ya neeche button dabakar apne UPI app se pay karein.\n"
                     "Payment hote hi access yahin mil jayega. (5 minute mein valid)"),
            reply_markup=buttons)
    else:
        await context.bot.send_message(
            chat_id,
            f"✅ Order ban gaya. Keemat: ₹{PRICE}\n\n"
            "Neeche button se payment karein. Payment hote hi access yahin mil jayega.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 Pay Now", url=pay_url)]]))

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
        mark_delivered(order_id)
        remove_pending(order_id)
        job.schedule_removal()
        await deliver(context, chat_id)

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
            "🎉 Payment mil gaya! Neeche link se group join karein:\n\n"
            f"{link}\n\nDhanyavaad!")

    if os.path.exists(EBOOK_PATH):
        with open(EBOOK_PATH, "rb") as f:
            await context.bot.send_document(chat_id, f, caption="📚 Ye rahi aapki ebook.")

async def on_startup(application: Application):
    for order_id, info in load_json(PENDING_FILE, {}).items():
        if time.time() - info["created"] < TIMEOUT_SEC + 60:
            schedule_poll(application.job_queue, order_id, info["chat_id"], info["created"])
    print("Bot chalu ho gaya.")

app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(buy, pattern="^buy$"))
app.run_polling()
