from dotenv import load_dotenv
import logging
import os
import traceback
import pymongo
import requests
from datetime import datetime
from pymongo import MongoClient
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone as pytz_timezone
from pymongo.errors import ServerSelectionTimeoutError
import google.generativeai as genai
from PIL import Image
import io
import time
from requests.exceptions import RequestException

# Load environment variables properly
load_dotenv()  # Load environment variables from .env file

# Set timezone to IST (India Standard Time)
timezone = pytz_timezone("Asia/Kolkata")

# Use this when initializing the scheduler
scheduler = AsyncIOScheduler()
scheduler.configure(timezone=timezone)

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

# Configure Google Generative AI
genai.configure(api_key=GEMINI_API_KEY)

# MongoDB Connection
if not MONGO_URI:
    logger.error("MongoDB URI not found in environment variables!")
    raise ValueError("MongoDB URI is required!")

try:
    mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)  # 5-second timeout
    mongo_client.server_info()  # Test the connection
    db = mongo_client['telegram_bot']
    logger.info("Connected to MongoDB successfully!")
except ServerSelectionTimeoutError as e:
    logger.error("Failed to connect to MongoDB. Please ensure MongoDB is running.")
    raise e
except Exception as e:
    logger.error(f"An error occurred while connecting to MongoDB: {e}")
    raise e

# Error logging function
def log_exception(context, error):
    logger.error(f"Exception in {context}: {error}")
    logger.error(traceback.format_exc())

# Start Command
async def start(update: Update, context: CallbackContext):
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
        await update.message.reply_text(
            "Welcome! Please share your phone number to complete registration.",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        )
    else:
        await update.message.reply_text("You are already registered!")

# Save Contact
async def save_contact(update: Update, context: CallbackContext):
    try:
        if update.message.contact:
            phone_number = update.message.contact.phone_number
            chat_id = update.message.contact.user_id
            db.users.update_one({"chat_id": chat_id}, {"$set": {"phone_number": phone_number}})
            logger.info(f"Phone number saved: {phone_number}")
            await update.message.reply_text("Thank you! Registration is now complete.", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("Please use the 'Share Phone Number' button to share your contact.")
    except Exception as e:
        log_exception("save_contact", e)
        await update.message.reply_text("An error occurred while saving your contact. Please try again.")

# Chat with Gemini API
async def gemini_chat(update: Update, context: CallbackContext):
    user_message = update.message.text
    chat_id = update.effective_chat.id

    try:
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(user_message)
        response_text = response.text

        # Save chat history to MongoDB
        db.chat_history.insert_one({
            "chat_id": chat_id,
            "user_message": user_message,
            "bot_response": response_text,
            "timestamp": datetime.now()
        })

        await update.message.reply_text(response_text)

    except Exception as e:
        logger.error(f"Exception in gemini_chat: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("I'm having trouble processing your request right now.")

# File Analysis (Images and PDFs)
# ... [Keep all previous imports and setup code unchanged] ...

# File Analysis (Images and PDFs)
async def analyze_file(update: Update, context: CallbackContext):
    file_id = update.message.document.file_id if update.message.document else update.message.photo[-1].file_id
    file_name = update.message.document.file_name if update.message.document else "image.jpg"

    try:
        # Get the file from Telegram
        file = await context.bot.get_file(file_id)
        file_url = file.file_path
        logger.info(f"Processing file: {file_url}")

        # Download the file content
        file_data = requests.get(file_url).content

        description = "Could not analyze this file type."

        # Initialize the updated model for vision analysis
        model = genai.GenerativeModel('gemini-1.5-flash')  # Use the updated model

        retry_count = 0
        max_retries = 3
        delay = 5  # seconds

        while retry_count < max_retries:
            try:
                if file_name.lower().endswith(('.png', '.jpg', '.jpeg')):  # Handle image files
                    # Process image with Gemini 1.5 Flash
                    img = Image.open(io.BytesIO(file_data))
                    
                    # Pass the PIL image object directly to the model
                    response = model.generate_content(
                        ["Analyze this image and provide a detailed description", img],
                        request_options={"timeout": 10}
                    )

                    description = response.text or "No description generated."
                    break  # Exit the loop if successful
                
                elif file_name.lower().endswith('.pdf'):  # Handle PDF files
                    response = model.generate_content(
                        "Analyze this PDF and summarize its contents.",
                        request_options={"timeout": 10}
                    )
                    description = response.text or "PDF analysis failed."
                    break  # Exit the loop if successful

            except Exception as e:
                logger.error(f"Error on attempt {retry_count + 1}: {str(e)}")
                if isinstance(e, RequestException) and e.response.status_code == 429:
                    # Handle the 429 error specifically
                    retry_count += 1
                    if retry_count < max_retries:
                        logger.info(f"Retrying in {delay} seconds...")
                        time.sleep(delay)  # Wait before retrying
                        delay *= 2  # Exponential backoff
                    else:
                        description = "Resource exhausted. Please try again later."
                        break
                else:
                    description = "An error occurred during processing."

        # Save to database
        db.file_metadata.insert_one({
            "chat_id": update.effective_chat.id,
            "file_name": file_name,
            "description": description,
            "timestamp": datetime.now()
        })

        await update.message.reply_text(f"Analysis Result:\n{description}")

    except Exception as e:
        logger.error(f"File analysis error: {str(e)}")
        await update.message.reply_text("Sorry, I couldn't analyze that file. Please try another image or PDF.")
        
# Web Search (Modified for Gemini API)
async def web_search(update: Update, context: CallbackContext):
    query = ' '.join(context.args)
    if not query:
        await update.message.reply_text("Please provide a search query after /websearch.")
        return

    try:
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(f"Perform a web search about: {query}")
        search_result = response.text or "No results found."

        await update.message.reply_text(f"Search Results:\n{search_result}")

    except Exception as e:
        log_exception("web_search", e)
        await update.message.reply_text("Unable to perform the search at the moment.")

# Referral System
async def referral_system(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = db.users.find_one({"chat_id": chat_id})
    if not user:
        await update.message.reply_text("Please register first using the /start command.")
        return

    try:
        referral_code = str(chat_id)[-6:]  # Simple referral code
        db.referrals.update_one(
            {"referral_code": referral_code},
            {"$set": {"referrer": user["username"], "timestamp": datetime.now()}},
            upsert=True
        )
        await update.message.reply_text(f"Your referral code is: {referral_code}\nShare it with friends to get rewards!")

    except Exception as e:
        log_exception("referral_system", e)
        await update.message.reply_text("Unable to generate a referral code. Please try again later.")

# Main Function (Updated for v20)
def main():
    try:
        # Create the application with the Telegram bot token
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Configure the scheduler's timezone
        app.job_queue.scheduler.configure(timezone=timezone)

        # Add handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.CONTACT, save_contact))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gemini_chat))
        app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, analyze_file))
        app.add_handler(CommandHandler("websearch", web_search))
        app.add_handler(CommandHandler("referral", referral_system))

        logger.info("Bot is running...")
        app.run_polling()

    except Exception as e:
        logger.error(f"An error occurred in the main function: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    main()
