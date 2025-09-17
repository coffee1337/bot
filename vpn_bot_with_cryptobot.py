# vpn_bot_with_cryptobot.py
import sqlite3
import logging
import json
import os
import uuid
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
import requests
from telegram import LabeledPrice
from telegram.ext import PreCheckoutQueryHandler

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация бота
try:
    from dotenv import load_dotenv
    load_dotenv()
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    ADMIN_ID = int(os.environ["ADMIN_ID"])
    CRYPTO_BOT_TOKEN = os.environ["CRYPTO_BOT_TOKEN"]
    CRYSTAL_PAY_LOGIN = os.environ.get("CRYSTAL_PAY_LOGIN", "")
    CRYSTAL_PAY_SECRET = os.environ.get("CRYSTAL_PAY_SECRET", "")
    STARS_PER_USDT = float(os.environ.get("STARS_PER_USDT", "70"))
    RUB_PER_USDT = float(os.environ.get("RUB_PER_USDT", "100"))  # курс: сколько RUB за 1 USDT
    CHANNEL_ID = os.environ.get("CHANNEL_ID", "@EcliptVPN")  # ID канала для обязательной подписки
except (KeyError, ImportError) as e:
    logger.error(f"Отсутствует переменная окружения или dotenv: {e}")
    logger.error("Пожалуйста, установите переменные окружения: BOT_TOKEN, ADMIN_ID, CRYPTO_BOT_TOKEN")
    exit(1)

CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api"
CRYSTAL_PAY_API_URL = "https://api.crystalpay.io/v2"

# Фиксированные цены в звёздах для тарифов
STARS_PRICE_BY_PLAN = {
    1: 50,    # 1 месяц
    2: 110,   # 3 месяца
    3: 205,   # 6 месяцев
    4: 285    # 12 месяцев
}

# Список стран
COUNTRIES = {
    'de': '🇩🇪 Германия',
    'ch': '🇨🇭 Швейцария',
    'nl': '🇳🇱 Нидерланды',
    'fi': '🇫🇮 Финляндия'
}

