import os
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
import asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗМІННІ ОТОЧЕННЯ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # ID VIP-групи або каналу
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))  # ID адміна

# 🔗 Посилання на загальну групу спілкування
PUBLIC_CHAT_LINK = os.getenv("PUBLIC_CHAT_LINK", "https://t.me/kerdos_group")

# =======================================================
# БАЗА ДАНИХ: PostgreSQL (Render) через asyncpg
# =======================================================
# Render автоматично створює змінну DATABASE_URL, коли ви додаєте
# PostgreSQL-базу до сервісу. Якщо бот і база знаходяться в одному
# регіоні Render, використовуйте "Internal Database URL" — тоді
# DB_SSL можна лишити вимкненим (за замовчуванням). Якщо підключаєтесь
# ззовні (External Database URL) або з іншого хостингу, встановіть
# змінну середовища DB_SSL=true.
DATABASE_URL = os.getenv("DATABASE_URL")
DB_SSL = os.getenv("DB_SSL", "false").lower() == "true"

# Пул з'єднань asyncpg. Ініціалізується у lifespan() при старті додатку.
db_pool: asyncpg.Pool | None = None

# ⬇️ РЕКВІЗИТИ КРИПТОГАМАНЦІВ BINANCE ⬇️
WALLET_USDT_TRC20 = "THeVYP6zqgJ3jKMhNAuBxqGk47iFno6pKL"
WALLET_USDT_BEP20 = "0x97eb6c4c2fe24798ccf24ed5d52cb228f32f5f5f"
WALLET_USDT_SOLANA = "5Pcc4WUfA1qBas6P42WDYRre8ugAenNe5UsN6c2DyUox"

# 🪙 Список монет, доступних для підключення до Signal Bot
AVAILABLE_COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "LINKUSDT",
    "ONDOUSDT", "JTOUSDT", "LTCUSDT", "APTUSDT", "DOTUSDT",
    "AVAXUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT", "AAVEUSDT",
    "XMRUSDT", "ETCUSDT", "VETUSDT", "GRTUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "THETAUSDT", "DASHUSDT",
]

bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

# Кешований username бота. Заповнюється один раз у lifespan (див. нижче),
# щоб НЕ робити зайвий виклик bot.get_me() на кожен клік по кнопці "Реферальна програма".
BOT_USERNAME = None


# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ЕКРАНУВАННЯ MARKDOWN
# =======================================================
def escape_md(text) -> str:
    """
    Екранує спецсимволи застарілого Telegram Markdown (V1): _ * ` [
    Без цього, якщо юзернейм, повідомлення в підтримку, текст квитанції або
    Signal Token містить один з цих символів (напр. @my_name), Telegram
    поверне помилку "can't parse entities" і повідомлення не надійде.
    Застосовується до БУДЬ-ЯКОГО динамічного/введеного користувачем тексту,
    який підставляється у повідомлення з parse_mode="Markdown".
    """
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text
# =======================================================


# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: LIFESPAN ЗАМІСТЬ on_event
# =======================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_USERNAME, db_pool

    if not DATABASE_URL:
        logger.error(
            "DATABASE_URL не встановлено! Додайте PostgreSQL-базу на Render "
            "і переконайтеся, що змінна середовища DATABASE_URL передана сервісу."
        )
    else:
        db_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            ssl="require" if DB_SSL else None,
            min_size=1,
            max_size=10,
        )
        await init_db()

    if bot:
        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username
        except Exception as e:
            logger.error(f"Не вдалося отримати username бота при старті: {e}")

    bg_task = asyncio.create_task(check_expired_trials())
    try:
        yield
    finally:
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass
        if db_pool:
            await db_pool.close()


app = FastAPI(lifespan=lifespan)
# =======================================================

# --- БАЗА ДАНИХ (PostgreSQL / asyncpg) ---

