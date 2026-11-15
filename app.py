import os
import sys
import time
import logging
import subprocess
import threading
import psutil
from PIL import Image, ImageDraw
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, render_template_string

# ==================== КОНФИГ ====================


if not TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")

logging.basicConfig(
    filename='bot_errors.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

UPLOAD_FOLDER = 'uploaded_scripts'
ASSETS_FOLDER = 'asets'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ASSETS_FOLDER, exist_ok=True)

REQUIRED_LIB = "telebot"
START_TIME = time.time()
user_states = {}

# 👇 НОВОЕ: квота 20 МБ НА КАЖДОГО пользователя
USER_QUOTA_BYTES = 20 * 1024 * 1024  # 20 МБ
# Словарь: {user_id: {"used_bytes": int}}  — можно пересчитывать из файлов
user_quota = {}

def get_user_script_path(user_id: int) -> str:
    return os.path.join(UPLOAD_FOLDER, f"script_user_{user_id}.py")

def get_user_used_bytes(user_id: int) -> int:
    """Считает, сколько байт занял конкретный пользователь."""
    path = get_user_script_path(user_id)
    if os.path.exists(path):
        return os.path.getsize(path)
    return 0

def get_user_remaining_mb(user_id: int) -> float:
    used = get_user_used_bytes(user_id)
    return max(0.0, round((USER_QUOTA_BYTES - used) / (1024 * 1024), 2))

def get_user_used_mb(user_id: int) -> float:
    return round(get_user_used_bytes(user_id) / (1024 * 1024), 2)

def check_quota(user_id: int, new_bytes: int) -> bool:
    """True, если после записи new_bytes пользователь не выйдет за 20 МБ."""
    return get_user_used_bytes(user_id) + new_bytes <= USER_QUOTA_BYTES

# Активные боты: {user_id: {"process": Popen, "file_path": path}}
active_hosted_bots = {}

# ==================== МОНИТОРИНГ CPU ====================
def monitor_hosted_bots():
    while True:
        try:
            for user_id, bot_info in list(active_hosted_bots.items()):
                proc = bot_info["process"]
                if proc.poll() is not None:
                    active_hosted_bots.pop(user_id, None)
                    continue
                try:
                    p_util = psutil.Process(proc.pid)
                    cpu_usage = p_util.cpu_percent(interval=1)
                except psutil.NoSuchProcess:
                    active_hosted_bots.pop(user_id, None)
                    continue

                if cpu_usage > 90.0:
                    logging.warning(f"Бот {user_id} (PID {proc.pid}) грузит CPU {cpu_usage}%")
                    proc.terminate()
                    active_hosted_bots.pop(user_id, None)
                    try:
                        bot.send_message(
                            ADMIN_CHAT_ID,
                            f"🚨 АВАРИЙНОЕ ОТКЛЮЧЕНИЕ (Hostander)\n"
                            f"👤 ID: {user_id}\n⚠️ CPU {cpu_usage}% — процесс остановлен."
                        )
                    except Exception as ex:
                        logging.error(f"Не смог уведомить админа: {ex}")
        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
        time.sleep(3)

threading.Thread(target=monitor_hosted_bots, daemon=True).start()

