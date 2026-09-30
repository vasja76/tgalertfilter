import asyncio
import os
import re
import threading
from datetime import datetime, timedelta
from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running 24/7!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))
SESSION_STRING = os.environ.get("SESSION_STRING")

# ИД сообщения с ключевыми словами
KEYWORDS_MESSAGE_ID = 3099291

# Дефолтный список ключевых слов (все строго в нижнем регистре)
DEFAULT_KEYWORDS = [
    "загроза балістики",
    "балістична загроза",
    "київ — спуск балістики",
    "київ - спуск балістики",

    "нивки",
    "нивок",
    "нивка",

    "онікс—м",
    "онікс-м",

    "циркони київ",
    "циркон київ",
    "циркон — київ",

    "київ кр",
    "кр київ",
    "зліт міг",
    "кинджал",

    "на київ ",
    "на київ!",
    "далі київ ",
    "на столицю",
    "вектор київ ",
    "вектор київ!",

    # "у київ ",
    # "до києва "
]

# Динамический список ключевых слов
KEYWORDS = list(DEFAULT_KEYWORDS)

TARGET_CHANNELS = [
    "@war_monitor",
    "@kievreal1",
    "@truexanewsua",
    # "@tgalertfilter"
]

client = TelegramClient(
    StringSession(SESSION_STRING), 
    API_ID, 
    API_HASH,
    connection_retries=5,
    retry_delay=3,
    auto_reconnect=True
)

HEARTBEAT_MESSAGE_ID = None
CURRENT_PULSE_COLOR = "🟢"
CURRENT_TICK = 0

def send_telegram_alert(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": MY_TELEGRAM_ID,
        "text": text
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Send err: {e}", flush=True)

def parse_keywords(text):
    parsed = []
    for line in text.splitlines():
        line_str = line.strip()
        if not line_str or line_str.startswith("#"):
            continue
        # Вытаскиваем содержимое между кавычками
        matches = re.findall(r'["\'](.*?)["\']', line_str)
        for match in matches:
            cleaned = match.strip().lower()
            if cleaned:
                parsed.append(cleaned)
    if not parsed:
        raise ValueError("Пустой список ключевых слов")
    return parsed

def update_heartbeat(color_symbol=None):
    global HEARTBEAT_MESSAGE_ID, CURRENT_PULSE_COLOR, CURRENT_TICK
    if color_symbol:
        CURRENT_PULSE_COLOR = color_symbol

    now_kyiv = (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M")
    text = f"{CURRENT_PULSE_COLOR} {now_kyiv} ({CURRENT_TICK})"
    
    if HEARTBEAT_MESSAGE_ID is None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "text": text,
            "disable_notification": True
        }
        try:
            res = requests.post(url, json=payload, timeout=10).json()
            if res.get("ok"):
                HEARTBEAT_MESSAGE_ID = res["result"]["message_id"]
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/unpinAllChatMessages", json={"chat_id": MY_TELEGRAM_ID}, timeout=10)
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage", json={"chat_id": MY_TELEGRAM_ID, "message_id": HEARTBEAT_MESSAGE_ID, "disable_notification": True}, timeout=10)
        except Exception as e:
            print(f"Pulse err: {e}", flush=True)
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": MY_TELEGRAM_ID,
            "message_id": HEARTBEAT_MESSAGE_ID,
            "text": text
        }
        try:
            res = requests.post(url, json=payload, timeout=10).json()
            if not res.get("ok"):
                HEARTBEAT_MESSAGE_ID = None
        except Exception as e:
            print(f"Pulse err: {e}", flush=True)

async def process_keywords_message(text):
    global KEYWORDS
    try:
        KEYWORDS = parse_keywords(text)
        update_heartbeat("🟡")
    except Exception as e:
        print(f"Ошибка парсинга ключей: {e}", flush=True)
        KEYWORDS = list(DEFAULT_KEYWORDS)
        update_heartbeat("🔴")

@client.on(events.MessageEdited(chats=MY_TELEGRAM_ID))
async def handle_message_edit(event):
    if event.id == KEYWORDS_MESSAGE_ID:
        await process_keywords_message(event.raw_text)

async def load_initial_keywords():
    try:
        msg = await client.get_messages(MY_TELEGRAM_ID, ids=KEYWORDS_MESSAGE_ID)
        if msg and msg.raw_text:
            await process_keywords_message(msg.raw_text)
    except Exception as e:
        print(f"Ошибка при первичном чтении сообщения {KEYWORDS_MESSAGE_ID}: {e}", flush=True)

async def heartbeat_loop():
    global CURRENT_TICK, CURRENT_PULSE_COLOR
    while True:
        CURRENT_TICK += 1
        # Если текущий статус желтый (сигнал об изменении), возвращаем обычный зеленый
        if CURRENT_PULSE_COLOR == "🟡":
            CURRENT_PULSE_COLOR = "🟢"
        update_heartbeat()
        await asyncio.sleep(600)

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):
    message_text = event.raw_text
    text_lower = message_text.lower()
    
    if any(keyword in text_lower for keyword in KEYWORDS):
        chat = await client.get_entity(event.chat_id)
        username = chat.username or (chat.usernames[0].username if getattr(chat, 'usernames', None) else None)
        channel_id = f"@{username}" if username else getattr(chat, 'title', 'Канал')
        
        alert_msg = f"▶ {channel_id}\n{message_text}"
        send_telegram_alert(alert_msg)

async def start_telethon():
    print("Запуск Telethon...", flush=True)
    try:
        await client.start()
        print("Telethon запущен!", flush=True)
        await load_initial_keywords()
        asyncio.create_task(heartbeat_loop())
        await client.run_until_disconnected()
    except Exception as e:
        print(f"Telethon err: {e}", flush=True)
        
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(start_telethon())
