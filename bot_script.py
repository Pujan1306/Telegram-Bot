import logging
from datetime import datetime
import pymongo 
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import requests
import os
import tempfile
from PIL import Image
import PyPDF2
import traceback

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants and configurations
TELEGRAM_TOKEN = os.getenv("7260145331:AAHU4Mpxm0TxQaLD3jHekgIAyLunwFTkPQc")
GEMINI_API_KEY = os.getenv("AIzaSyBb3t1NdSRrb1Qx4oc5ziJ0rzEYoReWvqM")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client['telegram_bot']

# Helper function for error logging
def log_exception(context, error):
    logger.error(f"Exception in {context}: {error}")
    logger.error(traceback.format_exc())

# Handlers for registration, chat, and features
def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user_data = {
        "chat_id": chat_id,
        "first_name": update.message.chat.first_name,
        "username": update.message.chat.username,
    }
    if not db.users.find_one({"chat_id": chat_id}):
        db.users.insert_one(user_data)
        logger.info(f"User registered: {user_data}")
        reply_keyboard = [[KeyboardButton("Share Phone Number", request_contact=True)]]
        update.message.reply_text(
            "Welcome! Please share your phone number to complete registration.",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        )
    else:
        update.message.reply_text("You are already registered!")

def save_contact(update: Update, context: CallbackContext):
    try:
        if update.message.contact:
            phone_number = update.message.contact.phone_number
            chat_id = update.message.contact.user_id
            db.users.update_one({"chat_id": chat_id}, {"$set": {"phone_number": phone_number}})
            logger.info(f"Phone number saved: {phone_number}")
            update.message.reply_text("Thank you! Registration is now complete.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        log_exception("save_contact", e)
        update.message.reply_text("An error occurred while saving your contact. Please try again.")

def gemini_chat(update: Update, context: CallbackContext):
    user_message = update.message.text
    chat_id = update.effective_chat.id

    try:
        response = requests.post(
            "https://gemini.googleapis.com/v1/models/text-bison-001:generateText",
            headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
            json={"text": user_message}
        )
        response.raise_for_status()
        response_text = response.json().get("response", "I'm not sure how to answer that.")

        db.chat_history.insert_one({
            "chat_id": chat_id,
            "user_message": user_message,
            "bot_response": response_text,
            "timestamp": datetime.now()
        })
        update.message.reply_text(response_text)

    except Exception as e:
        log_exception("gemini_chat", e)
        update.message.reply_text("I'm having trouble processing your request right now.")

def analyze_file(update: Update, context: CallbackContext):
    if not (update.message.document or update.message.photo):
        update.message.reply_text("Please send a valid file or image.")
        return

    file_id = update.message.document.file_id if update.message.document else update.message.photo[-1].file_id
    file_name = update.message.document.file_name if update.message.document else "image.jpg"

    try:
        file_path = context.bot.get_file(file_id).download()
        logger.info(f"File downloaded: {file_name}")

        if file_name.endswith(('.png', '.jpg', '.jpeg')):
            with Image.open(file_path) as img:
                img.verify()
                response = requests.post(
                    "https://gemini.googleapis.com/v1/images:annotate",
                    headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
                    files={"file": open(file_path, "rb")}
                )
                response.raise_for_status()
                description = response.json().get("description", "Unable to describe the image.")

        elif file_name.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(file_path)
            text = "\n".join(page.extract_text() for page in pdf_reader.pages)
            response = requests.post(
                "https://gemini.googleapis.com/v1/models/text-bison-001:generateText",
                headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
                json={"text": text[:4000]}  # Limiting to Gemini API's input size.
            )
            response.raise_for_status()
            description = response.json().get("response", "Unable to analyze the document.")

        else:
            description = "Unsupported file type. Please send an image or a PDF."

        db.file_metadata.insert_one({
            "chat_id": update.effective_chat.id,
            "file_name": file_name,
            "description": description,
            "timestamp": datetime.now()
        })
        update.message.reply_text(f"Analysis complete: {description}")

    except Exception as e:
        log_exception("analyze_file", e)
        update.message.reply_text("Sorry, I couldn't analyze the file. Please try again with a supported format.")

def web_search(update: Update, context: CallbackContext):
    query = ' '.join(context.args)
    if not query:
        update.message.reply_text("Please provide a search query after /websearch.")
        return

    try:
        response = requests.post(
            "https://gemini.googleapis.com/v1/search:query",
            headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
            json={"query": query}
        )
        response.raise_for_status()
        result = response.json()

        summary = result.get('summary', 'No results found.')
        links = result.get('top_links', [])
        links_text = '\n'.join(links)

        update.message.reply_text(f"Search Results:\n{summary}\n\nTop Links:\n{links_text}")

    except Exception as e:
        log_exception("web_search", e)
        update.message.reply_text("Unable to perform the search at the moment.")

def referral_system(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = db.users.find_one({"chat_id": chat_id})
    if not user:
        update.message.reply_text("Please register first using the /start command.")
        return

    try:
        referral_code = str(chat_id)[-6:]  # Generate a unique referral code based on chat ID
        db.referrals.update_one(
            {"referral_code": referral_code},
            {"$set": {"referrer": user["username"], "timestamp": datetime.now()}},
            upsert=True
        )
        update.message.reply_text(f"Your referral code is: {referral_code}\nShare it with friends to get rewards!")

    except Exception as e:
        log_exception("referral_system", e)
        update.message.reply_text("Unable to generate a referral code. Please try again later.")

def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    # Command and message handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.contact, save_contact))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, gemini_chat))
    dispatcher.add_handler(MessageHandler(Filters.document | Filters.photo, analyze_file))
    dispatcher.add_handler(CommandHandler("websearch", web_search, pass_args=True))
    dispatcher.add_handler(CommandHandler("referral", referral_system))

    # Start the bot
    updater.start_polling()
    logger.info("Bot started.")
    updater.idle()

if __name__ == "__main__":
    main()
