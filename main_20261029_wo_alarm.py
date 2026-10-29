import asyncio
import os
import re
import threading

from datetime import datetime, timezone, timedelta

from flask import Flask

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import requests


# ============================================================
# FLASK — простой веб-сервер для проверки, что приложение работает
# ============================================================

app = Flask(__name__)


@app.route('/')
def home():

    # Render/другой хостинг получает ответ от этого адреса,
    # поэтому приложение считается работающим.
    return "Bot is running 24/7!", 200


def run_flask():

    # Порт берём из переменной окружения.
    # Если переменная PORT отсутствует — используется 10000.
    port = int(os.environ.get("PORT", 10000))

    # Запускаем Flask отдельно от Telethon,
    # чтобы веб-сервер не блокировал работу Telegram-клиента.
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ============================================================
# TELEGRAM / ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================

# API_ID и API_HASH — данные Telegram API.
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

# BOT_TOKEN используется для отправки сообщений
# через официальный Bot API.
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Telegram ID пользователя, которому бот отправляет уведомления
# и в личном чате которого находится сообщение с ключевыми словами.
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID"))

# StringSession позволяет Telethon подключаться
# без повторной авторизации по номеру телефона.
SESSION_STRING = os.environ.get("SESSION_STRING")


# ============================================================
# СООБЩЕНИЕ С КЛЮЧЕВЫМИ СЛОВАМИ
# ============================================================

# ID сообщения, которое бот должен отслеживать.
#
# Это именно Telegram message_id, а не ID пользователя
# и не ID чата.
KEYWORDS_MESSAGE_ID = 99418


# ============================================================
# ДЕФОЛТНЫЙ СПИСОК КЛЮЧЕВЫХ СЛОВ
# ============================================================

# Этот список используется:
# 1. при первом запуске;
# 2. если сообщение с ключевыми словами не удалось обработать.
#
# Все ключевые слова хранятся в нижнем регистре,
# поэтому входящие сообщения также переводятся в нижний регистр.
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


# ============================================================
# ТЕКУЩИЙ ДИНАМИЧЕСКИЙ СПИСОК КЛЮЧЕВЫХ СЛОВ
# ============================================================

# В обычной работе именно этот список используется
# для проверки новых сообщений из каналов.
#
# В начале работы он является копией DEFAULT_KEYWORDS.
# После чтения сообщения KEYWORDS_MESSAGE_ID
# список может быть заменён содержимым этого сообщения.
KEYWORDS = list(DEFAULT_KEYWORDS)


# ============================================================
# КАНАЛЫ, КОТОРЫЕ НЕОБХОДИМО ОТСЛЕЖИВАТЬ
# ============================================================

TARGET_CHANNELS = [

    "@war_monitor",

    "@kievreal1",

    "@truexanewsua",

    # "@tgalertfilter"

]


# ============================================================
# TELETHON CLIENT
# ============================================================

# Создаём Telegram-клиент.
#
# StringSession содержит уже сохранённую авторизацию,
# поэтому клиент может подключаться автоматически.
#
# connection_retries — количество попыток повторного подключения.
# retry_delay — задержка между попытками.
# auto_reconnect — автоматически восстанавливать соединение.
client = TelegramClient(

    StringSession(SESSION_STRING),

    API_ID,

    API_HASH,

    connection_retries=5,

    retry_delay=3,

    auto_reconnect=True

)


# ============================================================
# СОСТОЯНИЕ ПУЛЬСА / HEARTBEAT
# ============================================================

# ID сообщения, которое используется как "пульс".
#
# При первом запуске сообщение ещё не существует,
# поэтому значение None.
#
# После создания сообщения сюда записывается его message_id.
HEARTBEAT_MESSAGE_ID = None


# Текущий цвет состояния пульса.
#
# 🟢 — обычная работа.
# 🟡 — были изменены ключевые слова.
# 🔴 — произошла ошибка при обработке ключевых слов.
CURRENT_PULSE_COLOR = "🟢"


# Счётчик циклов heartbeat.
CURRENT_TICK = 0


# ID Telegram-аккаунта бота.
#
# Используется для определения личного чата,
# в котором находится отслеживаемое сообщение.
BOT_USER_ID = None


# ============================================================
# ОТПРАВКА УВЕДОМЛЕНИЯ
# ============================================================

def send_telegram_alert(text):

    # Используем Telegram Bot API для отправки уведомления.
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {

        "chat_id": MY_TELEGRAM_ID,

        "text": text

    }

    try:

        requests.post(
            url,
            json=payload,
            timeout=10
        )

    except Exception as e:

        # Ошибка отправки уведомления не должна
        # останавливать весь Telegram-клиент.
        print(f"Send err: {e}", flush=True)


# ============================================================
# РАЗБОР СООБЩЕНИЯ С КЛЮЧЕВЫМИ СЛОВАМИ
# ============================================================

