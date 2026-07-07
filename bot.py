import os
import sys
import logging
import asyncio
import threading
import uuid
import atexit
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from queue import Queue
from datetime import datetime
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import telebot
from telebot import types, apihelper
import edge_tts

# ==================== CONFIGURATION ====================
class Config:
    BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "8833097044:AAEDc-nEP9a4yg9SgGsQHjdhfL7ShTbKja4")
    MAX_TEXT_LENGTH: int = 4000
    TEMP_DIR: str = "temp_audio"
    MAX_WORKERS: int = 4
    
    VOICES: Dict[str, Dict[str, str]] = {
        "am": {
            "ወንድ (አሜሃ)": "am-ET-AmehaNeural",
            "ሴት (መቅደስ)": "am-ET-MekdesNeural"
        },
        "en": {
            "ህፃን (Girl)": "en-US-AnaNeural",
            "ወጣት (Woman)": "en-US-JennyNeural",
            "አዋቂ (Man)": "en-US-ChristopherNeural",
            "ሽማግሌ (Man)": "en-US-GuyNeural",
            "አሮጊት (Woman)": "en-US-AriaNeural"
        }
    }
    
    SPEED_OPTIONS = {
        "በጣም ቀርፋፋ": "-50%", "ቀርፋፋ": "-20%", "መደበኛ": "+0%",
        "ፈጣን": "+20%", "በጣም ፈጣን": "+50%"
    }

# ==================== LOGGING & SETUP ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.path.exists(Config.TEMP_DIR):
    os.makedirs(Config.TEMP_DIR)

bot = telebot.TeleBot(Config.BOT_TOKEN, parse_mode="Markdown")
executor = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS)

# ==================== SESSION & QUEUE ====================
class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.queues = {}
        self.lock = threading.Lock()

    def get_user_data(self, user_id):
        with self.lock:
            if user_id not in self.sessions:
                self.sessions[user_id] = {"lang": None, "voice": None, "speed": "+0%", "is_processing": False}
                self.queues[user_id] = []
            return self.sessions[user_id], self.queues[user_id]

session_manager = SessionManager()

# ==================== AUDIO PROCESSING ====================
async def generate_audio_file(text: str, voice: str, speed: str, progress_func) -> str:
    filename = os.path.join(Config.TEMP_DIR, f"{uuid.uuid4()}.mp3")
    communicate = edge_tts.Communicate(text, voice, rate=speed)
    
    # የሂደት መቶኛ መቁጠሪያ
    for i in range(1, 101, 20):
        await progress_func(i)
        await asyncio.sleep(0.2)
        
    await communicate.save(filename)
    await progress_func(100)
    return filename

# ==================== MESSAGE HANDLERS ====================

@bot.message_handler(commands=['start'])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang_am"),
               types.InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"))
    bot.send_message(message.chat.id, "ሰላም! ቋንቋ ይምረጡ:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.message.chat.id
    session, _ = session_manager.get_user_data(user_id)
    
    if call.data.startswith("lang_"):
        session["lang"] = call.data.split("_")[1]
        markup = types.InlineKeyboardMarkup(row_width=2)
        for name, vid in Config.VOICES[session["lang"]].items():
            markup.add(types.InlineKeyboardButton(name, callback_data=f"voice_{vid}"))
        bot.edit_message_text("ድምጽ ይምረጡ:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        
    elif call.data.startswith("voice_"):
        session["voice"] = call.data.split("_")[1]
        markup = types.InlineKeyboardMarkup(row_width=2)
        for name, val in Config.SPEED_OPTIONS.items():
            markup.add(types.InlineKeyboardButton(name, callback_data=f"speed_{val}"))
        bot.edit_message_text("ፍጥነት ይምረጡ:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        
    elif call.data.startswith("speed_"):
        session["speed"] = call.data.split("_")[1]
        bot.edit_message_text("✅ ዝግጅት ተጠናቋል! ጽሁፍ ይላኩ።", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda m: True)
def text_handler(message):
    user_id = message.chat.id
    session, queue = session_manager.get_user_data(user_id)
    
    if not session["voice"]:
        bot.reply_to(message, "እባክዎ መጀመሪያ /start በመጫን ቅንብሮችን ያጠናቅቁ።")
        return

    queue.append(message.text)
    if not session["is_processing"]:
        executor.submit(process_queue, user_id, message.chat.id)

def process_queue(user_id, chat_id):
    session, queue = session_manager.get_user_data(user_id)
    session["is_processing"] = True
    
    while queue:
        text = queue.pop(0)
        msg = bot.send_message(chat_id, "⏳ ድምጽ እየተዘጋጀ ነው... (0%)")
        
        async def update_progress(percent):
            try:
                bot.edit_message_text(f"⏳ ድምጽ እየተዘጋጀ ነው... ({percent}%)", chat_id, msg.message_id)
            except: pass

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            audio_path = loop.run_until_complete(generate_audio_file(text, session["voice"], session["speed"], update_progress))
            loop.close()
            
            with open(audio_path, 'rb') as f:
                bot.send_audio(chat_id, f)
            bot.delete_message(chat_id, msg.message_id)
            os.remove(audio_path)
        except Exception as e:
            bot.edit_message_text(f"❌ ስህተት: {e}", chat_id, msg.message_id)
            
    session["is_processing"] = False

if __name__ == "__main__":
    print("🚀 ቦቱ እየሰራ ነው...")
    bot.infinity_polling()