async def init_db():
    async with db_pool.acquire() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT,
                action TEXT,
                price DOUBLE PRECISION,
                roi DOUBLE PRECISION,
                timestamp TIMESTAMPTZ
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                trial_used INTEGER DEFAULT 0,
                trial_start TIMESTAMPTZ,
                trial_end TIMESTAMPTZ,
                sub_end TIMESTAMPTZ,
                bot_sub_end TIMESTAMPTZ,
                signal_token TEXT,
                status TEXT DEFAULT 'free',
                lang TEXT DEFAULT 'ua',
                referrer_id BIGINT DEFAULT NULL,
                awaiting_support INTEGER DEFAULT 0,
                selected_coin TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coin_roi (
                ticker TEXT PRIMARY KEY,
                roi DOUBLE PRECISION,
                updated_at TIMESTAMPTZ
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_trades (
                symbol TEXT PRIMARY KEY,
                entry_price DOUBLE PRECISION,
                direction TEXT,
                time TEXT
            )
        """)
        # Міграція: додаємо selected_coin, якщо таблиця users була створена
        # до появи цієї колонки. IF NOT EXISTS робить це безпечним при кожному старті.
        await db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS selected_coin TEXT")


async def get_coin_roi(ticker: str):
    async with db_pool.acquire() as db:
        row = await db.fetchrow("SELECT roi, updated_at FROM coin_roi WHERE ticker = $1", ticker)
        if row and row["roi"] is not None:
            return row["roi"]
    return None

async def save_active_trade(symbol: str, entry_price: float, direction: str, time_str: str):
    """Зберігає або оновлює інформацію про відкриту позицію монети."""
    async with db_pool.acquire() as db:
        await db.execute("""
            INSERT INTO active_trades (symbol, entry_price, direction, time)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (symbol) DO UPDATE SET
                entry_price = excluded.entry_price,
                direction = excluded.direction,
                time = excluded.time
        """, symbol, entry_price, direction, time_str)

async def get_active_trade(symbol: str):
    """Отримує відкриту позицію для конкретної монети."""
    async with db_pool.acquire() as db:
        row = await db.fetchrow('SELECT entry_price, direction, time FROM active_trades WHERE symbol = $1', symbol)
        return tuple(row) if row else None

async def delete_active_trade(symbol: str):
    """Видаляє позицію з активних після її закриття."""
    async with db_pool.acquire() as db:
        await db.execute('DELETE FROM active_trades WHERE symbol = $1', symbol)

async def get_all_coin_roi() -> dict:
    result = {}
    async with db_pool.acquire() as db:
        rows = await db.fetch("SELECT ticker, roi FROM coin_roi")
        for row in rows:
            result[row["ticker"]] = row["roi"]
    return result

async def set_coin_roi(ticker: str, roi: float):
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as db:
        await db.execute("""
            INSERT INTO coin_roi (ticker, roi, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (ticker) DO UPDATE SET roi = excluded.roi, updated_at = excluded.updated_at
        """, ticker, roi, now)

async def set_user_selected_coin(user_id: int, ticker: str):
    async with db_pool.acquire() as db:
        await db.execute("""
            INSERT INTO users (user_id, selected_coin)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET selected_coin = excluded.selected_coin
        """, user_id, ticker)

async def get_user_lang(user_id: int) -> str:
    async with db_pool.acquire() as db:
        row = await db.fetchrow("SELECT lang FROM users WHERE user_id = $1", user_id)
        if row and row["lang"]:
            return row["lang"]
    return "ua"

async def set_user_lang(user_id: int, lang: str):
    async with db_pool.acquire() as db:
        await db.execute("""
            INSERT INTO users (user_id, lang)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET lang = excluded.lang
        """, user_id, lang)

async def set_awaiting_support(user_id: int, state: int):
    async with db_pool.acquire() as db:
        await db.execute("""
            INSERT INTO users (user_id, awaiting_support)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET awaiting_support = excluded.awaiting_support
        """, user_id, state)

async def get_awaiting_support(user_id: int) -> int:
    async with db_pool.acquire() as db:
        row = await db.fetchrow("SELECT awaiting_support FROM users WHERE user_id = $1", user_id)
        if row and row["awaiting_support"] is not None:
            return row["awaiting_support"]
    return 0

# --- ФОНОВИЙ ТАЙМЕР ЗВІЛЬНЕННЯ ТРИАЛУ ТА ПІДПИСКИ ---

async def check_expired_trials():
    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(timezone.utc)

            async with db_pool.acquire() as db:
                # 1. Завершення триалу (14 днів)
                expired_trials = await db.fetch(
                    "SELECT user_id, username, lang FROM users WHERE status = 'trial' AND trial_end <= $1",
                    now
                )

                for user_id, username, lang in expired_trials:
                    try:
                        if TELEGRAM_CHANNEL_ID:
                            await bot.ban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
                            await bot.unban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)

                        await db.execute("UPDATE users SET status = 'expired' WHERE user_id = $1", user_id)

                        user_lang = lang or "ua"
                        text = (
                            "⏳ **Ваш 14-денний тестовий період завершився!**\n\n"
                            "Сподіваємося, ви оцінили точність та якість сигналів **Kerdos**! 🚀\n\n"
                            "Щоб продовжити отримувати сигнали в реальному часі, оберіть варіант підписки нижче:"
                            if user_lang == "ua" else
                            "⏳ **Your 14-day free trial has expired!**\n\n"
                            "We hope you enjoyed the signal quality of **Kerdos**! 🚀\n\n"
                            "To keep receiving real-time signals, please select a subscription option below:"
                        )

                        await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            reply_markup=get_main_keyboard(user_lang),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to remove expired user {user_id}: {e}")

                # 2. Завершення платній підписки на VIP-групу
                expired_subs = await db.fetch(
                    "SELECT user_id, username, lang FROM users WHERE status = 'active' AND sub_end <= $1",
                    now
                )

                for user_id, username, lang in expired_subs:
                    try:
                        if TELEGRAM_CHANNEL_ID:
                            await bot.ban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
                            await bot.unban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)

                        await db.execute("UPDATE users SET status = 'expired' WHERE user_id = $1", user_id)

                        user_lang = lang or "ua"
                        text = (
                            "⏳ **Термін вашої підписки на VIP-групу Kerdos закінчився.**\n\nДля продовження підписки скористайтеся меню бота."
                            if user_lang == "ua" else
                            "⏳ **Your Kerdos VIP group subscription has expired.**\n\nPlease use the menu to renew your access."
                        )

                        await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            reply_markup=get_main_keyboard(user_lang),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to remove expired sub user {user_id}: {e}")

                # =======================================================
                # БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ЗАВЕРШЕННЯ ДОСТУПУ ДО SIGNAL BOT
                # =======================================================
                # 3. Завершення платної підписки на Signal Bot ($100/30 днів).
                # OKX-токен більше не пересилається автоматично — тому це
                # сповіщення також є для адміна нагадуванням прибрати токен
                # користувача зі сповіщень (Alert Message) TradingView.
                expired_bots = await db.fetch(
                    "SELECT user_id, username, lang, selected_coin, signal_token FROM users WHERE bot_sub_end IS NOT NULL AND bot_sub_end <= $1",
                    now
                )

                for user_id, username, lang, selected_coin, signal_token in expired_bots:
                    try:
                        await db.execute("UPDATE users SET bot_sub_end = NULL WHERE user_id = $1", user_id)

                        user_lang = lang or "ua"
                        user_text = (
                            "⏳ **Термін дії вашого Kerdos Signal Bot закінчився.**\n\n"
                            "Автоматичні сигнали для вашого акаунту OKX більше не надсилаються. "
                            "Щоб продовжити, оформіть підписку знову в меню бота."
                            if user_lang == "ua" else
                            "⏳ **Your Kerdos Signal Bot subscription has expired.**\n\n"
                            "Automated signals to your OKX account have stopped. "
                            "To continue, renew your subscription from the bot menu."
                        )
                        await bot.send_message(
                            chat_id=user_id,
                            text=user_text,
                            reply_markup=get_main_keyboard(user_lang),
                            parse_mode="Markdown"
                        )

                        if ADMIN_TELEGRAM_ID:
                            user_disp = f"@{escape_md(username)}" if username and username != "no_username" else f"ID: {user_id}"
                            coin_disp = selected_coin or "не обрано"
                            token_disp = f"`{escape_md(signal_token[:6])}...{escape_md(signal_token[-4:])}`" if signal_token else "немає"
                            admin_text = (
                                "⏰ **ДОСТУП ДО SIGNAL BOT ЗАВЕРШИВСЯ**\n\n"
                                f"👤 **Користувач:** {user_disp}\n"
                                f"🆔 **ID:** `{user_id}`\n"
                                f"🪙 **Монета:** `{coin_disp}`\n"
                                f"🔑 **Token:** {token_disp}\n\n"
                                "➡️ Не забудьте видалити токен цього користувача зі сповіщення "
                                "(Alert Message) у TradingView, якщо він не продовжить підписку."
                            )
                            await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=admin_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Failed to process expired bot access for user {user_id}: {e}")
                # =======================================================

        except Exception as e:
            logger.error(f"Error in check_expired_trials loop: {e}")

# --- КНОПКИ ТА МЕНЮ ---

def get_main_keyboard(lang="ua"):
    if lang == "ua":
        keyboard = [
            [InlineKeyboardButton("⏳ Моя підписка", callback_data="btn_my_sub")],
            [InlineKeyboardButton("🎁 Отримати 14 днів FREE", callback_data="btn_free_trial")],
            [InlineKeyboardButton("👥 Реферальна програма", callback_data="btn_referral")],
            [InlineKeyboardButton("📊 Доступ до VIP-групи ($20 / 30 днів)", callback_data="btn_buy_group")],
            [InlineKeyboardButton("🤖 Підключити Signal Bot ($100 / 30 днів)", callback_data="btn_connect_bot")],
            [InlineKeyboardButton("💎 Послуги та ціни", callback_data="btn_services")],
            [InlineKeyboardButton("📜 Правила спільноти", callback_data="btn_rules")],
            [InlineKeyboardButton("🛟 Підтримка / Допомога", callback_data="btn_support")],
            [InlineKeyboardButton("💬 Чат спільноти", url=PUBLIC_CHAT_LINK)],
            [InlineKeyboardButton("🇬🇧 Switch to English", callback_data="lang_en")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("⏳ My Subscription", callback_data="btn_my_sub")],
            [InlineKeyboardButton("🎁 Get 14-Day Free Trial", callback_data="btn_free_trial")],
            [InlineKeyboardButton("👥 Referral Program", callback_data="btn_referral")],
            [InlineKeyboardButton("📊 VIP Signals Group Access ($20 / 30 days)", callback_data="btn_buy_group")],
            [InlineKeyboardButton("🤖 Connect Signal Bot ($100 / 30 days)", callback_data="btn_connect_bot")],
            [InlineKeyboardButton("💎 Services & Pricing", callback_data="btn_services")],
            [InlineKeyboardButton("📜 Community Rules", callback_data="btn_rules")],
            [InlineKeyboardButton("🛟 Support / Help", callback_data="btn_support")],
            [InlineKeyboardButton("💬 Community Chat", url=PUBLIC_CHAT_LINK)],
            [InlineKeyboardButton("🇺🇦 Переключити на Українську", callback_data="lang_ua")]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard(lang="ua"):
    back_text = "🔙 Повернутися в меню" if lang == "ua" else "🔙 Back to Menu"
    return InlineKeyboardMarkup([[InlineKeyboardButton(back_text, callback_data="btn_back_main")]])

def get_cancel_support_keyboard(lang="ua"):
    cancel_text = "❌ Скасувати звернення" if lang == "ua" else "❌ Cancel Support Request"
    return InlineKeyboardMarkup([[InlineKeyboardButton(cancel_text, callback_data="btn_cancel_support")]])

async def get_coin_selection_keyboard(lang="ua"):
    """Клавіатура вибору монети для Signal Bot, з ROI за минулий місяць біля кожної монети."""
    roi_map = await get_all_coin_roi()
    rows = []
    row = []
    for i, ticker in enumerate(AVAILABLE_COINS):
        roi = roi_map.get(ticker)
        if roi is None:
            roi_label = "н/д" if lang == "ua" else "N/A"
        else:
            sign = "+" if roi >= 0 else ""
            roi_label = f"{sign}{roi:.1f}%"
        display = ticker.replace("USDT", "")
        button_text = f"{display} ({roi_label})"
        row.append(InlineKeyboardButton(button_text, callback_data=f"coin_{ticker}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔙 Назад" if lang == "ua" else "🔙 Back", callback_data="btn_back_main")
    ])
    return InlineKeyboardMarkup(rows)

# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: КЛАВІАТУРА АДМІН-ПАНЕЛІ
# =======================================================
def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Список підключених людей", callback_data="admin_users_list")],
        [InlineKeyboardButton("👑 Надати VIP", callback_data="admin_grant_vip")],
        [InlineKeyboardButton("🤖 Надати доступ до бота", callback_data="admin_grant_bot")],
        [InlineKeyboardButton("📈 Оновити ROI монет", callback_data="admin_roi_info")],
        [InlineKeyboardButton("🔙 Закрити", callback_data="btn_back_main")]
    ])
# =======================================================

# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ПІДРАХУНОК ДНІВ, ЩО ЗАЛИШИЛИСЬ
# =======================================================
def calc_days_left(end_dt) -> int:
    """Повертає кількість повних днів, що залишились до end_dt (0, якщо термін минув).

    Приймає datetime (як повертає asyncpg для колонок TIMESTAMPTZ) або
    ISO-рядок (для зворотної сумісності)."""
    try:
        if isinstance(end_dt, str):
            end_dt = datetime.fromisoformat(end_dt)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = end_dt - now
        if delta.total_seconds() <= 0:
            return 0
        days = delta.days + (1 if delta.seconds > 0 or delta.microseconds > 0 else 0)
        return max(days, 0)
    except Exception:
        return 0
# =======================================================

# --- ТЕКСТИ ПОВІДОМЛЕНЬ ---

def get_text_start(lang="ua"):
    if lang == "ua":
        return (
            "👋 **Вітаємо у спільноті Kerdos!**\n\n"
            "Я — **Corvin**, ваш персональний помічник аналітичної торгової системи **Kerdos**.\n\n"
            "🎁 **Спеціальні пропозиції та Бонуси:**\n"
            "• 🚀 **14 днів FREE-доступу:** Кожен новий користувач отримує 2 тижні безкоштовного тестового доступу до VIP-групи Kerdos!\n"
            "• 👥 **Реферальна програма «Приведи друга»:** За кожного друга, який візьме безкоштовний пробний період — отримуй **+14 днів безкоштовного доступу**!\n\n"
            "💎 **Наші Послуги та Прайс:**\n"
            "• 📊 **VIP-група з сигналами Kerdos:** **$20 / 30 днів** *(Аналітика ринку, торгові сигнали та чат спільноти)*\n"
            "• 🤖 **Персональний Signal Bot:** **$100 / 30 днів** *(Автоматичне підключення вашого акаунту OKX для миттєвої торгівлі)*\n\n"
            "⚠️ **Управління ризиками та відповідальність:**\n"
            "• 📈 Торгівля на криптовалютному ринку завжди пов'язана з високими ризиками.\n"
            "• 🛡️ Обов'язково дотримуйтесь суворого **ризик- та мані-менеджменту** — контролюйте розмір плеча та закладайте безпечний відсоток депозиту на одну угоду.\n"
            "• ⚖️ Ми **не несемо відповідальності** за ваш баланс та фінансові результати — ви повністю контролюєте власні кошти та самостійно приймаєте рішення.\n"
            "• 🔥 Проте при дотриманні дисципліни, системного підходу та правил стратегії — це дає чудові результати!\n\n"
            "📜 **Правила спільноти:**\n"
            "• 🚫 Без спаму, флуду, реклами та реферальних посилань.\n"
            "• 🤝 Ввічливе спілкування, без мату та токсичності.\n"
            "• 🛡️ Шахрайство = негайний бан.\n\n"
            "👇 **Обери потрібну дію з меню нижче:**"
        )
    return (
        "👋 **Welcome to the Kerdos community!**\n\n"
        "I am **Corvin**, your personal assistant for the **Kerdos** trading system.\n\n"
        "🎁 **Special Offers & Bonuses:**\n"
        "• 🚀 **14-Day FREE Trial:** Every new user gets 2 weeks of free trial access to our Kerdos VIP Signals Group!\n"
        "• 👥 **\"Refer a Friend\" Program:** Bring a friend, and once they claim their free trial, get **+14 days of free VIP access**!\n\n"
        "💎 **Services & Pricing:**\n"
        "• 📊 **Kerdos VIP Signals Group:** **$20 / 30 days** *(Market analytics, trade signals, and community access)*\n"
        "• 🤖 **Personal Signal Bot Setup:** **$100 / 30 days** *(Direct OKX bot connection for automated signal execution)*\n\n"
        "⚠️ **Risk Management & Disclaimer:**\n"
        "• 📈 Cryptocurrency trading involves substantial financial risk.\n"
        "• 🛡️ Always practice strict **risk and money management** — control your leverage and allocate a safe percentage of your capital per trade.\n"
        "• ⚖️ We **are not responsible** for your balance or trading outcomes — you maintain full control over your funds and make decisions independently.\n"
        "• 🔥 However, with proper discipline and strategic rule execution, it yields excellent long-term results!\n\n"
        "📜 **Community Rules:**\n"
        "• 🚫 No spam, flooding, self-promotion, or referral links.\n"
        "• 🤝 Respectful communication, no profanity or toxicity.\n"
        "• 🛡️ Fraudulent behavior results in an immediate permanent ban.\n\n"
        "👇 **Choose an option from the menu below:**"
    )

def get_text_support_prompt(lang="ua"):
    if lang == "ua":
        return (
            "🛟 **СЛУЖБА ПІДТРИМКИ KERDOS**\n\n"
            "Ви виявили помилку, маєте запитання щодо підписки або потребуєте допомоги з налаштуванням?\n\n"
            "📝 **Будь ласка, опишіть вашу проблему нижче в одному повідомленні:**\n"
            "*(Ви також можете додати скріншот або фото помилки)*\n\n"
            "⏳ *Corvin одразу ж передасть ваше звернення адміністратору!*"
        )
    return (
        "🛟 **KERDOS SUPPORT HELPDESK**\n\n"
        "Did you encounter an issue, have questions about your subscription, or need setup assistance?\n\n"
        "📝 **Please describe your issue below in a single message:**\n"
        "*(You can also attach a screenshot or photo)*\n\n"
        "⏳ *Corvin will forward your ticket directly to the administrator!*"
    )

def get_text_vip_payment(lang="ua"):
    if lang == "ua":
        return (
            "💳 **Оплата підписки на VIP-групу Kerdos ($20 / 30 днів)**\n\n"
            "Для активації підписки перекажіть **20 USDT** на один із гаманців Binance нижче:\n\n"
            f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
            f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
            f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
            "*(Натисніть на адресу, щоб її скопіювати)*\n\n"
            "📥 **ПІДТВЕРДЖЕННЯ ОПЛАТИ:**\n"
            "Після виконання переказу **надішліть квитанцію (фото, скріншот або текст з хешем транзакції) сюди в чат**.\n\n"
            "Я (Corvin) передам її адміністратору на перевірку, і доступ буде надано!"
        )
    return (
        "💳 **Kerdos VIP Group Subscription ($20 / 30 days)**\n\n"
        "To activate your subscription, send **20 USDT** to one of the Binance wallets below:\n\n"
        f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
        f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
        f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
        "*(Tap the address to copy it)*\n\n"
        "📥 **HOW TO CONFIRM PAYMENT:**\n"
        "After completing the transfer, **send the receipt (photo, screenshot, or transaction TxID) directly into this chat**.\n\n"
        "I (Corvin) will forward it to the admin for verification!"
    )

def get_text_bot_payment(lang="ua"):
    if lang == "ua":
        return (
            "🤖 **Підключення Kerdos Signal Bot ($100 / 30 днів)**\n\n"
            "Персональний бот для автоматичного виконання сигналів **Kerdos** на вашому акаунті OKX.\n\n"
            "⚡ **Переваги:**\n"
            "• Автоматичне відкриття/закриття угод 24/7\n"
            "• Без передачі API-ключів (безпечно через Signal Token)\n"
            "• Миттєва швидкість виконання сигналів\n\n"
            "💳 **Вартість:** **$100 / 30 днів**\n\n"
            "Перекажіть **100 USDT** на один із гаманців Binance:\n\n"
            f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
            f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
            f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
            "📥 **ПІДТВЕРДЖЕННЯ ОПЛАТИ:**\n"
            "Після переказу **надішліть квитанцію (скріншот або хеш) сюди в чат**."
        )
    return (
        "🤖 **Connect Kerdos Signal Bot ($100 / 30 days)**\n\n"
        "Automated bot for executing **Kerdos** signals directly on your OKX account.\n\n"
        "⚡ **Benefits:**\n"
        "• 24/7 automated trade execution\n"
        "• Safe setup without sharing API keys (via Signal Token)\n"
        "• Instant signal execution speed\n\n"
        "💳 **Price:** **$100 / 30 days**\n\n"
        "Send **100 USDT** to one of the Binance wallets below:\n\n"
        f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
        f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
        f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
        "📥 **HOW TO CONFIRM PAYMENT:**\n"
        "After transferring, **send your receipt (photo, screenshot, or TxID) into this chat**."
    )

def get_text_services(lang="ua"):
    if lang == "ua":
        return (
            "💎 **Наші Послуги та Прайс (Kerdos)**\n\n"
            "📊 **VIP-група з сигналами:** **$20 / 30 днів**\n\n"
            "🤖 **Персональний Signal Bot:** **$100 / 30 днів**\n\n"
            "🎁 **Бонуси:**\n"
            "• **14 днів FREE** для нових користувачів!\n"
            "• **+14 днів** за кожного друга, який візьме безкоштовний пробний період!"
        )
    return (
        "💎 **Services & Pricing (Kerdos)**\n\n"
        "📊 **VIP Signals Group Access:** **$20 / 30 days**\n\n"
        "🤖 **Personal Signal Bot Setup:** **$100 / 30 days**\n\n"
        "🎁 **Bonuses:**\n"
        "• **14-Day FREE Trial** for new users!\n"
        "• **+14 Days Free Access** for every referred friend who claims their free trial!"
    )

def get_text_rules(lang="ua"):
    if lang == "ua":
        return (
            "📜 **Правила спільноти Kerdos**\n\n"
            "🚫 **Без спаму та флуду:** Масові розсилки заборонені.\n"
            "❌ **Заборона реклами:** Реклама без дозволу заборонена.\n"
            "🤝 **Повага та етика:** Образи та токсичність неприпустимі.\n"
            "🤬 **Без нецензурної лексики:** Дотримуємося ввічливого спілкування.\n"
            "🛡️ **Без шахрайства:** Спроби скаму = бан."
        )
    return (
        "📜 **Kerdos Community Rules**\n\n"
        "🚫 **No Spam or Flooding:** Mass messaging is prohibited.\n"
        "❌ **No Advertising:** Self-promotion is forbidden.\n"
        "🤝 **Respect & Courtesy:** Toxicity will not be tolerated.\n"
        "🤬 **No Profanity:** Keep communication polite and clean.\n"
        "🛡️ **No Scams:** Immediate permanent ban."
    )

def get_text_choose_coin(lang="ua"):
    if lang == "ua":
        return (
            "🪙 **Оберіть монету для Signal Bot**\n\n"
            "Ваш Signal Bot працює лише з **однією монетою**. Оберіть, за якою парою ви хочете отримувати "
            "автоматичні сигнали (у дужках — ROI за минулий місяць за даними щомісячного звіту Kerdos):"
        )
    return (
        "🪙 **Choose a coin for your Signal Bot**\n\n"
        "Your Signal Bot works with **one coin only**. Pick the pair you want automated signals for "
        "(the number in brackets is last month's ROI from the Kerdos monthly report):"
    )

def get_text_coin_selected(ticker: str, lang="ua"):
    display = ticker.replace("USDT", "")
    if lang == "ua":
        return (
            f"✅ **Монету обрано: {display}**\n\n"
            "Тепер, будь ласка, надайте ваш **Signal Token**.\n\n"
            "📍 **Де знайти Signal Token на OKX:**\n"
            "1. Зайдіть на біржу **OKX** ➔ розділ **Торгувати (Trade)** ➔ **Торгові боти (Trading Bots)**.\n"
            "2. Оберіть **Сигнальний бот (Signal Bot)** ➔ **Створити власні сигнали (Create Custom Signal)**.\n"
            "3. Введіть назву сигналу (наприклад, `Kerdos Signals`) та натисніть **Створити**.\n"
            "4. Скопіюйте рядок **Signal Token** з налаштувань бота.\n\n"
            "📥 **Надішліть ваш токен у цей чат у такому форматі:**\n"
            "`Token: ваш_signal_token_тут`\n\n"
            "⏳ *Наш адміністратор вручну додасть ваш токен до сповіщень TradingView для обраної монети.*"
        )
    return (
        f"✅ **Coin selected: {display}**\n\n"
        "Now please provide your **Signal Token**.\n\n"
        "📍 **Where to find Signal Token on OKX:**\n"
        "1. Go to **OKX** ➔ **Trade** ➔ **Trading Bots**.\n"
        "2. Select **Signal Bot** ➔ **Create Custom Signal**.\n"
        "3. Name your signal (e.g., `Kerdos Signals`) and click **Create**.\n"
        "4. Copy the **Signal Token** string from the bot settings.\n\n"
        "📥 **Send your token in this chat using the format:**\n"
        "`Token: your_signal_token_here`\n\n"
        "⏳ *Our admin will manually add your token to the TradingView alert for your chosen coin.*"
    )

def get_text_token_saved(lang="ua"):
    if lang == "ua":
        return (
            "✅ **Signal Token отримано!**\n\n"
            "Ваш токен передано для інтеграції з системою сповіщень TradingView.\n\n"
            "⏳ Процес налаштування буде завершено протягом поточного дня."
        )
    return (
        "✅ **Signal Token received!**\n\n"
        "Your token has been submitted for integration with the TradingView alert system.\n\n"
        "⏳ The setup process will be completed within the day.."
    )

def get_text_token_invalid(lang="ua"):
    if lang == "ua":
        return (
            "⚠️ **Токен виглядає порожнім або занадто коротким.**\n\n"
            "Будь ласка, скопіюйте повний **Signal Token** з OKX і надішліть його у форматі:\n"
            "`Token: ваш_signal_token_тут`"
        )
    return (
        "⚠️ **The token looks empty or too short.**\n\n"
        "Please copy the full **Signal Token** from OKX and send it in the format:\n"
        "`Token: your_signal_token_here`"
    )

# --- РЕФЕРАЛЬНА ПРОГРАМА ТА ЛОГІКА ТРИАЛУ ---

async def get_referral_text(user_id: int, bot_username: str, lang: str = "ua") -> str:
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    async with db_pool.acquire() as db:
        active_refs = await db.fetchval(
            "SELECT COUNT(*) FROM users WHERE referrer_id = $1 AND trial_used = 1", user_id
        )

    if lang == "ua":
        return (
            "👥 **Реферальна програма Kerdos «Приведи друга»**\n\n"
            "Запрошуйте друзів та отримуйте **+14 днів безкоштовного доступу** до VIP-групи за кожного друга, який активує безкоштовний пробний період!\n\n"
            f"🔗 **Ваше персональне посилання:**\n`{ref_link}`\n\n"
            f"📊 **Ваші запрошені друзі, які взяли FREE-триал:** {active_refs}\n\n"
            "*(Натисніть на посилання, щоб скопіювати його та поділитися з друзями)*"
        )
    return (
        "👥 **Kerdos Referral Program \"Refer a Friend\"**\n\n"
        "Invite your friends and receive **+14 days of free VIP access** for every friend who activates their free trial!\n\n"
        f"🔗 **Your personal referral link:**\n`{ref_link}`\n\n"
        f"📊 **Friends who claimed FREE trial:** {active_refs}\n\n"
        "*(Tap the link to copy and share it with your friends)*"
    )

async def handle_free_trial_request(user_id: int, username: str, lang: str = "ua"):
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=14)

    async with db_pool.acquire() as db:
        user = await db.fetchrow("SELECT trial_used, referrer_id FROM users WHERE user_id = $1", user_id)

        if user and user["trial_used"] == 1:
            if lang == "ua":
                return "⚠️ **Ви вже використовували безкоштовний 14-денний період.**\n\nВи можете оформити підписку у головному меню."
            return "⚠️ **You have already used your 14-day free trial.**\n\nYou can subscribe in the main menu."

        referrer_id = user["referrer_id"] if user else None

        try:
            if not TELEGRAM_CHANNEL_ID:
                return "❌ Помилка: Не налаштовано TELEGRAM_CHANNEL_ID."

            invite_link = await bot.create_chat_invite_link(
                chat_id=TELEGRAM_CHANNEL_ID,
                member_limit=1,
                expire_date=int((now + timedelta(hours=24)).timestamp())
            )

            await db.execute("""
                INSERT INTO users (user_id, username, trial_used, trial_start, trial_end, status, lang)
                VALUES ($1, $2, 1, $3, $4, 'trial', $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    trial_used = 1,
                    trial_start = excluded.trial_start,
                    trial_end = excluded.trial_end,
                    status = 'trial'
            """, user_id, username, now, trial_end, lang)

            # --- АВТОМАТИЧНЕ НАРАХУВАННЯ БОНУСУ ЗАПРОШУЮЧОМУ ---
            if referrer_id:
                ref_user = await db.fetchrow("SELECT trial_end, sub_end, status, lang FROM users WHERE user_id = $1", referrer_id)

                if ref_user:
                    ref_trial_end, ref_sub_end, ref_status, ref_lang = ref_user["trial_end"], ref_user["sub_end"], ref_user["status"], ref_user["lang"]
                    ref_lang = ref_lang or "ua"

                    if ref_status == 'active' and ref_sub_end:
                        base_time = max(now, ref_sub_end)
                        new_end = base_time + timedelta(days=14)
                        await db.execute("UPDATE users SET sub_end = $1 WHERE user_id = $2", new_end, referrer_id)
                    else:
                        base_time = max(now, ref_trial_end) if ref_trial_end else now
                        new_end = base_time + timedelta(days=14)
                        await db.execute("UPDATE users SET trial_end = $1, status = 'trial' WHERE user_id = $2", new_end, referrer_id)

                    safe_username = escape_md(username)
                    bonus_msg = (
                        f"🥳 **Ваш друг (@{safe_username}) взяв безкоштовний тестовий період!**\n\n"
                        f"🎁 Вам автоматично нараховано **+14 днів безкоштовного доступу** до Kerdos VIP!\n"
                        f"⏰ Новий термін дії доступу: **{new_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                        if ref_lang == "ua" else
                        f"🥳 **Your friend (@{safe_username}) claimed their free trial!**\n\n"
                        f"🎁 You have automatically received **+14 free days** of Kerdos VIP access!\n"
                        f"⏰ New expiration date: **{new_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                    )
                    try:
                        await bot.send_message(chat_id=referrer_id, text=bonus_msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Failed to notify referrer {referrer_id}: {e}")

            if lang == "ua":
                return (
                    f"🎉 **Вам надано 14 днів безкоштовного доступу до Kerdos VIP!**\n\n"
                    f"🔗 **Ваше одноразове посилання:**\n{invite_link.invite_link}\n\n"
                    f"⏰ Доступ активний до: **{trial_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                )
            return (
                f"🎉 **You have been granted 14 days of free access to Kerdos VIP!**\n\n"
                f"🔗 **Your invite link:**\n{invite_link.invite_link}\n\n"
                f"⏰ Access valid until: **{trial_end.strftime('%Y-%m-%d %H:%M UTC')}**"
            )
        except Exception as e:
            logger.error(f"Error creating invite link for user {user_id}: {e}")
            return "❌ Помилка при створенні посилання. Переконайся, що Mireya додана у групу як адмін."

# --- ПЕРЕСИЛАННЯ SIGNAL TOKEN АДМІНУ ДЛЯ РУЧНОГО ДОДАВАННЯ В TRADINGVIEW ---

async def forward_token_to_admin(user_id: int, username: str, token: str):
    """
    Замість автоматичної відправки сигналів на OKX, бот просто пересилає
    отриманий Signal Token адміну. Адмін вручну додає цей токен окремим
    рядком сповіщення (alert) у TradingView (для обраної користувачем монети),
    і TradingView вже напряму відправляє сигнал на OKX для цього користувача.
    """
    if not ADMIN_TELEGRAM_ID or not bot:
        return

    user_disp = f"@{escape_md(username)}" if username and username != "no_username" else f"ID: {user_id}"

    async with db_pool.acquire() as db:
        row = await db.fetchrow("SELECT selected_coin FROM users WHERE user_id = $1", user_id)
        selected_coin = row["selected_coin"] if row and row["selected_coin"] else "не обрано"

    admin_text = (
        "🔑 **НОВИЙ SIGNAL TOKEN ВІД КОРИСТУВАЧА**\n\n"
        f"👤 **Користувач:** {user_disp}\n"
        f"🆔 **ID:** `{user_id}`\n"
        f"🪙 **Обрана монета:** `{selected_coin}`\n\n"
        "📋 **Token (натисніть, щоб скопіювати):**\n"
        f"`{escape_md(token)}`\n\n"
        f"➡️ Додайте цей токен окремим рядком у сповіщення (Alert Message) TradingView для `{selected_coin}`, "
        "щоб цей користувач отримував сигнали напряму на OKX."
    )

    try:
        await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=admin_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не вдалося переслати token адміну для user {user_id}: {e}")

# --- ВЕБХУК TELEGRAM ---

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    global BOT_USERNAME
    try:
        data = await request.json()
        update = Update.de_json(data, bot)

        if not update:
            return {"status": "ok"}

        # 1. ОБРОБКА ПОВІДОМЛЕНЬ
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user.id
            username = update.message.from_user.username or "no_username"
            user_lang = await get_user_lang(user_id)
            is_awaiting_support = await get_awaiting_support(user_id)

            # Обробка введення Signal Token (формат Token: xxx)
            if update.message.text and update.message.text.strip().lower().startswith("token:"):
                raw_token = update.message.text.strip().split(":", 1)[1].strip()

                # =======================================================
                # БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ВАЛІДАЦІЯ ДОВЖИНИ ТОКЕНА
                # =======================================================
                if len(raw_token) < 10:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=get_text_token_invalid(user_lang),
                        parse_mode="Markdown"
                    )
                    return {"status": "ok"}
                # =======================================================

                async with db_pool.acquire() as db:
                    await db.execute("UPDATE users SET signal_token = $1 WHERE user_id = $2", raw_token, user_id)

                # Пересилаємо токен адміну для ручного додавання у TradingView
                await forward_token_to_admin(user_id, username, raw_token)

                await bot.send_message(chat_id=chat_id, text=get_text_token_saved(user_lang), parse_mode="Markdown")
                return {"status": "ok"}

            # Команди та рефералка
            if update.message.text and update.message.text.startswith("/"):
                text = update.message.text.strip()
                await set_awaiting_support(user_id, 0) # Скидаємо очікування підтримки при використанні команд

                if text.startswith("/start"):
                    args = text.split()
                    if len(args) > 1 and args[1].startswith("ref_"):
                        try:
                            ref_id = int(args[1].split("_")[1])
                            if ref_id != user_id:
                                async with db_pool.acquire() as db:
                                    await db.execute("""
                                        INSERT INTO users (user_id, username, referrer_id, lang)
                                        VALUES ($1, $2, $3, $4)
                                        ON CONFLICT (user_id) DO UPDATE SET
                                            referrer_id = COALESCE(users.referrer_id, excluded.referrer_id)
                                    """, user_id, username, ref_id, user_lang)
                        except ValueError:
                            pass

                    await bot.send_message(chat_id=chat_id, text=get_text_start(user_lang), reply_markup=get_main_keyboard(user_lang), parse_mode="Markdown")
                    return {"status": "ok"}

                elif text == "/services":
                    await bot.send_message(chat_id=chat_id, text=get_text_services(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")
                    return {"status": "ok"}
                elif text == "/rules":
                    await bot.send_message(chat_id=chat_id, text=get_text_rules(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")
                    return {"status": "ok"}

                # =======================================================
                # БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: АДМІН-КОМАНДИ
                # =======================================================
                elif text == "/admin" and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="🛠 *Панель адміністратора:*\nОберіть потрібну дію нижче:",
                        reply_markup=get_admin_panel_keyboard(),
                        parse_mode="Markdown"
                    )
                    return {"status": "ok"}

                elif text.startswith("/give_vip") and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    parts = text.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        target_user_id = int(parts[1])
                        now = datetime.now(timezone.utc)
                        new_end = now + timedelta(days=30)

                        async with db_pool.acquire() as db:
                            # =======================================================
                            # ФІКС БАГ #1: INSERT ... ON CONFLICT замість голого UPDATE,
                            # інакше видача доступу користувачу, якого ще немає в БД,
                            # оновлює 0 рядків і доступу фактично не буде.
                            # =======================================================
                            await db.execute("""
                                INSERT INTO users (user_id, status, sub_end)
                                VALUES ($1, 'VIP', $2)
                                ON CONFLICT (user_id) DO UPDATE SET
                                    status = excluded.status,
                                    sub_end = excluded.sub_end
                            """, target_user_id, new_end)

                            row = await db.fetchrow("SELECT lang FROM users WHERE user_id = $1", target_user_id)
                            target_lang = row["lang"] if row and row["lang"] else "ua"

                        user_msg = (
                            f"🎉 **Адміністратор надав вам VIP доступ на 30 днів!**"
                            if target_lang == "ua" else
                            f"🎉 **Admin granted you VIP access for 30 days!**"
                        )

                        try:
                            await bot.send_message(chat_id=target_user_id, text=user_msg, parse_mode="Markdown")
                            notification_status = "📤 Користувачу надіслано сповіщення."
                        except Exception as e:
                            notification_status = f"⚠️ Доступ оновлено в БД, але не вдалося написати користувачу: {e}"

                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Статус **VIP** на 30 днів надано користувачу `{target_user_id}`.\n\n{notification_status}",
                            parse_mode="Markdown"
                        )
                    else:
                        await bot.send_message(chat_id=chat_id, text="Помилка. Використовуйте формат: `/give_vip 123456789`", parse_mode="Markdown")
                    return {"status": "ok"}

                elif text.startswith("/give_bot") and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    parts = text.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        target_user_id = int(parts[1])
                        now = datetime.now(timezone.utc)
                        new_end = now + timedelta(days=30)

                        async with db_pool.acquire() as db:
                            # =======================================================
                            # ФІКС БАГ #1: те саме для /give_bot
                            # =======================================================
                            await db.execute("""
                                INSERT INTO users (user_id, status, bot_sub_end)
                                VALUES ($1, 'BOT', $2)
                                ON CONFLICT (user_id) DO UPDATE SET
                                    status = excluded.status,
                                    bot_sub_end = excluded.bot_sub_end
                            """, target_user_id, new_end)

                            row = await db.fetchrow("SELECT lang, signal_token FROM users WHERE user_id = $1", target_user_id)
                            target_lang = row["lang"] if row and row["lang"] else "ua"
                            user_token = row["signal_token"] if row else None

                        try:
                            if user_token:
                                safe_token_snip = f"{escape_md(user_token[:6])}...{escape_md(user_token[-4:])}"
                                user_msg = (
                                    f"🎉 **Адміністратор надав вам доступ до Kerdos Signal Bot на 30 днів!**\n\n"
                                    f"✅ Ваш Signal Token вже наявний у системі та переданий адміну для підключення.\n"
                                    f"🔑 **Токен:** `{safe_token_snip}`"
                                    if target_lang == "ua" else
                                    f"🎉 **Admin granted you Kerdos Signal Bot access for 30 days!**\n\n"
                                    f"✅ Your Signal Token is already on file and has been passed along for setup.\n"
                                    f"🔑 **Token:** `{safe_token_snip}`"
                                )
                                await bot.send_message(chat_id=target_user_id, text=user_msg, parse_mode="Markdown")
                                # На випадок, якщо токен ще не пересилався — пересилаємо ще раз
                                await forward_token_to_admin(target_user_id, "", user_token)
                            else:
                                intro_msg = (
                                    "🎉 **Адміністратор надав вам доступ до Kerdos Signal Bot на 30 днів!**"
                                    if target_lang == "ua" else
                                    "🎉 **Admin granted you Kerdos Signal Bot access for 30 days!**"
                                )
                                await bot.send_message(chat_id=target_user_id, text=intro_msg, parse_mode="Markdown")
                                await bot.send_message(
                                    chat_id=target_user_id,
                                    text=get_text_choose_coin(target_lang),
                                    reply_markup=await get_coin_selection_keyboard(target_lang),
                                    parse_mode="Markdown"
                                )
                            notification_status = "📤 Користувачу надіслано сповіщення та інструкцію."
                        except Exception as e:
                            notification_status = f"⚠️ Доступ оновлено в БД, але не вдалося написати користувачу: {e}"

                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Статус **Bot** на 30 днів надано користувачу `{target_user_id}`.\n\n{notification_status}",
                            parse_mode="Markdown"
                        )
                    else:
                        await bot.send_message(chat_id=chat_id, text="Помилка. Використовуйте формат: `/give_bot 123456789`", parse_mode="Markdown")
                    return {"status": "ok"}

                # =======================================================
                # БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ADMIN ROI-КОМАНДИ (щомісячний звіт)
                # =======================================================
                elif text.startswith("/set_roi") and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    parts = text.split()
                    if len(parts) == 3:
                        ticker_raw = parts[1].upper()
                        try:
                            roi_value = float(parts[2].replace(",", "."))
                        except ValueError:
                            await bot.send_message(chat_id=chat_id, text="Помилка. ROI має бути числом, напр. `/set_roi BTCUSDT 12.5`", parse_mode="Markdown")
                            return {"status": "ok"}

                        if ticker_raw not in AVAILABLE_COINS:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"Помилка. `{ticker_raw}` немає у списку доступних монет.",
                                parse_mode="Markdown"
                            )
                            return {"status": "ok"}

                        await set_coin_roi(ticker_raw, roi_value)
                        sign = "+" if roi_value >= 0 else ""
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ ROI для `{ticker_raw}` встановлено: **{sign}{roi_value:.1f}%** (за минулий місяць).",
                            parse_mode="Markdown"
                        )
                    else:
                        await bot.send_message(
                            chat_id=chat_id,
                            text="Помилка. Використовуйте формат: `/set_roi TICKER VALUE`\n*(Наприклад: /set_roi BTCUSDT 12.5)*",
                            parse_mode="Markdown"
                        )
                    return {"status": "ok"}

                elif text == "/roi_list" and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    roi_map = await get_all_coin_roi()
                    lines = ["📊 *Поточний ROI за монетами (щомісячний звіт):*\n"]
                    for ticker in AVAILABLE_COINS:
                        roi = roi_map.get(ticker)
                        if roi is None:
                            lines.append(f"• `{ticker}` — н/д")
                        else:
                            sign = "+" if roi >= 0 else ""
                            lines.append(f"• `{ticker}` — {sign}{roi:.1f}%")
                    lines.append("\nОновити: `/set_roi TICKER VALUE`")
                    await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
                    return {"status": "ok"}
                # =======================================================

            # 📩 ОБРОБКА ЗВЕРНЕННЯ В ПІДТРИМКУ
            if is_awaiting_support == 1 and ADMIN_TELEGRAM_ID and user_id != ADMIN_TELEGRAM_ID:
                await set_awaiting_support(user_id, 0)

                admin_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Ввійти в чат / Відповісти", url=f"tg://user?id={user_id}")]
                ])

                safe_username = escape_md(username)
                support_header = f"🛟 **НОВЕ ЗВЕРНЕННЯ В ПІДТРИМКУ!**\n\n👤 **Від:** @{safe_username}\n🆔 **ID:** `{user_id}`\n🌐 **Мова:** {user_lang.upper()}\n"

                if update.message.photo:
                    photo_file_id = update.message.photo[-1].file_id
                    caption_text = f"{support_header}\n📝 **Опис:**\n{escape_md(update.message.caption) if update.message.caption else 'Без опису'}"
                    await bot.send_photo(
                        chat_id=ADMIN_TELEGRAM_ID,
                        photo=photo_file_id,
                        caption=caption_text,
                        reply_markup=admin_keyboard,
                        parse_mode="Markdown"
                    )
                elif update.message.text:
                    full_support_text = f"{support_header}\n📝 **Опис помилки:**\n{escape_md(update.message.text)}"
                    await bot.send_message(
                        chat_id=ADMIN_TELEGRAM_ID,
                        text=full_support_text,
                        reply_markup=admin_keyboard,
                        parse_mode="Markdown"
                    )

                confirm_text = (
                    "🚀 **Ваше звернення успішно передано адміністратору!**\n\nМи розглянемо його найближчим часом та зв'яжемося з вами."
                    if user_lang == "ua" else
                    "🚀 **Your support request has been delivered to the admin!**\n\nWe will review it and get back to you shortly."
                )
                await bot.send_message(chat_id=chat_id, text=confirm_text, reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")
                return {"status": "ok"}

            # 💳 ОБРОБКА КВИТАНЦІЙ ПРО ОПЛАТУ
            if ADMIN_TELEGRAM_ID and user_id != ADMIN_TELEGRAM_ID:
                admin_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Підтвердити VIP ($20)", callback_data=f"approve_vip_{user_id}"),
                        InlineKeyboardButton("🤖 Підтвердити Bot ($100)", callback_data=f"approve_bot_{user_id}")
                    ],
                    [InlineKeyboardButton("❌ Відхилити", callback_data=f"decline_{user_id}")]
                ])

                safe_username = escape_md(username)
                admin_text = f"📩 **НОВА КВИТАНЦІЯ!**\n\n👤 **Користувач:** @{safe_username}\n🆔 **ID:** `{user_id}`\n🌐 **Мова:** {user_lang.upper()}"

                if update.message.photo:
                    photo_file_id = update.message.photo[-1].file_id
                    await bot.send_photo(
                        chat_id=ADMIN_TELEGRAM_ID,
                        photo=photo_file_id,
                        caption=admin_text,
                        reply_markup=admin_keyboard,
                        parse_mode="Markdown"
                    )
                    reply_msg = "✅ **Вашу квитанцію (фото) отримано!** Адміністратор перевірить її найближчим часом." if user_lang == "ua" else "✅ **Receipt received!** The admin will review it shortly."
                    await bot.send_message(chat_id=chat_id, text=reply_msg)
                    return {"status": "ok"}

                elif update.message.text:
                    full_admin_text = f"{admin_text}\n\n📝 **Текст / Хеш:**\n`{escape_md(update.message.text)}`"
                    await bot.send_message(
                        chat_id=ADMIN_TELEGRAM_ID,
                        text=full_admin_text,
                        reply_markup=admin_keyboard,
                        parse_mode="Markdown"
                    )
                    reply_msg = "✅ **Вашу квитанцію отримано!** Адміністратор перевірить її найближчим часом." if user_lang == "ua" else "✅ **Receipt received!** The admin will review it shortly."
                    await bot.send_message(chat_id=chat_id, text=reply_msg)
                    return {"status": "ok"}

        # 2. ОБРОБКА CALLBACK-КНОПОК
        elif update.callback_query:
            query = update.callback_query
            user_id = query.from_user.id
            username = query.from_user.username or "no_username"
            data = query.data
            user_lang = await get_user_lang(user_id)

            await query.answer()

            if data == "lang_ua":
                await set_user_lang(user_id, "ua")
                await query.edit_message_text(text=get_text_start("ua"), reply_markup=get_main_keyboard("ua"), parse_mode="Markdown")
            elif data == "lang_en":
                await set_user_lang(user_id, "en")
                await query.edit_message_text(text=get_text_start("en"), reply_markup=get_main_keyboard("en"), parse_mode="Markdown")

            elif data == "btn_back_main":
                await set_awaiting_support(user_id, 0)
                await query.edit_message_text(text=get_text_start(user_lang), reply_markup=get_main_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_support":
                await set_awaiting_support(user_id, 1)
                await query.edit_message_text(text=get_text_support_prompt(user_lang), reply_markup=get_cancel_support_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_cancel_support":
                await set_awaiting_support(user_id, 0)
                cancel_msg = "❌ Звернення в підтримку скасовано." if user_lang == "ua" else "❌ Support request cancelled."
                await query.edit_message_text(text=f"{cancel_msg}\n\n{get_text_start(user_lang)}", reply_markup=get_main_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_services":
                await query.edit_message_text(text=get_text_services(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_rules":
                await query.edit_message_text(text=get_text_rules(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_buy_group":
                await query.edit_message_text(text=get_text_vip_payment(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_connect_bot":
                await query.edit_message_text(text=get_text_bot_payment(user_lang), reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_referral":
                # =======================================================
                # ФІКС БАГ #3: username бота кешується один раз (у lifespan,
                # з фолбеком тут якщо кеш ще порожній), а не запитується
                # у Telegram щоразу на клік по цій кнопці.
                # =======================================================
                if not BOT_USERNAME:
                    try:
                        me = await bot.get_me()
                        BOT_USERNAME = me.username
                    except Exception as e:
                        logger.error(f"Не вдалося отримати username бота: {e}")
                ref_text = await get_referral_text(user_id, BOT_USERNAME or "kerdos_bot", user_lang)
                await query.edit_message_text(text=ref_text, reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_free_trial":
                res_text = await handle_free_trial_request(user_id, username, user_lang)
                await query.edit_message_text(text=res_text, reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data == "btn_my_sub":
                async with db_pool.acquire() as db:
                    row = await db.fetchrow(
                        "SELECT status, trial_end, sub_end, bot_sub_end, signal_token, selected_coin FROM users WHERE user_id = $1",
                        user_id
                    )

                if not row:
                    sub_info = "У вас немає активних підписок." if user_lang == "ua" else "You have no active subscriptions."
                else:
                    status, t_end, s_end, b_end, token, selected_coin = (
                        row["status"], row["trial_end"], row["sub_end"], row["bot_sub_end"],
                        row["signal_token"], row["selected_coin"]
                    )
                    status_label = status.upper() if status else "FREE"
                    sub_info = (
                        f"📊 **Статус:** `{status_label}`\n\n"
                        if user_lang == "ua" else
                        f"📊 **Status:** `{status_label}`\n\n"
                    )

                    if t_end:
                        days_left = calc_days_left(t_end)
                        t_end_disp = t_end.strftime('%Y-%m-%d %H:%M')
                        if user_lang == "ua":
                            sub_info += f"🎁 **Триал:** залишилось {days_left} дн. (до {t_end_disp} UTC)\n"
                        else:
                            sub_info += f"🎁 **Trial:** {days_left} days left (until {t_end_disp} UTC)\n"

                    if s_end:
                        days_left = calc_days_left(s_end)
                        s_end_disp = s_end.strftime('%Y-%m-%d %H:%M')
                        if user_lang == "ua":
                            sub_info += f"💎 **VIP-група:** залишилось {days_left} дн. (до {s_end_disp} UTC)\n"
                        else:
                            sub_info += f"💎 **VIP Group:** {days_left} days left (until {s_end_disp} UTC)\n"

                    if b_end:
                        days_left = calc_days_left(b_end)
                        b_end_disp = b_end.strftime('%Y-%m-%d %H:%M')
                        if user_lang == "ua":
                            sub_info += f"🤖 **Signal Bot:** залишилось {days_left} дн. (до {b_end_disp} UTC)\n"
                        else:
                            sub_info += f"🤖 **Signal Bot:** {days_left} days left (until {b_end_disp} UTC)\n"

                    if selected_coin:
                        coin_label = "🪙 **Монета:**" if user_lang == "ua" else "🪙 **Coin:**"
                        sub_info += f"{coin_label} `{selected_coin}`\n"

                    if token:
                        sub_info += f"🔑 **OKX Token:** `{escape_md(token[:6])}...{escape_md(token[-4:])}`"

                await query.edit_message_text(text=sub_info, reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")

            elif data.startswith("coin_"):
                selected_ticker = data.replace("coin_", "", 1)
                if selected_ticker in AVAILABLE_COINS:
                    await set_user_selected_coin(user_id, selected_ticker)
                    await query.edit_message_text(
                        text=get_text_coin_selected(selected_ticker, user_lang),
                        reply_markup=get_back_keyboard(user_lang),
                        parse_mode="Markdown"
                    )
                else:
                    await query.answer("Невідома монета." if user_lang == "ua" else "Unknown coin.", show_alert=True)

            # =======================================================
            # БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ОБРОБКА КНОПОК АДМІН-ПАНЕЛІ
            # =======================================================

            elif data.startswith("admin_"):
                if user_id != ADMIN_TELEGRAM_ID:
                    await query.answer("У вас немає доступу до цієї функції!", show_alert=True)
                    return {"status": "ok"}

                if data == "admin_users_list":
                    async with db_pool.acquire() as db:
                        users = await db.fetch("""
                            SELECT user_id, username, status, trial_end, sub_end, bot_sub_end
                            FROM users
                            ORDER BY user_id DESC
                            LIMIT 30
                        """)

                    if not users:
                        text = "📊 База користувачів порожня."
                    else:
                        text = "📊 *Останні користувачі та їх активні послуги:*\n\n"
                        now_utc = datetime.now(timezone.utc)

                        for u_id, u_name, u_status, t_end, s_end, b_end in users:
                            u_name_disp = f"@{escape_md(u_name)}" if u_name and u_name != "no_username" else f"ID: `{u_id}`"
                            services = []

                            if t_end and t_end > now_utc:
                                days_left = calc_days_left(t_end)
                                services.append(f"⏳ Тріал: {days_left} дн. (до {t_end.strftime('%d.%m')})")

                            if s_end and s_end > now_utc:
                                days_left = calc_days_left(s_end)
                                services.append(f"💎 VIP: {days_left} дн. (до {s_end.strftime('%d.%m')})")

                            if b_end and b_end > now_utc:
                                days_left = calc_days_left(b_end)
                                services.append(f"🤖 Bot: {days_left} дн. (до {b_end.strftime('%d.%m')})")

                            services_str = " | ".join(services) if services else "немає активних послуг"
                            status_disp = u_status.upper() if u_status else "FREE"
                            text += f"👤 {u_name_disp} — Статус: `{status_disp}`\n└ {services_str}\n\n"

                    await query.edit_message_text(text=text, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")

                elif data == "admin_grant_vip":
                    await query.edit_message_text(
                        text="Для надання VIP доступу, надішліть команду в чат у форматі:\n`/give_vip USER_ID`\n*(Наприклад: /give_vip 123456789)*",
                        reply_markup=get_admin_panel_keyboard(),
                        parse_mode="Markdown"
                    )

                elif data == "admin_grant_bot":
                    await query.edit_message_text(
                        text="Для надання доступу до Signal Bot, надішліть команду в чат у форматі:\n`/give_bot USER_ID`\n*(Наприклад: /give_bot 123456789)*",
                        reply_markup=get_admin_panel_keyboard(),
                        parse_mode="Markdown"
                    )

                elif data == "admin_roi_info":
                    roi_map = await get_all_coin_roi()
                    lines = ["📈 *Поточний ROI за монетами (щомісячний звіт):*\n"]
                    for ticker in AVAILABLE_COINS:
                        roi = roi_map.get(ticker)
                        if roi is None:
                            lines.append(f"• `{ticker}` — н/д")
                        else:
                            sign = "+" if roi >= 0 else ""
                            lines.append(f"• `{ticker}` — {sign}{roi:.1f}%")
                    lines.append("\nОновити одну монету: `/set_roi TICKER VALUE`\n*(Наприклад: /set_roi BTCUSDT 12.5)*")
                    await query.edit_message_text(text="\n".join(lines), reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")
            # =======================================================

            # АДМІНСЬКІ ДІЇ (ПІДТВЕРДЖЕННЯ / ВІДХИЛЕННЯ)
            elif data.startswith(("approve_vip_", "approve_bot_", "decline_")) and user_id == ADMIN_TELEGRAM_ID:
                action_type, target_user_id = data.rsplit("_", 1)
                target_user_id = int(target_user_id)
                target_lang = await get_user_lang(target_user_id)
                now = datetime.now(timezone.utc)
                new_end = now + timedelta(days=30)

                async with db_pool.acquire() as db:
                    if action_type == "approve_vip":
                        await db.execute("UPDATE users SET status = 'active', sub_end = $1 WHERE user_id = $2", new_end, target_user_id)

                        invite_link = await bot.create_chat_invite_link(chat_id=TELEGRAM_CHANNEL_ID, member_limit=1) if TELEGRAM_CHANNEL_ID else None
                        link_str = invite_link.invite_link if invite_link else "Перевірте канал."

                        user_msg = f"🎉 **Оплату VIP-групи підтверджено!**\n\n🔗 Ваше посилання для входу: {link_str}" if target_lang == "ua" else f"🎉 **VIP payment approved!**\n\n🔗 Link: {link_str}"
                        await bot.send_message(chat_id=target_user_id, text=user_msg)
                        await query.edit_message_text(text=f"✅ VIP підтверджено для ID: `{target_user_id}`")

                    elif action_type == "approve_bot":
                        await db.execute("UPDATE users SET bot_sub_end = $1 WHERE user_id = $2", new_end, target_user_id)

                        approve_intro = (
                            "🎉 **Оплату Kerdos Signal Bot підтверджено!**"
                            if target_lang == "ua" else
                            "🎉 **Kerdos Signal Bot payment approved!**"
                        )
                        await bot.send_message(chat_id=target_user_id, text=approve_intro, parse_mode="Markdown")
                        await bot.send_message(
                            chat_id=target_user_id,
                            text=get_text_choose_coin(target_lang),
                            reply_markup=await get_coin_selection_keyboard(target_lang),
                            parse_mode="Markdown"
                        )
                        await query.edit_message_text(text=f"✅ Signal Bot підтверджено для ID: `{target_user_id}`")

                    elif action_type == "decline":
                        user_msg = "❌ **Вашу квитанцію відхилено.** Якщо ви виявили помилку, зверніться до підтримки." if target_lang == "ua" else "❌ **Receipt declined.** Please contact support if you believe this is an error."
                        await bot.send_message(chat_id=target_user_id, text=user_msg)
                        await query.edit_message_text(text=f"❌ Оплату відхилено для ID: `{target_user_id}`")

    except Exception as e:
        logger.error(f"Error handling Telegram webhook: {e}")

    return {"status": "ok"}

# --- ЕНДПОІНТ ДЛЯ ПРИЙОМУ СИГНАЛІВ З TRADINGVIEW (WEBHOOK) ---

@app.post("/webhook")
async def tradingview_webhook(request: Request):
    try:
        data = await request.json()
        raw_ticker = data.get("ticker", "UNKNOWN")
        ticker = str(raw_ticker).upper().replace(".P", "").replace("PERP", "").strip()
        
        raw_action = str(data.get("action", "")).lower().strip() # "buy" або "sell"
        price = float(data.get("price", 0.0))
        
        # Читаємо стан ринку з ключа "strategy" (ваш JSON з TV) або "market_position"
        market_pos = str(data.get("strategy") or data.get("market_position") or "").lower().strip()
        comment = str(data.get("comment", "")).lower().strip()

        # Визначаємо, чи це сигнал на закриття (flat)
        is_close_signal = (
            market_pos == "flat"
            or "close" in raw_action
            or "exit" in raw_action
            or "close" in comment
            or "exit" in comment
        )

        now = datetime.now(timezone.utc)
        now_str = now.strftime('%Y-%m-%d %H:%M UTC')
        roi_text = ""

        # --- ЗАКРИТТЯ ПОЗИЦІЇ ---
        if is_close_signal:
            active_trade = await get_active_trade(ticker)
            
            if active_trade:
                entry_price, saved_direction, _ = active_trade
                
                # Напрямок і ROI розраховуються ЗА ЗБЕРЕЖЕНИМ ВХОДОМ з БД
                if "long" in saved_direction or "buy" in saved_direction:
                    action_label = "🔒 CLOSE LONG POSITION"
                    roi = ((price - entry_price) / entry_price) * 100
                else:
                    action_label = "🔒 CLOSE SHORT POSITION"
                    roi = ((entry_price - price) / entry_price) * 100

                price_block = f"💵 **Entry Price:** {entry_price}\n💵 **Close Price:** {price}"
                roi_emoji = "📈" if roi >= 0 else "📉"
                roi_text = f"{roi_emoji} **ROI:** `{roi:+.2f}%`\n"

                await delete_active_trade(ticker)
            else:
                action_label = "🔒 CLOSE POSITION"
                price_block = f"💵 **Close Price:** {price}"
                roi_text = "⚠️ *Ціну входу в базі не знайдено*\n"

            db_action = "close"

        # --- ВХІД У ПОЗИЦІЮ ---
        else:
            if market_pos == "long" or "buy" in raw_action:
                action_label = "🟢 BUY / LONG"
                db_action = "buy"
                direction_type = "long"
            elif market_pos == "short" or "sell" in raw_action:
                action_label = "🔴 SELL / SHORT"
                db_action = "sell"
                direction_type = "short"
            else:
                action_label = raw_action.upper()
                db_action = raw_action
                direction_type = raw_action

            await save_active_trade(ticker, price, direction_type, now_str)
            price_block = f"💵 **Entry Price:** {price}"

        # 1. Запис в історію
        async with db_pool.acquire() as db:
            await db.execute(
                "INSERT INTO trades (ticker, action, price, timestamp) VALUES ($1, $2, $3, $4)",
                ticker, db_action, price, now
            )

        # 2. Сповіщення в Telegram
        if TELEGRAM_CHANNEL_ID and bot:
            signal_text = (
                f"⚡ **KERDOS SIGNAL** ⚡\n\n"
                f"🪙 **Coin:** #{ticker}\n"
                f"🎯 **Action:** {action_label}\n"
                f"{price_block}\n"
                f"{roi_text}"
                f"⏰ **Time:** {now_str}"
            )
            await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=signal_text, parse_mode="Markdown")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing TradingView webhook: {e}")
        return {"status": "error", "message": str(e)}