def parse_keywords(text):

    parsed = []

    # Обрабатываем сообщение построчно.
    for line in text.splitlines():

        line_str = line.strip()

        # Пустые строки и строки-комментарии пропускаем.
        if not line_str or line_str.startswith("#"):

            continue

        # Вытаскиваем содержимое между кавычками.
        #
        # Например:
        # "київ"
        #
        # превратится в:
        # київ
        matches = re.findall(r'["\'](.*?)["\']', line_str)

        if matches:

            # Если в строке есть кавычки,
            # используем найденное содержимое.
            for match in matches:

                cleaned = match.lower()

                if cleaned:

                    parsed.append(cleaned)

        else:

            # Если кавычек нет, используем всю строку
            # как ключевое слово.
            cleaned = line_str.lower()

            if cleaned:

                parsed.append(cleaned)


    # Если после обработки ничего не получили,
    # считаем сообщение некорректным.
    if not parsed:

        raise ValueError("Пустой список ключевых слов")


    return parsed


# ============================================================
# ОБНОВЛЕНИЕ ПУЛЬСА
# ============================================================

def update_heartbeat(color_symbol=None):

    global HEARTBEAT_MESSAGE_ID
    global CURRENT_PULSE_COLOR
    global CURRENT_TICK


    # Если передан новый цвет — меняем текущее состояние.
    if color_symbol:

        CURRENT_PULSE_COLOR = color_symbol


    # Получаем текущее время UTC и переводим его
    # в киевское время (UTC+3).
    now_kyiv = (
        datetime.now(timezone.utc) + timedelta(hours=3)
    ).strftime("%H:%M")


    # Формат сообщения пульса:
    #
    # 🟢 11:30 (5)
    #
    # Цвет + время + номер цикла.
    text = f"{CURRENT_PULSE_COLOR} {now_kyiv} ({CURRENT_TICK})"


    # ========================================================
    # ЕСЛИ ПУЛЬС ЕЩЁ НЕ СОЗДАН
    # ========================================================

    if HEARTBEAT_MESSAGE_ID is None:

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        payload = {

            "chat_id": MY_TELEGRAM_ID,

            "text": text,

            # Пульс не должен давать звуковое уведомление.
            "disable_notification": True

        }

        try:

            res = requests.post(
                url,
                json=payload,
                timeout=10
            ).json()


            if res.get("ok"):

                # Запоминаем ID созданного сообщения.
                HEARTBEAT_MESSAGE_ID = res["result"]["message_id"]


                # Закрепляем ровно один раз при отправке нового сообщения.
                requests.post(

                    f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",

                    json={
                        "chat_id": MY_TELEGRAM_ID,
                        "message_id": HEARTBEAT_MESSAGE_ID,
                        "disable_notification": True
                    },

                    timeout=10

                )


        except Exception as e:

            print(f"Pulse err: {e}", flush=True)


    # ========================================================
    # ЕСЛИ ПУЛЬС УЖЕ СУЩЕСТВУЕТ
    # ========================================================

    else:

        # Просто редактируем текст — Telegram сам обновит
        # плашку закреплённого сообщения вверху.
        #
        # Новое сообщение при каждом цикле НЕ создаётся.
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"


        payload = {

            "chat_id": MY_TELEGRAM_ID,

            "message_id": HEARTBEAT_MESSAGE_ID,

            "text": text

        }


        try:

            res = requests.post(
                url,
                json=payload,
                timeout=10
            ).json()


            # Если Telegram сообщает об ошибке редактирования,
            # сбрасываем ID.
            #
            # При следующем обновлении будет создано
            # новое сообщение пульса.
            if not res.get("ok"):

                HEARTBEAT_MESSAGE_ID = None


        except Exception as e:

            print(f"Pulse err: {e}", flush=True)


# ============================================================
# ОБРАБОТКА СООБЩЕНИЯ С КЛЮЧЕВЫМИ СЛОВАМИ
# ============================================================

async def process_keywords_message(text):

    global KEYWORDS

    try:

        # Пытаемся разобрать содержимое сообщения
        # и заменить текущий список ключевых слов.
        KEYWORDS = parse_keywords(text)


        # Выводим в лог, какие ключевые слова реально загрузились.
        print(
            f"Ключевые слова загружены ({len(KEYWORDS)} шт.): {KEYWORDS}",
            flush=True
        )


        # Жёлтый пульс означает:
        # сообщение с настройками было изменено
        # и новые ключевые слова успешно загружены.
        update_heartbeat("🟡")


    except Exception as e:

        print(
            f"Ошибка парсинга ключей: {e}",
            flush=True
        )


        # Если новые ключевые слова некорректны,
        # возвращаем рабочий список по умолчанию.
        KEYWORDS = list(DEFAULT_KEYWORDS)


        # Красный пульс показывает ошибку.
        update_heartbeat("🔴")


# ============================================================
# ОТСЛЕЖИВАНИЕ РЕДАКТИРОВАНИЯ СООБЩЕНИЯ
# ============================================================

@client.on(events.MessageEdited)
async def handle_message_edit(event):

    # Нас интересуют только сообщения в личном чате
    # с нужным Telegram-аккаунтом.
    #
    # BOT_USER_ID определяется при первоначальной загрузке.
    if event.is_private and (
        BOT_USER_ID is None or event.chat_id == BOT_USER_ID
    ):

        # Проверяем именно ID нужного сообщения.
        #
        # Любые другие сообщения игнорируются.
        if event.id == KEYWORDS_MESSAGE_ID:

            # Если сообщение изменилось —
            # заново читаем его содержимое.
            await process_keywords_message(event.raw_text)


