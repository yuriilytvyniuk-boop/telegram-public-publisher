import os
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
import aiosqlite

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ЗМІННІ ОТОЧЕННЯ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # ID VIP-групи або каналу
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))  # ID адміна

# 🔗 Посилання на загальну групу спілкування
PUBLIC_CHAT_LINK = os.getenv("PUBLIC_CHAT_LINK", "https://t.me/kerdos_group")

DB_PATH = "trades.db"

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
    global BOT_USERNAME
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


app = FastAPI(lifespan=lifespan)
# =======================================================

# --- БАЗА ДАНИХ ---

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                action TEXT,
                price REAL,
                roi REAL,
                timestamp DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                trial_used INTEGER DEFAULT 0,
                trial_start DATETIME,
                trial_end DATETIME,
                sub_end DATETIME,
                bot_sub_end DATETIME,
                signal_token TEXT,
                status TEXT DEFAULT 'free',
                lang TEXT DEFAULT 'ua',
                referrer_id INTEGER DEFAULT NULL,
                awaiting_support INTEGER DEFAULT 0,
                selected_coin TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coin_roi (
                ticker TEXT PRIMARY KEY,
                roi REAL,
                updated_at DATETIME
            )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS active_trades (
            symbol TEXT PRIMARY KEY,
            entry_price REAL,
            direction TEXT,
            time TEXT
        )
    """)
        # Міграція: додаємо selected_coin, якщо БД була створена до цього оновлення
        try:
            await db.execute("ALTER TABLE users ADD COLUMN selected_coin TEXT")
        except Exception:
            pass  # колонка вже існує
        await db.commit()


async def get_coin_roi(ticker: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT roi, updated_at FROM coin_roi WHERE ticker = ?", (ticker,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return row[0]
    return None

async def save_active_trade(symbol: str, entry_price: float, direction: str, time_str: str):
    """Зберігає або оновлює інформацію про відкриту позицію монети."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO active_trades (symbol, entry_price, direction, time)
            VALUES (?, ?, ?, ?)
        ''', (symbol, entry_price, direction, time_str))
        await db.commit()

async def get_active_trade(symbol: str):
    """Отримує відкриту позицію для конкретної монети."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT entry_price, direction, time FROM active_trades WHERE symbol = ?', (symbol,)) as cursor:
            row = await cursor.fetchone()
            return row

async def delete_active_trade(symbol: str):
    """Видаляє позицію з активних після її закриття."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM active_trades WHERE symbol = ?', (symbol,))
        await db.commit()

async def get_all_coin_roi() -> dict:
    result = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT ticker, roi FROM coin_roi") as cursor:
            rows = await cursor.fetchall()
            for ticker, roi in rows:
                result[ticker] = roi
    return result

async def set_coin_roi(ticker: str, roi: float):
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO coin_roi (ticker, roi, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET roi = excluded.roi, updated_at = excluded.updated_at
        """, (ticker, roi, now.isoformat()))
        await db.commit()

async def set_user_selected_coin(user_id: int, ticker: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, selected_coin)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET selected_coin = excluded.selected_coin
        """, (user_id, ticker))
        await db.commit()

async def get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]
    return "ua"

async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, lang)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang
        """, (user_id, lang))
        await db.commit()

async def set_awaiting_support(user_id: int, state: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, awaiting_support)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET awaiting_support = excluded.awaiting_support
        """, (user_id, state))
        await db.commit()

async def get_awaiting_support(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT awaiting_support FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return row[0]
    return 0

# --- ФОНОВИЙ ТАЙМЕР ЗВІЛЬНЕННЯ ТРИАЛУ ТА ПІДПИСКИ ---

async def check_expired_trials():
    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(timezone.utc)

            async with aiosqlite.connect(DB_PATH) as db:
                # 1. Завершення триалу (14 днів)
                async with db.execute(
                    "SELECT user_id, username, lang FROM users WHERE status = 'trial' AND trial_end <= ?",
                    (now.isoformat(),)
                ) as cursor:
                    expired_trials = await cursor.fetchall()

                for user_id, username, lang in expired_trials:
                    try:
                        if TELEGRAM_CHANNEL_ID:
                            await bot.ban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
                            await bot.unban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)

                        await db.execute("UPDATE users SET status = 'expired' WHERE user_id = ?", (user_id,))
                        await db.commit()

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
                async with db.execute(
                    "SELECT user_id, username, lang FROM users WHERE status = 'active' AND sub_end <= ?",
                    (now.isoformat(),)
                ) as cursor:
                    expired_subs = await cursor.fetchall()

                for user_id, username, lang in expired_subs:
                    try:
                        if TELEGRAM_CHANNEL_ID:
                            await bot.ban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)
                            await bot.unban_chat_member(chat_id=TELEGRAM_CHANNEL_ID, user_id=user_id)

                        await db.execute("UPDATE users SET status = 'expired' WHERE user_id = ?", (user_id,))
                        await db.commit()

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
                async with db.execute(
                    "SELECT user_id, username, lang, selected_coin, signal_token FROM users WHERE bot_sub_end IS NOT NULL AND bot_sub_end <= ?",
                    (now.isoformat(),)
                ) as cursor:
                    expired_bots = await cursor.fetchall()

                for user_id, username, lang, selected_coin, signal_token in expired_bots:
                    try:
                        await db.execute("UPDATE users SET bot_sub_end = NULL WHERE user_id = ?", (user_id,))
                        await db.commit()

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
    return InlineKeyboardMarkup(rows)

# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: КЛАВІАТУРА АДМІН-ПАНЕЛІ
# =======================================================
def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Список підключених людей", callback_data="admin_users_list")],
        [InlineKeyboardButton("👑 Надати VIP", callback_data="admin_grant_vip")],
        [InlineKeyboardButton("🤖 Надати доступ до бота", callback_data="admin_grant_bot")],
        [InlineKeyboardButton("📈 Оновити ROI монет", callback_data="admin_roi_info")]
    ])
# =======================================================

# =======================================================
# БЛОК ДОДАНОГО ФУНКЦІОНАЛУ: ПІДРАХУНОК ДНІВ, ЩО ЗАЛИШИЛИСЬ
# =======================================================
def calc_days_left(end_iso: str) -> int:
    """Повертає кількість повних днів, що залишились до end_iso (0, якщо термін минув)."""
    try:
        end_dt = datetime.fromisoformat(end_iso)
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
            "Я — **Mireya**, ваш персональний помічник аналітичної торгової системи **Kerdos**.\n\n"
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
        "I am **Mireya**, your personal assistant for the **Kerdos** trading system.\n\n"
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
            "⏳ *Mireya одразу ж передасть ваше звернення адміністратору!*"
        )
    return (
        "🛟 **KERDOS SUPPORT HELPDESK**\n\n"
        "Did you encounter an issue, have questions about your subscription, or need setup assistance?\n\n"
        "📝 **Please describe your issue below in a single message:**\n"
        "*(You can also attach a screenshot or photo)*\n\n"
        "⏳ *Mireya will forward your ticket directly to the administrator!*"
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
            "Я (Mireya) передам її адміністратору на перевірку, і доступ буде надано!"
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
        "I (Mireya) will forward it to the admin for verification!"
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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND trial_used = 1", (user_id,)) as cursor:
            row = await cursor.fetchone()
            active_refs = row[0] if row else 0

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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT trial_used, referrer_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()

        if user and user[0] == 1:
            if lang == "ua":
                return "⚠️ **Ви вже використовували безкоштовний 14-денний період.**\n\nВи можете оформити підписку у головному меню."
            return "⚠️ **You have already used your 14-day free trial.**\n\nYou can subscribe in the main menu."

        referrer_id = user[1] if user else None

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
                VALUES (?, ?, 1, ?, ?, 'trial', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    trial_used = 1,
                    trial_start = excluded.trial_start,
                    trial_end = excluded.trial_end,
                    status = 'trial'
            """, (user_id, username, now.isoformat(), trial_end.isoformat(), lang))
            await db.commit()

            # --- АВТОМАТИЧНЕ НАРАХУВАННЯ БОНУСУ ЗАПРОШУЮЧОМУ ---
            if referrer_id:
                async with db.execute("SELECT trial_end, sub_end, status, lang FROM users WHERE user_id = ?", (referrer_id,)) as cursor:
                    ref_user = await cursor.fetchone()

                if ref_user:
                    ref_trial_end, ref_sub_end, ref_status, ref_lang = ref_user
                    ref_lang = ref_lang or "ua"

                    if ref_status == 'active' and ref_sub_end:
                        curr_end = datetime.fromisoformat(ref_sub_end)
                        if curr_end.tzinfo is None:
                            curr_end = curr_end.replace(tzinfo=timezone.utc)
                        base_time = max(now, curr_end)
                        new_end = base_time + timedelta(days=14)
                        await db.execute("UPDATE users SET sub_end = ? WHERE user_id = ?", (new_end.isoformat(), referrer_id))
                    else:
                        curr_end = None
                        if ref_trial_end:
                            curr_end = datetime.fromisoformat(ref_trial_end)
                            if curr_end.tzinfo is None:
                                curr_end = curr_end.replace(tzinfo=timezone.utc)

                        base_time = max(now, curr_end) if curr_end else now
                        new_end = base_time + timedelta(days=14)
                        await db.execute("UPDATE users SET trial_end = ?, status = 'trial' WHERE user_id = ?", (new_end.isoformat(), referrer_id))

                    await db.commit()

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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT selected_coin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            selected_coin = row[0] if row and row[0] else "не обрано"

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