# Инициализация базы данных
def init_db():
    try:
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        
        # Таблица тарифов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                duration INTEGER NOT NULL,
                price REAL NOT NULL,
                description TEXT
            )
        ''')
        
        # Таблица конфигураций
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                country TEXT NOT NULL,
                config TEXT NOT NULL,
                is_used BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (plan_id) REFERENCES plans (id)
            )
        ''')
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT
            )
        ''')
        
        # Проверка и добавление столбца balance
        cursor.execute("PRAGMA table_info(users)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'balance' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0.0")
            logger.info("Добавлен столбец balance в таблицу users")
        
        # Таблица заказов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_id INTEGER,
                config_id INTEGER,
                order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expiry_date TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (plan_id) REFERENCES plans (id),
                FOREIGN KEY (config_id) REFERENCES configs (id)
            )
        ''')
        
        # Таблица платежей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT DEFAULT 'purchase',
                plan_id INTEGER,
                amount REAL,
                invoice_id TEXT UNIQUE,
                cryptobot_invoice_id TEXT,
                crystal_pay_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (plan_id) REFERENCES plans (id)
            )
        ''')
        
        # Проверка и добавление недостающих столбцов в payments (миграции)
        cursor.execute("PRAGMA table_info(payments)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'type' not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN type TEXT DEFAULT 'purchase'")
            logger.info("Добавлен столбец type в таблицу payments")
        if 'cryptobot_invoice_id' not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN cryptobot_invoice_id TEXT")
            logger.info("Добавлен столбец cryptobot_invoice_id в таблицу payments")
        if 'crystal_pay_id' not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN crystal_pay_id TEXT")
            logger.info("Добавлен столбец crystal_pay_id в таблицу payments")
        if 'status' not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'pending'")
            logger.info("Добавлен столбец status в таблицу payments")
        if 'created_at' not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            logger.info("Добавлен столбец created_at в таблицу payments")
        
        # Проверка и добавление столбца country в configs
        cursor.execute("PRAGMA table_info(configs)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'country' not in columns:
            cursor.execute("ALTER TABLE configs ADD COLUMN country TEXT NOT NULL DEFAULT 'de'")
            logger.info("Добавлен столбец country в таблицу configs")
        
        # Добавление тарифов (обновленные цены)
        cursor.execute("DELETE FROM plans")  # Очищаем старые тарифы
        plans = [
            (1, "1 месяц", 1, 1.0, "VPN на 1 месяц"),
            (2, "3 месяца", 3, 2.5, "VPN на 3 месяца"),
            (3, "6 месяцев", 6, 4.0, "VPN на 6 месяцев"),
            (4, "12 месяцев", 12, 5.0, "VPN на 12 месяцев")
        ]
        cursor.executemany("INSERT INTO plans VALUES (?, ?, ?, ?, ?)", plans)
        
        # Таблица промокодов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                max_activations INTEGER,
                used_activations INTEGER DEFAULT 0,
                expires_at TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        # Таблица активаций промокодов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS promo_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (code) REFERENCES promo_codes (code),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована успешно")
    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}")
        exit(1)

# Получение баланса пользователя
def get_balance(user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0.0

# Обновление баланса
def update_balance(user_id, amount):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE users SET balance = balance + ? WHERE user_id = ?
    """, (amount, user_id))
    conn.commit()
    conn.close()

# Конвертация RUB->USDT
def rub_to_usdt(rub_amount):
    try:
        return round(float(rub_amount) / RUB_PER_USDT, 2)
    except Exception:
        return 0.0

# Получение тарифов из БД
def get_plans():
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plans ORDER BY duration")
    plans = cursor.fetchall()
    conn.close()
    return plans

# Получение плана по ID
def get_plan_by_id(plan_id):
    plans = get_plans()
    return next((p for p in plans if p[0] == plan_id), None)

# Получение неиспользованного конфига для тарифа и страны
def get_unused_config(plan_id, country):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, config FROM configs 
        WHERE plan_id = ? AND country = ? AND is_used = FALSE 
        LIMIT 1
    """, (plan_id, country))
    config = cursor.fetchone()
    conn.close()
    return config

# Пометка конфига как использованного
def mark_config_as_used(config_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE configs SET is_used = TRUE WHERE id = ?", (config_id,))
    conn.commit()
    conn.close()

# Сохранение/обновление пользователя
def save_user(user):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, balance)
        VALUES (?, ?, ?, ?, COALESCE((SELECT balance FROM users WHERE user_id = ?), 0.0))
    """, (user.id, user.username, user.first_name, user.last_name, user.id))
    conn.commit()
    conn.close()
    logger.info(f"Пользователь сохранён: user_id={user.id}, username={user.username}")
    
# Создание заказа
def create_order(user_id, plan_id, config_id, duration):
    expiry_date = datetime.now() + timedelta(days=duration * 30)
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO orders (user_id, plan_id, config_id, expiry_date)
        VALUES (?, ?, ?, ?)
    """, (user_id, plan_id, config_id, expiry_date))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

# Получение заказов пользователя
def get_user_orders(user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT o.id, p.name, o.order_date, o.expiry_date, c.config, c.country
        FROM orders o
        JOIN plans p ON o.plan_id = p.id
        LEFT JOIN configs c ON o.config_id = c.id
        WHERE o.user_id = ? AND o.expiry_date > CURRENT_TIMESTAMP
        ORDER BY o.order_date DESC
    """, (user_id,))
    orders = cursor.fetchall()
    conn.close()
    return orders

# Получение статистики конфигураций
def get_configs_stats():
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.name, c.country, COUNT(*) as count
        FROM configs c
        JOIN plans p ON c.plan_id = p.id
        WHERE c.is_used = FALSE
        GROUP BY p.id, c.country
    """)
    stats = cursor.fetchall()
    conn.close()
    return stats

# Создание платежа
def create_payment(user_id, payment_type, plan_id, amount):
    invoice_id = str(uuid.uuid4())
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO payments (user_id, type, plan_id, invoice_id, amount)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, payment_type, plan_id, invoice_id, amount))
    conn.commit()
    conn.close()
    logger.info(f"Создан платёж: user_id={user_id}, type={payment_type}, plan_id={plan_id}, amount={amount}, invoice_id={invoice_id}")
    return invoice_id

# Обновление cryptobot_invoice_id
def update_cryptobot_invoice_id(internal_invoice_id, cb_invoice_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE payments SET cryptobot_invoice_id = ? WHERE invoice_id = ?
    """, (cb_invoice_id, internal_invoice_id))
    conn.commit()
    conn.close()

# Обновление crystal_pay_id в базе данных
def update_crystal_pay_id(internal_invoice_id, crystal_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE payments SET crystal_pay_id = ? WHERE invoice_id = ?
    """, (crystal_id, internal_invoice_id))
    conn.commit()
    conn.close()

# Обновление статуса платежа
def update_payment_status(invoice_id, status):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE payments SET status = ? WHERE invoice_id = ?
    """, (status, invoice_id))
    conn.commit()
    conn.close()

# Получение данных платежа
def get_payment(internal_invoice_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.user_id, p.type, p.plan_id, p.amount, p.invoice_id, p.cryptobot_invoice_id, p.crystal_pay_id, p.status, p.created_at, pl.name as plan_name 
        FROM payments p
        LEFT JOIN plans pl ON p.plan_id = pl.id
        WHERE p.invoice_id = ?
    """, (internal_invoice_id,))
    payment = cursor.fetchone()
    conn.close()
    if payment:
        logger.info(f"Получен платёж: invoice_id={internal_invoice_id}, type={payment[2]}, status={payment[8]}")
    else:
        logger.warning(f"Платёж не найден: invoice_id={internal_invoice_id}")
    return payment

# Создание счета в CryptoBot
def create_cryptobot_invoice(user_id, amount, description, payload):
    data = {
        "amount": str(amount),
        "asset": "USDT",  # Изменено на USDT
        "description": description,
        "payload": payload,
        "paid_btn_name": "viewItem",
        "paid_btn_url": f"https://t.me/{BOT_TOKEN.split(':')[0]}?start=menu"
    }
    
    try:
        logger.info(f"Sending request to CryptoBot API: {data}")
        url = f"{CRYPTO_BOT_API_URL}/createInvoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        response = requests.post(url, headers=headers, data=data, timeout=10)
        logger.info(f"CryptoBot response: {response.status_code} - {response.text}")
        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                return result["result"]
            logger.error(f"CryptoBot API error: {result}")
            return None
        logger.error(f"HTTP error: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"Error creating CryptoBot invoice: {e}")
        return None
        
# alias, чтобы старые вызовы не падали
def create_crypto_invoice(user_id, amount, description, payload=None):
    return create_cryptobot_invoice(user_id, amount, description, payload)

# Создание счета в CrystalPAY
def create_crystal_pay_invoice(user_id, amount, description, callback_url=None):
    """Создание счета в CrystalPAY (минимальный JSON-набор полей)"""
    url = f"{CRYSTAL_PAY_API_URL}/invoice/create/"

    invoice_id = str(uuid.uuid4())

    payload = {
        "auth_login": CRYSTAL_PAY_LOGIN,
        "auth_secret": CRYSTAL_PAY_SECRET,
        "type": "purchase",
        "amount": int(round(float(amount))),
        "extra": invoice_id,
        "lifetime": 300
    }
    # callback_url опционально, если поддерживается вашим тарифом
    if callback_url:
        payload["callback_url"] = callback_url

    try:
        headers = {"Content-Type": "application/json"}
        logger.info("Sending request to CrystalPAY API (json)")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"CrystalPAY response: {response.status_code} - {response.text}")

        if response.status_code == 200:
            result = response.json()
            if not result.get("error"):
                return {
                    "invoice_id": invoice_id,
                    "crystal_id": result.get("id"),
                    "url": result.get("url")
                }
            else:
                logger.error(f"CrystalPAY API error body: {result}")
                return {"error": True, "errors": result.get("errors", [])}
        else:
            logger.error(f"HTTP error: {response.status_code} - {response.text}")
            return {"error": True, "errors": [f"HTTP {response.status_code}"]}
    except Exception as e:
        logger.error(f"Error creating CrystalPAY invoice: {e}")
        return {"error": True, "errors": [str(e)]}

def create_crystal_pay_invoice_rub(user_id, rub_amount, description, internal_invoice_id, callback_url=None):
    """Создание счёта в CrystalPAY в RUB (type=purchase, amount в RUB)."""
    url = f"{CRYSTAL_PAY_API_URL}/invoice/create/"

    payload = {
        "auth_login": CRYSTAL_PAY_LOGIN,
        "auth_secret": CRYSTAL_PAY_SECRET,
        "type": "purchase",
        "amount": int(rub_amount),  # в рублях
        "extra": internal_invoice_id,
        "lifetime": 300
    }
    if callback_url:
        payload["callback_url"] = callback_url

    try:
        headers = {"Content-Type": "application/json"}
        logger.info("Sending request to CrystalPAY API (json, RUB)")
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"CrystalPAY RUB response: {response.status_code} - {response.text}")
        if response.status_code == 200:
            result = response.json()
            if not result.get("error"):
                return {
                    "crystal_id": result.get("id"),
                    "url": result.get("url")
                }
            else:
                logger.error(f"CrystalPAY RUB API error body: {result}")
                return {"error": True, "errors": result.get("errors", [])}
        else:
            logger.error(f"HTTP error (RUB): {response.status_code} - {response.text}")
            return {"error": True, "errors": [f"HTTP {response.status_code}"]}
    except Exception as e:
        logger.error(f"Error creating CrystalPAY RUB invoice: {e}")
        return {"error": True, "errors": [str(e)]}

def check_crystal_pay_payment(crystal_id):
    """Проверка статуса платежа в CrystalPAY (используем JSON)."""
    url = f"{CRYSTAL_PAY_API_URL}/invoice/info/"

    data = {
        "auth_login": CRYSTAL_PAY_LOGIN,
        "auth_secret": CRYSTAL_PAY_SECRET,
        "id": crystal_id
    }

    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, headers=headers, json=data, timeout=10)
        logger.info(f"CrystalPAY check response: {response.status_code} - {response.text}")
        if response.status_code == 200:
            result = response.json()
            if not result.get("error"):
                return result.get("state", "notpayed")
            else:
                logger.error(f"CrystalPAY check error body: {result}")
                return "error"
        else:
            logger.error(f"HTTP error checking CrystalPAY payment: {response.status_code} - {response.text}")
            return "error"
    except Exception as e:
        logger.error(f"Error checking CrystalPAY payment: {e}")
        return "error"

# Главное меню с красивыми кнопками
def get_main_menu(is_admin=False):
    keyboard = [
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🛍️ Купить VPN", callback_data="plans")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    if is_admin:
        keyboard.append([InlineKeyboardButton("⚙️ Админ", callback_data="admin")])
    # Не логируем контент кнопок с эмодзи, чтобы избежать UnicodeEncodeError на некоторых консолях
    logger.info(f"Формирование главного меню для is_admin={is_admin}")
    return InlineKeyboardMarkup(keyboard)
    
# Кнопки профиль
def get_profile_menu():
    keyboard = [
        [InlineKeyboardButton("💰 Пополнить баланс", callback_data="topup")],
        [InlineKeyboardButton("🎁 Активировать промокод", callback_data="promo")],
        [InlineKeyboardButton("🧾 Мои VPN", callback_data="orders")],
        [InlineKeyboardButton("📊 История платежей", callback_data="payment_history")],
        [InlineKeyboardButton("🔙 Главное меню", callback_data="menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Кнопки стран
def get_countries_keyboard(back_callback):
    keyboard = []
    for code, name in COUNTRIES.items():
        keyboard.append([InlineKeyboardButton(name, callback_data=f"country_{code}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(keyboard)

# Админ панель
def get_admin_panel():
    keyboard = [
        [InlineKeyboardButton("📤 Загрузить конфиги", callback_data="admin_upload")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔍 Конфиги", callback_data="admin_configs")],
        [InlineKeyboardButton("🎁 Промокоды", callback_data="admin_promos")],
        [InlineKeyboardButton("💸 Выдать баланс", callback_data="admin_grant_balance")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("💰 Платежи", callback_data="admin_payments")],
        [InlineKeyboardButton("🔙 Выход", callback_data="menu")]
    ]
    logger.info(f"Формирование админ-панели: {keyboard}")
    return InlineKeyboardMarkup(keyboard)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        save_user(user)
        
        # Проверяем подписку на канал (кроме админа)
        if user.id != ADMIN_ID:
            is_subscribed = await check_channel_subscription(context.bot, user.id)
            if not is_subscribed:
                text, keyboard = get_subscription_required_menu()
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
                context.user_data['state'] = 'subscription_required'
                return
        
        welcome_text = (
            "🌟 *Добро пожаловать в EcliptVPN!*\n\n"
            "🔐 Безопасный VPN с выбором стран.\n"
            "💳 Пополните баланс от 1$ и покупайте тарифы.\n\n"
            "Выберите действие:"
        )
        
        await update.message.reply_text(welcome_text, reply_markup=get_main_menu(user.id == ADMIN_ID), parse_mode=ParseMode.MARKDOWN)
        context.user_data['state'] = 'menu'
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("Произошла ошибка. Пожалуйста, попробуйте позже.")

# Обработка callback'ов
def escape_markdown(text):
    """Экранирование специальных символов для Markdown."""
    if text is None:
        return ""
    escape_chars = r'_[]*()~`>#-+={}|.!='
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text

async def check_channel_subscription(bot, user_id):
    """Проверяет подписку пользователя на канал"""
    try:
        # Получаем информацию о статусе подписки
        chat_member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Ошибка проверки подписки для пользователя {user_id}: {e}")
        return False

def get_subscription_required_menu():
    """Создает меню с требованием подписки на канал"""
    text = """🔒 *Доступ ограничен*

Для использования бота необходимо подписаться на наш канал:

📢 [EcliptVPN](https://t.me/EcliptVPN)

После подписки нажмите кнопку "✅ Проверить подписку" для продолжения.

📋 [Политика конфиденциальности](https://teletype.in/@ecliptvpn/Zw_fLfMQHWb)
📋 [Условия использования](https://teletype.in/@ecliptvpn/Zw_fLfMQHWb)"""

    keyboard = [
        [InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/EcliptVPN")],
        [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription")]
    ]
    
    return text, InlineKeyboardMarkup(keyboard)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    logger.info(f"Callback data: {data}, user_id: {user_id}, state: {context.user_data.get('state')}")
    
    # Проверяем подписку для всех действий (кроме админа и самой проверки подписки)
    if user_id != ADMIN_ID and data != "check_subscription":
        is_subscribed = await check_channel_subscription(context.bot, user_id)
        if not is_subscribed:
            text, keyboard = get_subscription_required_menu()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'subscription_required'
            return
    
    # Обработка проверки подписки
    if data == "check_subscription":
        if user_id == ADMIN_ID:
            # Админ всегда имеет доступ
            welcome_text = (
                "🌟 *Добро пожаловать в EcliptVPN!*\n\n"
                "🔐 Безопасный VPN с выбором стран.\n"
                "💳 Пополните баланс от 1$ и покупайте тарифы.\n\n"
                "Выберите действие:"
            )
            await query.edit_message_text(welcome_text, reply_markup=get_main_menu(True), parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'menu'
        else:
            is_subscribed = await check_channel_subscription(context.bot, user_id)
            if is_subscribed:
                welcome_text = (
                    "✅ *Подписка подтверждена!*\n\n"
                    "🌟 *Добро пожаловать в EcliptVPN!*\n\n"
                    "🔐 Безопасный VPN с выбором стран.\n"
                    "💳 Пополните баланс от 1$ и покупайте тарифы.\n\n"
                    "Выберите действие:"
                )
                await query.edit_message_text(welcome_text, reply_markup=get_main_menu(False), parse_mode=ParseMode.MARKDOWN)
                context.user_data['state'] = 'menu'
            else:
                text, keyboard = get_subscription_required_menu()
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
        return
    
    if data == "menu":
        context.user_data['state'] = 'menu'
        menu_text = "🌟 *Главное меню*"
        reply_markup = get_main_menu(user_id == ADMIN_ID)
        try:
            await query.edit_message_text(menu_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Ошибка при возврате в меню для user_id {user_id}: {e}")
            await query.message.reply_text(menu_text.replace("*", ""), reply_markup=reply_markup)
            await context.bot.send_message(ADMIN_ID, f"⚠️ Ошибка возврата в меню для user_id {user_id}: {e}")
        return
    
    if data == "admin":
        if user_id != ADMIN_ID:
            logger.info(f"Попытка доступа к админ-панели от user_id {user_id} (не админ)")
            await query.edit_message_text("❌ Нет доступа.")
            return
        admin_text = "🔧 *Админ панель*"
        reply_markup = get_admin_panel()
        try:
            await query.edit_message_text(admin_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Ошибка при открытии админ-панели для user_id {user_id}: {e}")
            await query.message.reply_text(admin_text.replace("*", ""), reply_markup=reply_markup)
            await context.bot.send_message(ADMIN_ID, f"⚠️ Ошибка открытия админ-панели для user_id {user_id}: {e}")
        context.user_data['state'] = 'admin_menu'
        return
    
    if data == "profile":
        balance = get_balance(user_id)
        username = escape_markdown(query.from_user.username or 'Не указан')
        first_name = escape_markdown(query.from_user.first_name)
        balance_str = escape_markdown(f"{balance:.2f}")
        profile_text = (
            f"👤 *Ваш профиль*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"👻 Имя: {first_name}\n"
            f"📛 Username: @{username}\n"
            f"💰 Баланс: *{balance_str} USDT*\n\n"
            f"Выберите действие:"
        )
        reply_markup = get_profile_menu()
        try:
            await query.edit_message_text(profile_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Ошибка при открытии профиля для user_id {user_id}: {e}")
            # Попробуем отправить новое сообщение без Markdown
            profile_text_safe = (
                f"👤 Ваш профиль\n\n"
                f"🆔 ID: {user_id}\n"
                f"👻 Имя: {query.from_user.first_name}\n"
                f"📛 Username: @{query.from_user.username or 'Не указан'}\n"
                f"💰 Баланс: {balance:.2f} USDT\n\n"
                f"Выберите действие:"
            )
            await query.message.reply_text(profile_text_safe, reply_markup=reply_markup)
        return
    
    if data == "plans":
        await show_plans(update, context)
        return
    
    if data == "orders":
        orders = get_user_orders(user_id)
        if not orders:
            orders_text = "📋 *У вас нет активных VPN-подписок.*"
        else:
            orders_text = "📋 *Ваши активные VPN:*\n\n"
            for order in orders:
                country_emoji = COUNTRIES.get(order[5], '🌍')
                config_escaped = escape_markdown(order[4])
                orders_text += (
                    f"🆔 Заказ #{order[0]}\n"
                    f"📦 {order[1]} | {country_emoji}\n"
                    f"📅 С: {order[2][:10]}\n"
                    f"⏰ До: {order[3][:10]}\n"
                    f"🔑 Конфиг: `{config_escaped}`\n\n"
                )
        keyboard = [[InlineKeyboardButton("🔙 Профиль", callback_data="profile")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(orders_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Ошибка при открытии заказов для user_id {user_id}: {e}")
            await query.message.reply_text(orders_text.replace("*", ""), reply_markup=reply_markup)
        return

    if data == "topup":
        keyboard = [
            [InlineKeyboardButton("₽ 50", callback_data="topup_rub_amount_50")],
            [InlineKeyboardButton("₽ 100", callback_data="topup_rub_amount_100")],
            [InlineKeyboardButton("₽ 250", callback_data="topup_rub_amount_250")],
            [InlineKeyboardButton("₽ 400", callback_data="topup_rub_amount_400")],
            [InlineKeyboardButton("₽ 500", callback_data="topup_rub_amount_500")],
            [InlineKeyboardButton("✍️ Другая сумма (₽)", callback_data="topup_rub_custom")],
            [InlineKeyboardButton("🔙 Профиль", callback_data="profile")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("🇷🇺 Выберите сумму для пополнения (RUB):", reply_markup=reply_markup)
        return

    if data.startswith("topup_crypto_"):
        parts = data.split("_")
        try:
            amount = float(parts[-1])
        except Exception:
            await query.edit_message_text("❌ Неверная сумма для CryptoBot.")
            return
        # создаём внутренний платёж
        internal_invoice_id = create_payment(user_id, 'topup', None, amount)
        description = f"Пополнение баланса на {amount} USDT"
        payload = json.dumps({"invoice_id": internal_invoice_id, "type": "topup"})
        invoice = create_crypto_invoice(user_id, amount, description, payload)
        if not invoice:
            await query.edit_message_text("❌ Ошибка создания счёта CryptoBot.")
            return
        # сохраняем внешний id в БД
        update_cryptobot_invoice_id(internal_invoice_id, invoice.get("invoice_id"))
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=invoice.get('pay_url'))],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_payment_{internal_invoice_id}")],
            [InlineKeyboardButton("🔙 Профиль", callback_data="profile")]
        ]
        await query.edit_message_text("⏳ Ожидание оплаты. Нажмите для оплаты:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data['state'] = 'waiting_payment'
        return

    elif data.startswith("check_invoice_"):
        invoice_id = int(callback_data.split("_")[2])
        status = await check_cryptobot_invoice(invoice_id)
        if status == "paid":
            await context.bot.send_message(chat_id=user_id, text="✅ Оплата прошла успешно!")
            await deliver_config(query, context, plan_id, plan_name, user_id, country)
        else:
            await context.bot.send_message(chat_id=user_id, text="❌ Оплата пока не получена.")


    # Обработка RUB CrystalPay ДОЛЖНА быть раньше общего обработчика topup_crystal_
    if data.startswith("topup_crystal_rub_"):
        parts = data.split('_')
        try:
            rub_amount = int(parts[3])
            internal_invoice_id = parts[4]
        except Exception:
            await query.edit_message_text("❌ Неверная сумма для CrystalPay (RUB).")
            return
        crystal = create_crystal_pay_invoice_rub(user_id, rub_amount, f"Пополнение на {rub_amount} RUB", internal_invoice_id)
        if not crystal or crystal.get("error"):
            await query.edit_message_text("❌ Ошибка создания счёта CrystalPay (RUB).")
            return
        if crystal.get("crystal_id"):
            update_crystal_pay_id(internal_invoice_id, crystal["crystal_id"])
        keyboard = [
            [InlineKeyboardButton("💎 Оплатить", url=crystal.get('url', ''))],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_crystal_topup_{internal_invoice_id}")],
            [InlineKeyboardButton("🔙 Профиль", callback_data="profile")]
        ]
        await query.edit_message_text(
            f"Ссылка для оплаты (RUB) через CrystalPay:\n{crystal.get('url')}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['state'] = 'waiting_payment'
        return

    if data.startswith("topup_crystal_"):
        parts = data.split("_")
        # ожидаем формат topup_crystal_{usdt_amount}
        try:
            amount = float(parts[-1])
        except Exception:
            await query.edit_message_text("❌ Неверная сумма для CrystalPay.")
            return
        internal_invoice_id = create_payment(user_id, 'topup', None, amount)
        description = f"Пополнение баланса на {amount} USDT"
        # важное: CrystalPay работает в RUB. Создаём счёт в RUB по курсу
        rub_amount = int(round(amount * RUB_PER_USDT))
        crystal_invoice = create_crystal_pay_invoice_rub(user_id, rub_amount, description, internal_invoice_id)
        if not crystal_invoice or crystal_invoice.get("error"):
            await query.edit_message_text("❌ Ошибка при создании счёта CrystalPay (RUB).")
            return
        # сохраняем crystal id в БД
        if crystal_invoice.get("crystal_id"):
            update_crystal_pay_id(internal_invoice_id, crystal_invoice.get("crystal_id"))
        keyboard = [
            [InlineKeyboardButton("💎 Оплатить", url=crystal_invoice.get('url', ''))],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_crystal_topup_{internal_invoice_id}")],
            [InlineKeyboardButton("🔙 Профиль", callback_data="profile")]
        ]
        await query.edit_message_text(
            f"Ссылка для оплаты через CrystalPay (RUB):\n{crystal_invoice.get('url')}\n\nК зачислению: ~{amount:.2f} USDT",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data['state'] = 'waiting_payment'
        return

    # выбор USD больше не используется

    if data.startswith("topup_rub_amount_"):
        rub_amount = int(data.split('_')[3])
        usdt_amount = rub_to_usdt(rub_amount)
        internal_invoice_id = create_payment(user_id, 'topup', None, usdt_amount)
        description = f"Пополнение баланса на {rub_amount} RUB (~{usdt_amount} USDT)"
        # Для CryptoBot платёж всё равно будет в USDT, предлагаем оба способа
        keyboard = [
            [InlineKeyboardButton("🤖 CryptoBot (USDT)", callback_data=f"topup_crypto_{usdt_amount}")],
            [InlineKeyboardButton("💎 CrystalPAY (RUB)", callback_data=f"topup_crystal_rub_{rub_amount}_{internal_invoice_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="topup_rub")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"Сумма: {rub_amount} RUB (зачислим ~{usdt_amount} USDT).\nВыберите способ:",
            reply_markup=reply_markup
        )
        return

    if data.startswith("topup_crystal_rub_"):
        parts = data.split('_')
        rub_amount = int(parts[3])
        internal_invoice_id = parts[4]
        # создадим отдельный счёт в CrystalPay в рублях: используем amount как целое RUB
        crystal = create_crystal_pay_invoice_rub(user_id, rub_amount, f"Пополнение на {rub_amount} RUB", internal_invoice_id)
        if not crystal or crystal.get("error"):
            await query.edit_message_text("❌ Ошибка создания счёта CrystalPay (RUB).")
            return
        if crystal.get("crystal_id"):
            update_crystal_pay_id(internal_invoice_id, crystal["crystal_id"])
        await query.edit_message_text(f"Ссылка для оплаты (RUB) через CrystalPay:\n{crystal.get('url')}")
        context.user_data['state'] = 'waiting_payment'
        return

    if data.startswith("topup_amount_"):
        amount = int(data.split('_')[2])
        keyboard = [
            [InlineKeyboardButton("💰 CryptoBot", callback_data=f"topup_crypto_{amount}")],
            [InlineKeyboardButton("💎 CrystalPAY", callback_data=f"topup_crystal_{amount}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="topup")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"💰 Пополнение на {amount} USDT\n\nВыберите способ оплаты:", reply_markup=reply_markup)
        return
    
    if data == "topup_rub_custom":
        await query.edit_message_text("✍️ Введите сумму пополнения в рублях (от 50 до 100000):")
        context.user_data['state'] = 'waiting_topup_rub_amount'
        return
    
    if data == "help":
        help_text = (
            "❓ <b>Помощь</b>\n\n"
            "🔐 <b>Как купить VPN:</b>\n"
            "1) Пополните баланс от 1 USDT (через CryptoBot или CrystalPay).\n"
            "2) Выберите тариф и страну.\n"
            "3) Подтвердите покупку — сумма спишется с баланса.\n\n"
            "🌍 <b>Подключение к VPN</b>\n\n"
            "— <b>Android / iOS</b> через V2RayTun\n"
            "  1) Установите приложение V2RayTun из официального магазина.\n"
            "  2) Получите конфиг в разделе Мои VPN после покупки.\n"
            "  3) Скопируйте строку конфига целиком.\n"
            "  4) В V2RayTun нажмите «Добавить профиль» и выберите «Импорт из буфера обмена».\n"
            "  5) Сохраните профиль и нажмите «Подключить».\n\n"
            "— <b>Windows / macOS / Linux</b> через Hiddify\n"
            "  1) Скачайте Hiddify Client с официального сайта (hiddify.com).\n"
            "  2) Откройте клиент и выберите «Импорт из буфера обмена».\n"
            "  3) Вставьте строку конфига и сохраните профиль.\n"
            "  4) Нажмите «Подключить».\n\n"
            "💡 <b>Примечания</b>\n"
            "— Если у вас несколько профилей, отключайте один перед включением другого.\n"
            "— При проблемах с подключением попробуйте сменить страну.\n"
            "— Убедитесь, что другие VPN или прокси отключены.\n\n"
            "📋 <b><a href='https://teletype.in/@ecliptvpn/Zw_fLfMQHWb'>Политика конфиденциальности</a></b>\n"
            "📋 <b><a href='https://teletype.in/@ecliptvpn/Zw_fLfMQHWb'>Условия использования</a></b>\n\n"
            "📞 <b>Поддержка:</b> @xacan_1337\n"
        )
        keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return
    
    # Админ кнопки
    if data == "admin_upload":
        if user_id != ADMIN_ID:
            return
        upload_text = "📤 *Загрузка конфигов*\n\nВыберите страну:"
        await query.edit_message_text(upload_text, reply_markup=get_countries_keyboard("admin"), parse_mode=ParseMode.MARKDOWN)
        context.user_data['state'] = 'admin_select_country_upload'
        return
    
    if data.startswith("country_") and context.user_data.get('state') == 'admin_select_country_upload':
        country = data.split('_')[1]
        plan_text = "📤 Выберите тариф:"
        plans = get_plans()
        keyboard = []
        for plan in plans:
            keyboard.append([InlineKeyboardButton(f"{plan[1]} ({plan[3]} USDT)", callback_data=f"admin_upload_plan_{plan[0]}_{country}")])
        keyboard.append([InlineKeyboardButton("🔙 Админ", callback_data="admin")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(plan_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    
    if data.startswith("admin_upload_plan_"):
        parts = data.split('_')
        plan_id = int(parts[3])
        country = parts[4]
        context.user_data['uploading_plan'] = plan_id
        context.user_data['uploading_country'] = country
        upload_text = (
            f"📤 Загрузка для {COUNTRIES[country]} | {get_plan_by_id(plan_id)[1]}\n\n"
            "📁 Отправьте JSON-файл с конфигами (строка или массив строк)."
        )
        await query.edit_message_text(upload_text, parse_mode=ParseMode.MARKDOWN)
        return
    
    if data == "admin_stats":
        if user_id != ADMIN_ID:
            return
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM orders WHERE expiry_date > CURRENT_TIMESTAMP")
        active_orders = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(amount) FROM payments WHERE status = 'paid'")
        total_revenue = cursor.fetchone()[0] or 0
        # Количество использованных промокодов
        cursor.execute("SELECT COUNT(*) FROM promo_activations")
        promo_used = cursor.fetchone()[0]
        # Сумма выданных бонусов через промокоды
        cursor.execute("SELECT SUM(p.amount) FROM promo_activations a JOIN promo_codes p ON a.code = p.code")
        promo_bonus = cursor.fetchone()[0] or 0
        # Сумма вручную выданных бонусов (через admin_grant_balance)
        # (нет отдельной таблицы, считаем по payments с type='grant', если реализовано, иначе пропустить)
        # stats_text
        conn.close()
        stats_text = (
            f"📊 *Статистика*\n\n"
            f"👥 Пользователей: *{users_count}*\n"
            f"📦 Активных VPN: *{active_orders}*\n"
            f"💰 Доход: *{total_revenue:.2f} USDT*\n"
            f"🎁 Использовано промокодов: *{promo_used}*\n"
            f"💸 Выдано бонусов (промокоды): *{promo_bonus:.2f} USDT*"
        )
        keyboard = [[InlineKeyboardButton("🔙 Админ", callback_data="admin")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(stats_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    
    if data == "admin_configs":
        if user_id != ADMIN_ID:
            return
        stats = get_configs_stats()
        if not stats:
            configs_text = "🔍 *Конфигурации*\n\nНет доступных конфигов."
        else:
            configs_text = "🔍 *Доступные конфиги*\n\n"
            for stat in stats:
                plan_name, country_code, count = stat
                country_name = COUNTRIES.get(country_code, '🌍 Неизвестно')
                configs_text += f"📦 {plan_name} | {country_name}: *{count}*\n"
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Админ", callback_data="admin")]])
        await query.edit_message_text(configs_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    
    if data in ["admin_users", "admin_payments"]:
        await query.edit_message_text("🔧 Функция в разработке.", reply_markup=[[InlineKeyboardButton("🔙 Админ", callback_data="admin")]])
        return
    
    if data.startswith("country_") and 'selected_plan' in context.user_data:
        await country_selected(update, context)
        return
    
    if data.startswith("buy_balance_"):
        await buy_with_balance(update, context)
        return
    
    if data.startswith("check_payment_"):
        await check_payment(update, context)
        return

    if data.startswith("check_crystal_topup_"):
        try:
            internal_invoice_id = data.replace("check_crystal_topup_", "", 1)
        except Exception:
            await query.edit_message_text("❌ Платёж не найден.")
            return
        await check_crystal_topup_status(update, context, internal_invoice_id)
        return

    if data.startswith("plan_"):
        await plan_selected(update, context)
        return

    if data == "promo":
        promo_text = (
            "🎁 *Активация промокода*\n\n"
            "Введите промокод одним сообщением.\n\n"
            "Промокод можно использовать только один раз."
        )
        keyboard = [[InlineKeyboardButton("🔙 Профиль", callback_data="profile")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(promo_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        context.user_data['state'] = 'waiting_promo'
        return

    if data == "admin_promos":
        if user_id != ADMIN_ID:
            return
        promo_menu = (
            "🎁 *Промокоды*\n\n"
            "Выберите действие:"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton("📋 Список промокодов", callback_data="admin_list_promos")],
            [InlineKeyboardButton("🔙 Админ", callback_data="admin")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(promo_menu, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    if data == "admin_create_promo":
        if user_id != ADMIN_ID:
            return
        text = (
            "➕ *Создание промокода*\n\n"
            "Введите через пробел: КОД СУММА МАКС\\_АКТИВАЦИЙ\(или 0\) ДНЕЙ\(или 0, если без срока\)\n"
            "Пример: `SUMMER2025 5 10 30`"
        )
        keyboard = [[InlineKeyboardButton("🔙 Промокоды", callback_data="admin_promos")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        context.user_data['state'] = 'waiting_create_promo'
        return
    if data == "admin_grant_balance":
        if user_id != ADMIN_ID:
            return
        grant_text = (
            "💸 *Выдать баланс*\n\n"
            "Введите ID пользователя и сумму через пробел (например: 123456789 10):"
        )
        keyboard = [[InlineKeyboardButton("🔙 Админ", callback_data="admin")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(grant_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        context.user_data['state'] = 'waiting_grant_id'
        return

    if data == "admin_list_promos":
        if user_id != ADMIN_ID:
            return
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT code, amount, max_activations, used_activations, expires_at, is_active FROM promo_codes ORDER BY code")
        promos = cursor.fetchall()
        conn.close()
        if not promos:
            text = "Нет промокодов."
            keyboard = [[InlineKeyboardButton("🔙 Промокоды", callback_data="admin_promos")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            return
        text = "📋 *Список промокодов*\n\nВыберите промокод для управления:" 
        keyboard = []
        for p in promos:
            code, amount, max_a, used_a, expires, active = p
            max_a = max_a if max_a is not None else '∞'
            expires = expires[:10] if expires else '∞'
            status = '✅' if active else '❌'
            text += f"\n{status} `{code}` | {amount} USDT | {used_a}/{max_a} | до {expires}"
            row = [
                InlineKeyboardButton("❌ Деактивировать" if active else "✅ Активировать", callback_data=f"admin_deactivate_promo_{code}"),
                InlineKeyboardButton("🗑️ Удалить", callback_data=f"admin_delete_promo_{code}")
            ]
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("🔙 Промокоды", callback_data="admin_promos")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("admin_deactivate_promo_"):
        if user_id != ADMIN_ID:
            return
        code = data.replace("admin_deactivate_promo_", "")
        promo = get_promo_code(code)
        if not promo:
            await query.edit_message_text("Промокод не найден.")
            return
        if promo[5]:
            deactivate_promo_code(code)
            await query.edit_message_text(f"❌ Промокод `{code}` деактивирован.", parse_mode=ParseMode.MARKDOWN)
        else:
            # Активировать обратно
            conn = sqlite3.connect('vpn_bot.db')
            cursor = conn.cursor()
            cursor.execute("UPDATE promo_codes SET is_active = 1 WHERE code = ?", (code,))
            conn.commit()
            conn.close()
            await query.edit_message_text(f"✅ Промокод `{code}` активирован.", parse_mode=ParseMode.MARKDOWN)
        # Вернуться к списку
        await button_callback(update, context)
        return
    if data.startswith("admin_delete_promo_"):
        if user_id != ADMIN_ID:
            return
        code = data.replace("admin_delete_promo_", "")
        # Подтверждение удаления
        text = f"Вы уверены, что хотите удалить промокод `{code}`? Это действие необратимо."
        keyboard = [
            [InlineKeyboardButton("🗑️ Подтвердить удаление", callback_data=f"admin_confirm_delete_promo_{code}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_list_promos")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("admin_confirm_delete_promo_"):
        if user_id != ADMIN_ID:
            return
        code = data.replace("admin_confirm_delete_promo_", "")
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        cursor.execute("DELETE FROM promo_activations WHERE code = ?", (code,))
        cursor.execute("DELETE FROM promo_codes WHERE code = ?", (code,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"🗑️ Промокод `{code}` удалён.", parse_mode=ParseMode.MARKDOWN)
        # Вернуться к списку
        await button_callback(update, context)
        return

    if data.startswith("pay_crystal_"):
        parts = data.split('_')
        plan_id = int(parts[2])
        country = parts[3]
        await process_crystal_pay_payment(update, context, plan_id, country)
        return

    if data.startswith("check_crystal_"):
        internal_invoice_id = data.split('_')[2]
        await check_crystal_pay_payment_status(update, context, internal_invoice_id)
        return

    if data.startswith("pay_stars_"):
        # Покупка тарифа через Stars
        parts = data.split('_')
        plan_id = int(parts[2])
        country = parts[3]
        plan = get_plan_by_id(plan_id)
        if not plan:
            await query.edit_message_text("❌ Тариф не найден.")
            return
        # Используем фиксированную цену в звёздах, если есть, иначе рассчитываем по курсу
        stars_amount = STARS_PRICE_BY_PLAN.get(plan_id)
        if stars_amount is None:
            amount_usdt = plan[3]
            stars_amount = max(1, int(round(amount_usdt * STARS_PER_USDT)))
        title = "Оплата VPN звёздами"
        description = f"{plan[1]} | {COUNTRIES.get(country, country)} — {stars_amount}⭐"
        payload = json.dumps({"type": "stars_purchase", "plan_id": plan_id, "country": country})
        try:
            await send_stars_invoice(context, query.message.chat_id, title, description, payload, stars_amount)
            await query.edit_message_text("⏳ Счёт на оплату звёздами отправлен в чат.")
        except Exception as e:
            logger.error(f"Stars invoice error for user {user_id}, plan {plan_id}: {e}")
            await query.message.reply_text("❌ Не удалось создать счёт в Stars. Убедитесь, что у вас доступен Telegram Stars и попробуйте ещё раз.")
        return

# Обработка текстовых сообщений (для ввода суммы пополнения)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'state' not in context.user_data:
        return
    
    user_id = update.effective_user.id
    
    # Проверяем подписку для всех текстовых сообщений (кроме админа)
    if user_id != ADMIN_ID:
        is_subscribed = await check_channel_subscription(context.bot, user_id)
        if not is_subscribed:
            text, keyboard = get_subscription_required_menu()
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'subscription_required'
        return
    
    state = context.user_data['state']
    if state == 'waiting_topup_rub_amount':
        try:
            rub_amount = int(float(update.message.text))
            if rub_amount < 50 or rub_amount > 100000:
                await update.message.reply_text("Сумма должна быть от 50 до 100000 RUB. Попробуйте снова.")
                return

            usdt_amount = rub_to_usdt(rub_amount)
            internal_invoice_id = create_payment(user_id, 'topup', None, usdt_amount)

            keyboard = [
                [InlineKeyboardButton("🤖 CryptoBot (USDT)", callback_data=f"topup_crypto_{usdt_amount}")],
                [InlineKeyboardButton("💎 CrystalPAY (RUB)", callback_data=f"topup_crystal_rub_{rub_amount}_{internal_invoice_id}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="topup")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"Сумма: {rub_amount} RUB (зачислим ~{usdt_amount} USDT).\nВыберите способ:",
                reply_markup=reply_markup
            )
        except ValueError:
            await update.message.reply_text("Введите корректное число.")

    if state == 'waiting_stars_amount':
        try:
            stars_amount = int(update.message.text.strip())
            if stars_amount < 1:
                await update.message.reply_text("Минимум 1 ⭐. Попробуйте снова.")
                return
            title = "Пополнение баланса звёздами"
            description = f"Курс: 1 USDT = {STARS_PER_USDT:.2f} ⭐"
            payload = json.dumps({"type": "stars_topup"})
            await send_stars_invoice(context, update.message.chat_id, title, description, payload, stars_amount)
            context.user_data['state'] = 'waiting_payment_stars'
        except ValueError:
            await update.message.reply_text("Введите целое число — количество ⭐.")
        return

    if state == 'waiting_promo':
        code = update.message.text.strip()
        user_id = update.effective_user.id
        promo = get_promo_code(code)
        if not promo:
            await update.message.reply_text("❌ Промокод не найден.")
            return
        if not promo[5]:  # is_active
            await update.message.reply_text("❌ Промокод не активен.")
            return
        if promo[4]:  # expires_at
            try:
                from datetime import datetime
                expires = datetime.fromisoformat(promo[4])
                if expires < datetime.now():
                    await update.message.reply_text("❌ Срок действия промокода истёк.")
                    return
            except Exception:
                pass
        if promo[2] is not None and promo[3] >= promo[2]:  # used_activations >= max_activations
            await update.message.reply_text("❌ Промокод уже использован максимальное число раз.")
            return
        if is_promo_activated_by_user(code, user_id):
            await update.message.reply_text("❌ Вы уже использовали этот промокод.")
            return
        # Всё ок, активируем
        activate_promo_code(code, user_id)
        update_balance(user_id, promo[1])
        credited_str = escape_markdown(f"{promo[1]:.2f}")
        await update.message.reply_text(f"🎉 Промокод активирован! На ваш баланс зачислено {credited_str} USDT.")
        context.user_data['state'] = 'menu'
        # Показываем профиль
        balance = get_balance(user_id)
        username = escape_markdown(update.effective_user.username or 'Не указан')
        first_name = escape_markdown(update.effective_user.first_name)
        balance_str = escape_markdown(f"{balance:.2f}")
        profile_text = (
            f"👤 *Ваш профиль*\n\n"
            f"🆔 ID: `{user_id}`\n"
            f"👻 Имя: {first_name}\n"
            f"📛 Username: @{username}\n"
            f"💰 Баланс: *{balance_str} USDT*\n\n"
            f"Выберите действие:"
        )
        reply_markup = get_profile_menu()
        await update.message.reply_text(profile_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if state == 'waiting_grant_id':
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Нет доступа.")
            return
        try:
            parts = update.message.text.strip().split()
            if len(parts) != 2:
                await update.message.reply_text("Введите ID и сумму через пробел (например: 123456789 10)")
                return
            target_id = int(parts[0])
            amount = float(parts[1])
            update_balance(target_id, amount)
            await update.message.reply_text(f"✅ Пользователю {target_id} начислено {amount:.2f} USDT.")
            # Возврат в админ-панель
            admin_text = "🔧 *Админ панель*"
            reply_markup = get_admin_panel()
            await update.message.reply_text(admin_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'admin_menu'
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}\nВведите ID и сумму через пробел.")
        return

    if state == 'waiting_create_promo':
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Нет доступа.")
            return
        try:
            parts = update.message.text.strip().split()
            if len(parts) < 2 or len(parts) > 4:
                await update.message.reply_text("Введите КОД СУММА МАКС_АКТИВАЦИЙ(или 0) ДНЕЙ(или 0, если без срока). Пример: SUMMER2025 5 10 30")
                return
            code = parts[0]
            amount = float(parts[1])
            max_activations = int(parts[2]) if int(parts[2]) > 0 else None
            days = int(parts[3]) if len(parts) > 3 else 0
            from datetime import datetime, timedelta
            expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
            create_promo_code(code, amount, max_activations, expires_at)
            await update.message.reply_text(f"✅ Промокод {code} создан! Сумма: {amount} USDT, Макс: {max_activations or '∞'}, Срок: {days if days > 0 else '∞'} дней.")
            # Возврат в меню промокодов
            promo_menu = ("🎁 *Промокоды*\n\nВыберите действие:")
            keyboard = [
                [InlineKeyboardButton("➕ Создать промокод", callback_data="admin_create_promo")],
                [InlineKeyboardButton("📋 Список промокодов", callback_data="admin_list_promos")],
                [InlineKeyboardButton("🔙 Админ", callback_data="admin")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(promo_menu, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'admin_menu'
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}\nВведите КОД СУММА МАКС_АКТИВАЦИЙ(или 0) ДНЕЙ(или 0, если без срока). Пример: SUMMER2025 5 10 30")
        return

# Показ тарифов с красивыми кнопками
async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        plans = get_plans()
        user_id = update.callback_query.from_user.id if update.callback_query else update.effective_user.id
        balance = get_balance(user_id)
        text = (
            f"🛍️ *Выберите тариф*\n\n"
            f"💰 Ваш баланс: *{balance:.2f} USDT*\n\n"
        )
        keyboard = []
        
        for plan in plans:
            can_afford = balance >= plan[3]
            status_emoji = "✅" if can_afford else "💳"
            button_text = f"{status_emoji} {plan[1]} | {plan[3]} USDT"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"plan_{plan[0]}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Меню", callback_data="menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        query = update.callback_query
        if query:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in show_plans: {e}")
        if update.callback_query:
            await update.callback_query.message.reply_text("Произошла ошибка.")
        else:
            await update.message.reply_text("Произошла ошибка.")

# Обработка выбора тарифа
async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        plan_id = int(query.data.split('_')[1])
        plan = get_plan_by_id(plan_id)
        
        if not plan:
            await query.edit_message_text("❌ Тариф не найден.")
            return
        
        user_id = query.from_user.id
        balance = get_balance(user_id)
        can_afford = balance >= plan[3]
        
        confirmation_text = (
            f"📦 *{plan[1]}*\n\n"
            f"💰 Цена: *{plan[3]} USDT*\n"
            f"💳 Баланс: *{balance:.2f} USDT*\n\n"
            f"🌍 Выберите страну:"
        )
        
        keyboard = get_countries_keyboard("plans")
        await query.edit_message_text(confirmation_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
        context.user_data['selected_plan'] = plan_id
        context.user_data['can_afford'] = can_afford
    except Exception as e:
        logger.error(f"Error in plan_selected: {e}")
        await query.edit_message_text("Произошла ошибка.")

# Обработка выбора страны
async def country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    country_code = query.data.split('_')[1]
    plan_id = context.user_data.get('selected_plan')
    plan = get_plan_by_id(plan_id)
    can_afford = context.user_data.get('can_afford', False)
    
    confirmation_text = (
        f"🌍 *{COUNTRIES[country_code]}*\n"
        f"📦 *{plan[1]}*\n"
        f"💰 *{plan[3]} USDT*\n\n"
        f"✅ Подтвердить покупку?"
    )
    
    keyboard = []
    if can_afford:
        keyboard.append([InlineKeyboardButton("💳 С баланса", callback_data=f"buy_balance_{plan_id}_{country_code}")])
    keyboard.append([InlineKeyboardButton("🔙 Тарифы", callback_data="plans")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(confirmation_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# Покупка с баланса
async def buy_with_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        parts = query.data.split('_')
        plan_id = int(parts[2])
        country = parts[3]
        plan = get_plan_by_id(plan_id)
        
        user_id = query.from_user.id
        balance = get_balance(user_id)
        if balance < plan[3]:
            await query.edit_message_text("❌ Недостаточно средств.")
            return
        
        # Вычесть с баланса
        update_balance(user_id, -plan[3])
        
        # Выдать конфиг
        config_data = get_unused_config(plan_id, country)
        if not config_data:
            await query.edit_message_text("❌ Конфиги закончились. Свяжитесь с поддержкой.")
            await context.bot.send_message(ADMIN_ID, f"⚠️ Закончились конфиги: {plan[1]} | {COUNTRIES[country]}")
            return
        
        config_id, config = config_data
        mark_config_as_used(config_id)
        create_order(user_id, plan_id, config_id, plan[2])
        
        config_escaped = escape_markdown(config)
        success_text = (
            f"🎉 *Покупка успешна!*\n\n"
            f"💰 Новый баланс: *{get_balance(user_id):.2f} USDT*\n\n"
            f"🌍 {COUNTRIES[country]}\n"
            f"📦 {plan[1]}\n\n"
            f"🔑 *Конфиг:*\n"
            f"```{config_escaped}```"
        )
        
        await query.edit_message_text(success_text, parse_mode=ParseMode.MARKDOWN_V2)
        
        # Уведомить админа
        username = query.from_user.username or query.from_user.first_name
        await context.bot.send_message(
            ADMIN_ID,
            f"🆕 Покупка с баланса!\n👤 {username} (ID: {user_id})\n📦 {plan[1]}\n🌍 {COUNTRIES[country]}\n💰 {plan[3]} USDT"
        )
    except Exception as e:
        logger.error(f"Error in buy_with_balance: {e}")
        await query.edit_message_text("Произошла ошибка.")

# Обработка оплаты через CryptoBot
async def process_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        parts = query.data.split('_')
        plan_id = int(parts[1])
        country = parts[2]
        plan = get_plan_by_id(plan_id)
        user_id = query.from_user.id
        amount = plan[3]
        
        invoice_id = create_payment(user_id, 'purchase', plan_id, amount)
        description = f"VPN {plan[1]} | {COUNTRIES[country]}"
        payload = json.dumps({"invoice_id": invoice_id, "type": "purchase", "country": country})
        
        invoice = create_cryptobot_invoice(user_id, amount, description, payload)
        if not invoice:
            await query.edit_message_text("❌ Ошибка создания счёта.")
            return
        
        update_cryptobot_invoice_id(invoice_id, invoice["invoice_id"])
        
        pay_url = invoice["pay_url"]
        payment_text = (
            f"💳 *Оплата VPN*\n\n"
            f"📦 {plan[1]}\n"
            f"🌍 {COUNTRIES[country]}\n"
            f"💰 *{amount} USDT*\n\n"
            f"⏳ Статус: Ожидание"
        )
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=pay_url)],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_payment_{invoice_id}")],
            [InlineKeyboardButton("🔙 Меню", callback_data="menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(payment_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in process_payment: {e}")
        await query.edit_message_text("Произошла ошибка.")

# Проверка статуса оплаты
async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        data = query.data
        prefix = "check_payment_"
        internal_invoice_id = data[len(prefix):] if data.startswith(prefix) else data
        logger.info(f"check_payment: raw_data={data}, parsed_internal_invoice_id={internal_invoice_id}")
        payment = get_payment(internal_invoice_id)
        if not payment:
            await query.edit_message_text("❌ Платёж не найден.")
            return

        cb_invoice_id = payment[6]  # cryptobot_invoice_id
        payment_type = payment[2]
        user_id = payment[1]
        amount = payment[4]

        logger.info(f"Проверка платежа: invoice_id={internal_invoice_id}, type={payment_type}, status={payment[7]}, user_id={user_id}")

        if not cb_invoice_id:
            await query.edit_message_text("❌ Счёт не найден.")
            return

        # Проверка в CryptoBot
        url = f"{CRYPTO_BOT_API_URL}/getInvoices"
        headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
        params = {"invoice_ids": cb_invoice_id}

        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code != 200:
            await query.edit_message_text("❌ Ошибка подключения.")
            return

        result = response.json()
        if not result.get("ok") or not result["result"].get("items"):
            await query.edit_message_text("❌ Ошибка проверки.")
            return

        cb_invoice = result["result"]["items"][0]
        status = cb_invoice["status"]
        payload_data = json.loads(cb_invoice.get("payload", "{}"))
        if payload_data.get("invoice_id") != internal_invoice_id:
            await query.edit_message_text("❌ Несоответствие платежа.")
            return

        update_payment_status(internal_invoice_id, status)

        if status == "paid":
            if payment_type == "topup":
                update_balance(user_id, amount)
                await query.edit_message_text(
                    f"🎉 Баланс пополнен!\n💰 +{amount} USDT\n💳 Новый баланс: *{get_balance(user_id):.2f} USDT*",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif payment_type == "purchase":
                country = payload_data.get("country", "de")
                plan_id = payment[3]
                plan_name = payment[10]
                await deliver_config(query, context, plan_id, plan_name, user_id, country)
            else:
                await query.edit_message_text("✅ Оплата получена.")
            return
        elif status == "expired":
            await query.edit_message_text("⏰ Счёт истёк. Создайте новый.")
            return
        else:
            await query.edit_message_text("⏳ Ожидание оплаты. Нажмите 'Проверить' позже.")
            return
    except Exception as e:
        logger.error(f"Error in check_payment: {e}")
        await query.edit_message_text("Произошла ошибка.")
        
# Выдача конфига после оплаты покупки
async def deliver_config(query, context, plan_id, plan_name, user_id, country):
    try:
        config_data = get_unused_config(plan_id, country)
        if not config_data:
            if hasattr(query, 'message'):
                await query.message.reply_text("❌ Конфиги закончились.")
            else:
                await query.reply_text("❌ Конфиги закончились.")
            await context.bot.send_message(ADMIN_ID, f"⚠️ Закончились конфиги: {plan_name} | {COUNTRIES[country]}")
            return
        
        config_id, config = config_data
        mark_config_as_used(config_id)
        create_order(user_id, plan_id, config_id, get_plan_by_id(plan_id)[2])
        
        # Экранируем конфиг для Markdown
        config_escaped = escape_markdown(config)
        success_text = (
            f"🎉 *Оплата подтверждена!*\n\n"
            f"🌍 {COUNTRIES[country]}\n"
            f"📦 {plan_name}\n\n"
            f"🔑 *Конфиг:*\n"
            f"\`\`\`{config_escaped}\`\`\`"
        )
        keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data="menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if hasattr(query, 'edit_message_text'):
            try:
                await query.edit_message_text(success_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.error(f"Ошибка отправки конфига для user_id {user_id}: {e}")
                success_text_safe = (
                    f"🎉 Оплата подтверждена!\n\n"
                    f"🌍 {COUNTRIES[country]}\n"
                    f"📦 {plan_name}\n\n"
                    f"🔑 Конфиг:\n"
                    f"{config}"
                )
                await query.message.reply_text(success_text_safe, reply_markup=reply_markup)
        else:
            try:
                await query.reply_text(success_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except Exception as e:
                logger.error(f"Ошибка отправки конфига для user_id {user_id}: {e}")
                success_text_safe = (
                    f"🎉 Оплата подтверждена!\n\n"
                    f"🌍 {COUNTRIES[country]}\n"
                    f"📦 {plan_name}\n\n"
                    f"🔑 Конфиг:\n"
                    f"{config}"
                )
                await query.reply_text(success_text_safe, reply_markup=reply_markup)
        
        # Уведомить админа
        username = query.from_user.username or query.from_user.first_name if hasattr(query, 'from_user') else context.chat_data.get('username', 'user')
        await context.bot.send_message(
            ADMIN_ID,
            f"🆕 Новый заказ!\n👤 {username} (ID: {user_id})\n📦 {plan_name}\n🌍 {COUNTRIES[country]}"
        )
    except Exception as e:
        logger.error(f"Error in deliver_config: {e}")
        if hasattr(query, 'message'):
            await query.message.reply_text("Ошибка выдачи конфига.")
        else:
            await query.reply_text("Ошибка выдачи конфига.")

# Команда /admin
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return
    await update.message.reply_text("🔧 *Админ панель*", reply_markup=get_admin_panel(), parse_mode=ParseMode.MARKDOWN)

# Обработка загруженного файла (для админа)
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or 'uploading_plan' not in context.user_data:
        return
    
    plan_id = context.user_data['uploading_plan']
    country = context.user_data.get('uploading_country', 'de')
    document = update.message.document
    
    if document.mime_type != 'application/json':
        await update.message.reply_text("❌ Отправьте JSON-файл.")
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        configs = json.loads(file_content.decode('utf-8'))
        
        if isinstance(configs, str):
            configs = [configs]
        elif not isinstance(configs, list):
            await update.message.reply_text("❌ Неверный формат JSON: ожидается строка или массив строк.")
            return
        
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        inserted = 0
        for config in configs:
            if isinstance(config, str) and config.startswith('vless://'):
                # Сохраняем полную строку конфига, включая часть после #
                cursor.execute("INSERT INTO configs (plan_id, country, config) VALUES (?, ?, ?)", (plan_id, country, config))
                inserted += 1
            else:
                logger.warning(f"Пропущен некорректный конфиг: {config[:50]}...")
        
        conn.commit()
        conn.close()
        
        if inserted == 0:
            await update.message.reply_text("❌ Не удалось загрузить конфиги: проверьте формат (должно начинаться с vless://).")
        else:
            await update.message.reply_text(
                f"✅ Загружено *{inserted}* конфигов для {COUNTRIES[country]} | {get_plan_by_id(plan_id)[1]}.",
                parse_mode=ParseMode.MARKDOWN
            )
        del context.user_data['uploading_plan']
        del context.user_data['uploading_country']
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка JSON: {e}")
        await update.message.reply_text("❌ Ошибка JSON: проверьте содержимое файла.")
    except Exception as e:
        logger.error(f"Ошибка в handle_document: {e}")
        await update.message.reply_text("❌ Ошибка загрузки.")

# Получить промокод по коду
def get_promo_code(code):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT code, amount, max_activations, used_activations, expires_at, is_active FROM promo_codes WHERE code = ?", (code,))
    promo = cursor.fetchone()
    conn.close()
    return promo

# Проверить, активировал ли пользователь промокод
def is_promo_activated_by_user(code, user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM promo_activations WHERE code = ? AND user_id = ?", (code, user_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

# Активировать промокод для пользователя
def activate_promo_code(code, user_id):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO promo_activations (code, user_id) VALUES (?, ?)", (code, user_id))
    cursor.execute("UPDATE promo_codes SET used_activations = used_activations + 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()

# Создать промокод
def create_promo_code(code, amount, max_activations=None, expires_at=None):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO promo_codes (code, amount, max_activations, expires_at, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (code, amount, max_activations, expires_at))
    conn.commit()
    conn.close()

# Деактивировать промокод
def deactivate_promo_code(code):
    conn = sqlite3.connect('vpn_bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE promo_codes SET is_active = 0 WHERE code = ?", (code,))
    conn.commit()
    conn.close()

async def send_stars_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str, description: str, payload: str, stars_amount: int):
    prices = [LabeledPrice(label="XTR", amount=stars_amount)]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except Exception as e:
        logger.error(f"PreCheckout error: {e}")
        await query.answer(ok=False, error_message="Платёж отклонён. Попробуйте позже.")

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    if not sp:
        return
    currency = sp.currency
    total_amount = sp.total_amount  # для Stars — это количество звёзд
    payload = sp.invoice_payload
    try:
        data = json.loads(payload)
    except Exception:
        data = {"type": "unknown"}
    user_id = update.effective_user.id
    if currency == "XTR":
        if data.get("type") == "stars_topup":
            credited_usdt = total_amount / STARS_PER_USDT
            update_balance(user_id, credited_usdt)
            await update.message.reply_text(
                f"🎉 Баланс пополнен на {credited_usdt:.2f} USDT за {total_amount}⭐"
            )
            # показать профиль
            balance = get_balance(user_id)
            balance_str = escape_markdown(f"{balance:.2f}")
            profile_text = (
                f"👤 *Ваш профиль*\n\n"
                f"🆔 ID: `{user_id}`\n"
                f"💰 Баланс: *{balance_str} USDT*\n\n"
                f"Выберите действие:"
            )
            await update.message.reply_text(profile_text, reply_markup=get_profile_menu(), parse_mode=ParseMode.MARKDOWN_V2)
        elif data.get("type") == "stars_purchase":
            plan_id = int(data.get("plan_id"))
            country = data.get("country", "de")
            plan = get_plan_by_id(plan_id)
            if not plan:
                await update.message.reply_text("❌ Тариф не найден.")
                return
            await deliver_config(update.message, context, plan_id, plan[1], user_id, country)
        else:
            await update.message.reply_text("Платёж получен.")
    else:
        await update.message.reply_text("Платёж получен.")
        
from telegram.ext import MessageHandler, filters

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    payment = update.message.successful_payment
    logger.info(f"Пользователь {user_id} оплатил через Stars: {payment.total_amount} {payment.currency}")
    await update.message.reply_text("✅ Оплата прошла успешно!")
    await deliver_config(query, context, plan_id, plan_name, user_id, country)
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))

# Обработка оплаты через CrystalPAY
async def process_crystal_pay_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int, country: str):
    try:
        query = update.callback_query
        plan = get_plan_by_id(plan_id)
        user_id = query.from_user.id
        amount = int(round(float(plan[3])))
        
        # Создаем запись о платеже в базе данных
        invoice_id = create_payment(user_id, 'purchase', plan_id, amount)
        description = ""
        
        # Создаем счет в CrystalPAY
        crystal_invoice = create_crystal_pay_invoice(user_id, amount, description)
        if not crystal_invoice or crystal_invoice.get("error"):
            await query.edit_message_text("❌ Ошибка создания счёта в CrystalPAY.")
            return
        
        # Обновляем запись с ID от CrystalPAY
        if crystal_invoice.get("crystal_id"):
            update_crystal_pay_id(invoice_id, crystal_invoice["crystal_id"])
        
        payment_text = (
            f"💎 *Оплата через CrystalPAY*\n\n"
            f"📦 {plan[1]}\n"
            f"🌍 {COUNTRIES[country]}\n"
            f"💰 *{amount} USDT*\n\n"
            f"⏳ Статус: Ожидание оплаты"
        )
        
        keyboard = [
            [InlineKeyboardButton("💎 Оплатить", url=crystal_invoice.get("url", ""))],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_crystal_{invoice_id}")],
            [InlineKeyboardButton("🔙 Меню", callback_data="menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(payment_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in process_crystal_pay_payment: {e}")
        await query.edit_message_text("❌ Произошла ошибка при создании платежа.")

# Проверка статуса платежа CrystalPAY
async def check_crystal_pay_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE, internal_invoice_id: str):
    try:
        query = update.callback_query
        payment = get_payment(internal_invoice_id)
        
        if not payment:
            await query.edit_message_text("❌ Платёж не найден.")
            return
        
        if payment[8] == "paid":  # status
            await query.edit_message_text("✅ Платёж уже обработан!")
            return
        
        # Получаем crystal_pay_id из базы данных
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT crystal_pay_id FROM payments WHERE invoice_id = ?", (internal_invoice_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result or not result[0]:
            await query.edit_message_text("❌ ID платежа CrystalPAY не найден.")
            return
        
        crystal_id = result[0]
        status = check_crystal_pay_payment(crystal_id)
        
        if status == "payed":
            # Платеж успешен
            update_payment_status(internal_invoice_id, "paid")
            plan_id = payment[3]
            country = 'de'
            plan = get_plan_by_id(plan_id)
            
            await deliver_config(query.message, context, plan_id, plan[1], payment[1], country)
            await query.edit_message_text("✅ Платёж успешно обработан! Конфигурация отправлена.")
            
        elif status == "notpayed":
            await query.edit_message_text("⏳ Платёж ещё не поступил. Попробуйте позже.")
            
        elif status == "overpayed":
            # Переплата - всё равно засчитываем
            update_payment_status(internal_invoice_id, "paid")
            plan_id = payment[3]
            country = 'de'
            plan = get_plan_by_id(plan_id)
            
            await deliver_config(query.message, context, plan_id, plan[1], payment[1], country)
            await query.edit_message_text("✅ Платёж обработан (переплата)! Конфигурация отправлена.")
            
        else:
            await query.edit_message_text("❌ Ошибка проверки платежа. Попробуйте позже.")
            
    except Exception as e:
        logger.error(f"Error checking CrystalPAY payment: {e}")
        await query.edit_message_text("❌ Произошла ошибка при проверке платежа.")

# Обработка пополнения баланса через CrystalPAY
async def process_crystal_topup(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        
        # Создаем запись о пополнении в базе данных
        amount_int = int(round(float(amount)))
        invoice_id = create_payment(user_id, 'topup', None, amount_int)
        description = ""
        
        # Создаем счет в CrystalPAY
        crystal_invoice = create_crystal_pay_invoice(user_id, amount_int, description)
        if not crystal_invoice or crystal_invoice.get("error"):
            await query.edit_message_text("❌ Ошибка создания счёта в CrystalPAY.")
            return
        
        # Обновляем запись с ID от CrystalPAY
        if crystal_invoice.get("crystal_id"):
            update_crystal_pay_id(invoice_id, crystal_invoice["crystal_id"])
        
        payment_text = (
            f"💎 *Пополнение через CrystalPAY*\n\n"
            f"💰 Сумма: *{amount_int} USDT*\n\n"
            f"⏳ Статус: Ожидание оплаты"
        )
        
        keyboard = [
            [InlineKeyboardButton("💎 Оплатить", url=crystal_invoice.get("url", ""))],
            [InlineKeyboardButton("✅ Проверить", callback_data=f"check_crystal_topup_{invoice_id}")],
            [InlineKeyboardButton("🔙 Профиль", callback_data="profile")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(payment_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in process_crystal_topup: {e}")
        await query.edit_message_text("❌ Произошла ошибка при создании платежа.")

# Проверка статуса пополнения CrystalPAY
async def check_crystal_topup_status(update: Update, context: ContextTypes.DEFAULT_TYPE, internal_invoice_id: str):
    try:
        query = update.callback_query
        payment = get_payment(internal_invoice_id)
        
        if not payment:
            await query.edit_message_text("❌ Платёж не найден.")
            return
        
        if payment[8] == "paid":  # status
            await query.edit_message_text("✅ Пополнение уже обработано!")
            return
        
        # Получаем crystal_pay_id из базы данных
        conn = sqlite3.connect('vpn_bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT crystal_pay_id FROM payments WHERE invoice_id = ?", (internal_invoice_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result or not result[0]:
            await query.edit_message_text("❌ ID платежа CrystalPAY не найден.")
            return
        
        crystal_id = result[0]
        status = check_crystal_pay_payment(crystal_id)
        
        if status in ["payed", "overpayed"]:
            # Пополнение успешно
            update_payment_status(internal_invoice_id, "paid")
            amount = payment[4]  # amount
            user_id = payment[1]  # user_id
            
            # Пополняем баланс
            update_balance(user_id, amount)
            
            await query.edit_message_text(f"✅ Баланс успешно пополнен на {amount} USDT!")
            
            # Показываем обновленный профиль
            balance = get_balance(user_id)
            balance_str = escape_markdown(f"{balance:.2f}")
            profile_text = (
                f"👤 *Ваш профиль*\n\n"
                f"🆔 ID: `{user_id}`\n"
                f"💰 Баланс: *{balance_str} USDT*\n\n"
                f"Выберите действие:"
            )
            await query.message.reply_text(profile_text, reply_markup=get_profile_menu(), parse_mode=ParseMode.MARKDOWN_V2)
            
        elif status == "notpayed":
            await query.edit_message_text("⏳ Платёж ещё не поступил. Попробуйте позже.")
        else:
            await query.edit_message_text("❌ Ошибка проверки платежа. Попробуйте позже.")
            
    except Exception as e:
        logger.error(f"Error checking CrystalPAY topup: {e}")

        await query.edit_message_text("❌ Произошла ошибка при проверке пополнения.")

if __name__ == "__main__":
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    
    logger.info("Бот запущен")
    application.run_polling()