# ============================================================
# ПЕРВОНАЧАЛЬНАЯ ЗАГРУЗКА КЛЮЧЕВЫХ СЛОВ
# ============================================================

async def load_initial_keywords():

    global BOT_USER_ID

    try:

        # Получаем информацию о боте через Bot API.
        #
        # Это позволяет узнать username бота,
        # после чего через Telethon получаем его Telegram entity.
        res = requests.get(

            f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",

            timeout=10

        ).json()


        if res.get("ok"):

            bot_username = res["result"]["username"]


            # Получаем Telegram entity бота.
            bot_entity = await client.get_entity(bot_username)


            # Сохраняем числовой ID бота.
            #
            # Он затем используется в MessageEdited.
            BOT_USER_ID = bot_entity.id


            # Читаем конкретное сообщение по его message_id.
            msg = await client.get_messages(
                bot_entity,
                ids=KEYWORDS_MESSAGE_ID
            )


            if msg and msg.raw_text:

                # Если сообщение существует и содержит текст —
                # загружаем из него ключевые слова.
                await process_keywords_message(msg.raw_text)

            else:

                print(
                    f"Сообщение {KEYWORDS_MESSAGE_ID} не найдено.",
                    flush=True
                )


        else:

            print(
                "Не удалось получить данные бота через getMe API",
                flush=True
            )


    except Exception as e:

        print(
            f"Ошибка при считывании сообщения {KEYWORDS_MESSAGE_ID}: {e}",
            flush=True
        )


# ============================================================
# ЦИКЛ HEARTBEAT / ПУЛЬСА
# ============================================================

async def heartbeat_loop():

    global CURRENT_TICK
    global CURRENT_PULSE_COLOR


    # Бесконечный цикл.
    # Работает всё время, пока запущен Telethon.
    while True:

        # Увеличиваем номер цикла.
        CURRENT_TICK += 1


        # Если предыдущий цикл установил жёлтый цвет
        # из-за изменения ключевых слов,
        # при следующем обычном пульсе возвращаем зелёный.
        if CURRENT_PULSE_COLOR == "🟡":

            CURRENT_PULSE_COLOR = "🟢"


        # Обновляем существующее сообщение пульса.
        update_heartbeat()


        # Следующее обновление через 10 минут.
        await asyncio.sleep(600)


# ============================================================
# ОТСЛЕЖИВАНИЕ НОВЫХ СООБЩЕНИЙ В ЦЕЛЕВЫХ КАНАЛАХ
# ============================================================

@client.on(events.NewMessage(chats=TARGET_CHANNELS))
async def handle_new_message(event):

    # Получаем текст нового сообщения.
    message_text = event.raw_text


    # Переводим текст в нижний регистр,
    # чтобы поиск не зависел от регистра букв.
    text_lower = message_text.lower()


    # Проверяем наличие хотя бы одного ключевого слова.
    #
    # Если найдено совпадение — отправляем уведомление.
    if any(keyword in text_lower for keyword in KEYWORDS):

        # Получаем информацию о канале.
        chat = await client.get_entity(event.chat_id)


        # Сначала пытаемся получить обычный username.
        #
        # Если его нет — пробуем получить username
        # из списка дополнительных usernames.
        username = (
            chat.username
            or (
                chat.usernames[0].username
                if getattr(chat, 'usernames', None)
                else None
            )
        )


        # Формируем обозначение канала.
        #
        # Если username существует:
        # @channel
        #
        # Если username отсутствует:
        # используется название канала.
        channel_id = (
            f"@{username}"
            if username
            else getattr(chat, 'title', 'Канал')
        )


        # Формируем текст уведомления.
        alert_msg = f"▶ {channel_id}\n{message_text}"


        # Отправляем уведомление в личный чат.
        send_telegram_alert(alert_msg)


# ============================================================
# ЗАПУСК TELETHON
# ============================================================

async def start_telethon():

    print("Запуск Telethon...", flush=True)

    try:

        # Подключаемся к Telegram.
        await client.start()

        print("Telethon запущен!", flush=True)


        # При запуске сначала читаем существующее
        # сообщение с ключевыми словами.
        await load_initial_keywords()


        # Запускаем heartbeat в отдельной asyncio-задаче,
        # чтобы он одновременно работал с обработчиками Telegram.
        asyncio.create_task(heartbeat_loop())


        # Остаёмся подключёнными к Telegram
        # до момента отключения клиента.
        await client.run_until_disconnected()


    except Exception as e:

        print(f"Telethon err: {e}", flush=True)


# ============================================================
# ГЛАВНАЯ ТОЧКА ЗАПУСКА
# ============================================================

if __name__ == "__main__":

    # Flask запускается в отдельном daemon-потоке,
    # чтобы одновременно работал Telegram-клиент.
    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()


    # Основной asyncio-цикл для Telethon.
    asyncio.run(start_telethon())