# ==================== МИНИ-САЙТ ====================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><title>Hostander</title>
<style>
body{font-family:Arial;background:#181c24;color:#fff;text-align:center;padding:50px}
.container{max-width:600px;margin:auto;background:#222831;padding:30px;border-radius:10px;border:1px solid #4682b4}
h1{color:#4682b4}p{color:#b0c4de}
.btn{display:inline-block;margin-top:20px;padding:10px 20px;background:#4682b4;color:#fff;text-decoration:none;border-radius:5px}
</style></head>
<body><div class="container">
<h1>🚀 Hostander Hosting</h1>
<p>Добро пожаловать на платформу хостинга ботов <b>Hostander</b>!</p>
<p><b>Активных ботов:</b> {{ active_count }}</p>
<a href="/site-status" class="btn">Системные метрики</a>
</div></body></html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE, active_count=len(active_hosted_bots))

@app.route('/site-status')
def site_status():
    uptime = int(time.time() - START_TIME)
    hours, minutes = uptime // 3600, (uptime % 3600) // 60
    total_used = sum(
        os.path.getsize(os.path.join(UPLOAD_FOLDER, f))
        for f in os.listdir(UPLOAD_FOLDER)
        if os.path.isfile(os.path.join(UPLOAD_FOLDER, f))
    )
    return (
        f"<body style='font-family:Arial;background:#181c24;color:#fff;text-align:center;padding-top:50px'>"
        f"<h2>📊 Hostander — Системные метрики</h2>"
        f"<p>🤖 Активных ботов: {len(active_hosted_bots)}</p>"
        f"<p>💾 Общий диск: {round(total_used/1024/1024, 2)} МБ</p>"
        f"<p>📦 Квота на пользователя: {USER_QUOTA_BYTES // (1024*1024)} МБ</p>"
        f"<p>⏱ Uptime: {hours}ч {minutes}м</p>"
        f"<br><a href='/' style='color:#4682b4'>← На главную</a></body>"
    )

@app.route('/health')
def health():
    return "OK", 200

# ==================== ХЕЛПЕРЫ ====================
def send_asset_photo(chat_id, filename, caption=""):
    path = os.path.join(ASSETS_FOLDER, filename)
    if os.path.exists(path):
        with open(path, 'rb') as photo:
            bot.send_photo(chat_id, photo, caption=caption, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, caption, parse_mode="Markdown")

# ==================== КОМАНДЫ ====================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_states[user_id] = {"state": "waiting_for_token"}
    caption = (
        "🚀 Добро пожаловать в Hostander!\n\n"
        "🤖 Я помогу запустить вашего Telegram-бота.\n\n"
        "📖 Инструкция:\n"
        "1. Создайте бота через @BotFather.\n"
        "2. Отправьте токен мне.\n"
        "3. Отправьте текст или файл .py вашего скрипта.\n\n"
        f"💡 У каждого пользователя личная квота — {USER_QUOTA_BYTES//(1024*1024)} МБ.\n"
        "Используйте /help, если запутаетесь."
    )
    send_asset_photo(message.chat.id, "start.png", caption)

@bot.message_handler(commands=['help'])
def send_help(message):
    bot.reply_to(message, (
        "📚 Справка Hostander:\n\n"
        "🚀 /start — начать мастер добавления бота\n"
        "❓ /help — эта справка\n"
        "📊 /status — ваша карточка квоты и состояния\n"
        "📋 /logs — лог ошибок системы (для админа)\n\n"
        f"📦 Ваша личная квота: {USER_QUOTA_BYTES//(1024*1024)} МБ"
    ), parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def send_status_card(message):
    try:
        user_id = message.from_user.id
        uptime = int(time.time() - START_TIME)
        hours, minutes = uptime // 3600, (uptime % 3600) // 60

        used_mb = get_user_used_mb(user_id)
        remaining_mb = get_user_remaining_mb(user_id)
        limit_mb = USER_QUOTA_BYTES // (1024 * 1024)

        if remaining_mb < 3.0:
            send_asset_photo(
                message.chat.id, "memorywarning.png",
                f"⚠️ Hostander: у вас осталось {remaining_mb} МБ из {limit_mb} МБ."
            )

        img_w, img_h = 600, 380
        image = Image.new("RGB", (img_w, img_h), color=(24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle([15, 15, img_w - 15, img_h - 15], outline=(70, 130, 180), width=2)
        draw.text((35, 35), "🚀 Hostander — Статус", fill=(255, 255, 255))

        stats = [
            f"👤 ID: {user_id}",
            f"💾 Ваша квота: {used_mb} МБ / {limit_mb} МБ",
            f"📦 Свободно у вас: {remaining_mb} МБ",
            f"🤖 Ботов запущено у вас: {1 if user_id in active_hosted_bots else 0}",
            f"⏱ Uptime хостинга: {hours}ч {minutes}м",
        ]
        y = 90
        for s in stats:
            draw.text((35, y), s, fill=(200, 210, 230))
            y += 45

        image_path = f"status_{user_id}.png"
        image.save(image_path)
        with open(image_path, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, caption="📈 Ваши показатели:")
        os.remove(image_path)
    except Exception:
        logging.exception("Ошибка статуса")

@bot.message_handler(commands=['logs'])
def send_logs(message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return bot.reply_to(message, "⛔ Только для админа.")
    log_file = 'bot_errors.log'
    if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
        with open(log_file, 'rb') as f:
            bot.send_document(message.chat.id, f, caption="📋 Логи:")
    else:
        bot.reply_to(message, "✅ Лог пуст.")

# ==================== ОБРАБОТКА ТЕКСТА / ДОКУМЕНТА ====================
@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text
    if text.startswith('/'):
        return

    user_data = user_states.get(user_id, {})
    state = user_data.get("state")

    try:
        if state == "waiting_for_token":
            if ":" not in text or len(text) < 15:
                return bot.reply_to(message, "❌ Неверный формат токена.")
            user_states[user_id] = {"state": "waiting_for_script", "bot_token": text.strip()}
            send_asset_photo(message.chat.id, "botsaved.png",
                             "✅ Токен сохранён!\n👉 Отправьте текст Python-скрипта или файл .py.")
            return

        elif state == "waiting_for_script":
            if REQUIRED_LIB not in text:
                return bot.reply_to(message, f"❌ В коде нет библиотеки {REQUIRED_LIB}.")

            bot_token = user_data.get("bot_token")
            file_path = get_user_script_path(user_id)
            full_code = f"# Token: {bot_token}\n" + text
            encoded = full_code.encode("utf-8")

            # 👇 ПРОВЕРКА ИНДИВИДУАЛЬНОЙ КВОТЫ 20 МБ
            if not check_quota(user_id, len(encoded)):
                return bot.reply_to(
                    message,
                    f"❌ Превышена ваша личная квота {USER_QUOTA_BYTES//(1024*1024)} МБ.\n"
                    f"Занято: {get_user_used_mb(user_id)} МБ."
                )

            with open(file_path, 'wb') as f:
                f.write(encoded)

            send_to_admin(message, file_path, bot_token)
            user_states.pop(user_id, None)
    except Exception:
        logging.exception("Ошибка текста")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    try:
        user_id = message.from_user.id
        user_data = user_states.get(user_id, {})
        if user_data.get("state") != "waiting_for_script":
            return

        document = message.document
        if not document.file_name.endswith('.py'):
            return bot.reply_to(message, "❌ Нужен файл .py")

        # 👇 ИНДИВИДУАЛЬНАЯ КВОТА: проверяем размер ДО скачивания
        if document.file_size > USER_QUOTA_BYTES:
            return bot.reply_to(
                message,
                f"❌ Файл больше вашей квоты {USER_QUOTA_BYTES//(1024*1024)} МБ."
            )

        file_info = bot.get_file(document.file_id)
        downloaded = bot.download_file(file_info.file_path)

        if not check_quota(user_id, len(downloaded)):
            return bot.reply_to(
                message,
                f"❌ Превышена ваша личная квота {USER_QUOTA_BYTES//(1024*1024)} МБ.\n"
                f"Занято: {get_user_used_mb(user_id)} МБ."
            )

        file_path = get_user_script_path(user_id)
        with open(file_path, 'wb') as f:
            f.write(downloaded)

        with open(file_path, 'r', encoding='utf-8') as f:
            code = f.read()
        if REQUIRED_LIB not in code:
            return bot.reply_to(message, f"❌ В коде нет библиотеки {REQUIRED_LIB}.")

        send_to_admin(message, file_path, user_data.get("bot_token"))
        user_states.pop(user_id, None)
    except Exception:
        logging.exception("Ошибка документа")

def send_to_admin(message, file_path, bot_token):
    user_id = message.from_user.id
    send_asset_photo(message.chat.id, "senttocheck.png",
                     "🎉 Скрипт отправлен на модерацию администратору!")

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user_id}")
    )
    with open(file_path, 'rb') as f:
        bot.send_document(
            ADMIN_CHAT_ID, f,
            caption=f"🔔 Заявка от {user_id}\n🔑 Токен: {bot_token}\n"
                    f"📦 Занято у пользователя: {get_user_used_mb(user_id)} МБ / "
                    f"{USER_QUOTA_BYTES//(1024*1024)} МБ"
        )

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.from_user.id != ADMIN_CHAT_ID:
        return
    action, target_id = call.data.split("_")
    target_id = int(target_id)

    if action == "approve":
        file_path = get_user_script_path(target_id)
        proc_pid = "-"
        if os.path.exists(file_path):
            if target_id in active_hosted_bots:
                try:
                    active_hosted_bots[target_id]["process"].terminate()
                except Exception:
                    pass

            # 👇 Запуск через тот же интерпретатор, что и основной процесс
            proc = subprocess.Popen([sys.executable, file_path])
            active_hosted_bots[target_id] = {"process": proc, "file_path": file_path}
            proc_pid = proc.pid

        send_asset_photo(target_id, "checked.png",
                         "🎉 Ваш бот одобрен и запущен в фоне!")
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=f"{call.message.caption}\n\nСтатус: ✅ Запущено (PID: {proc_pid})",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Бот запущен!")

    elif action == "reject":
        if target_id in active_hosted_bots:
            try:
                active_hosted_bots[target_id]["process"].terminate()
            except Exception:
                pass
            active_hosted_bots.pop(target_id, None)

        # По желанию: освободить квоту при отклонении
        # os.remove(get_user_script_path(target_id)) если файл есть

        send_asset_photo(target_id, "rejected.png",
                         "❌ Ваша заявка отклонена администратором.")
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=f"{call.message.caption}\n\nСтатус: ❌ Отклонено",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Отклонено.")

# ==================== WEBHOOK ====================
@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Forbidden', 403

@app.route('/set_webhook')
def set_webhook():
    if not WEBHOOK_URL:
        return "❌ Не задан WEBHOOK_URL / RENDER_EXTERNAL_URL", 500
    url = f"{WEBHOOK_URL.rstrip('/')}/{TOKEN}"
    bot.remove_webhook()
    ok = bot.set_webhook(url=url)
    return (f"✅ Вебхук установлен: {url}" if ok else "❌ Ошибка установки"), 200

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    # На Render переменная PORT задаётся автоматически
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
