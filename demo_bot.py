import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

CHANNEL_IDS = {
    "Main CP DEMO": -1004304439937,
    "Indian r##p MMS Leaked": -1004360171518,
    "Chi$#dd mms leaked video DEMO": -1004339995876,
    "Desi Cucks Bundle DEMO": -1004346101582,
    "Punjabi leak Bundle DEMO": -1003934594969,
    "Desi viral Bhabhi DEMO": -1004317800836,
    "Jaslin Kaur Demo": -1003746920545,
    "Mom Son DEMO": -1003907927607,
    "Bro Sis Demo": -1003962620877,
    "Tango Live Video Call Demo": -1003922128376,
    "Best Edits Demo": -1004482705956,
    "Desi Flashing Demo": -1004281697182,
    "Full Open Dance demo": -1004467834124,
    "Dress Change Demo": -1004348122859,
    "Aditiy Mistry Demo": -1003643700138,
    "Full Webseries Demo": -1004462474135,
    "Desi Moti Gand Walking Demo": -1004363997989,
    "Teen CP Demo": -1003966340422,
    "Mom and Daughter CP Demo": -1004350514494,
    "Foreign CP Demo": -1004403613395,
    "Tamil CP Demo": -1004262611728,
    "𝗙𝗼𝗿𝗰𝗲𝗱 𝘀𝗲𝘅 CP Demo": -1004262611728,
    "All in one CP Demo": -1003931380677,
    "All complete Combo Package": -1004347644527,
}

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for category in CHANNEL_IDS.keys():
        keyboard.append([InlineKeyboardButton(category, callback_data="cat_" + category)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "*Welcome to get all demo videos instantly Bot!*\n\nSelect a category to watch demo videos and get full long BUY CP massage me http://t.me/Kraja8:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )

async def send_category_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category = query.data.replace("cat_", "")
    user_id = query.from_user.id
    channel_id = CHANNEL_IDS.get(category)

    if not channel_id:
        await query.message.reply_text("Category not found!")
        return

    await query.message.reply_text(
        "Please wait... Sending " + category + " demo videos!"
    )

    success = 0
    for msg_id in range(1, 50):
        try:
            await context.bot.forward_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=msg_id
            )
            success += 1
        except Exception:
            continue

    keyboard = [[InlineKeyboardButton("Back to Categories", callback_data="back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if success > 0:
        msg = "Done!\n\n" + str(success) + " demo videos sent!\n\nWant to see another category?\n\nPayment karke full access lo!\nScreenshot bhejo: @Kraja8"
    else:
        msg = "No videos found.\n\nPayment karke full access lo!\nScreenshot bhejo: @Kraja8"

    await context.bot.send_message(
        chat_id=user_id,
        text=msg,
        reply_markup=reply_markup,
    )

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = []
    for category in CHANNEL_IDS.keys():
        keyboard.append([InlineKeyboardButton(category, callback_data="cat_" + category)])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "Select a category to watch demo videos:",
        reply_markup=reply_markup,
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="back"))
    app.add_handler(CallbackQueryHandler(send_category_videos, pattern="^cat_"))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
