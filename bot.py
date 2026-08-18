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

PUBLIC_CHAT_LINK = os.getenv("PUBLIC_CHAT_LINK", "https://t.me/kerdos_group")

DB_PATH = "trades.db"

WALLET_USDT_TRC20 = "THeVYP6zqgJ3jKMhNAuBxqGk47iFno6pKL"
WALLET_USDT_BEP20 = "0x97eb6c4c2fe24798ccf24ed5d52cb228f32f5f5f"
WALLET_USDT_SOLANA = "5Pcc4WUfA1qBas6P42WDYRre8ugAenNe5UsN6c2DyUox"

AVAILABLE_COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT", "LINKUSDT",
    "ONDOUSDT", "JTOUSDT", "LTCUSDT", "APTUSDT", "DOTUSDT",
    "AVAXUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT", "AAVEUSDT",
    "XMRUSDT", "ETCUSDT", "VETUSDT", "GRTUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "THETAUSDT", "DASHUSDT",
]

bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
BOT_USERNAME = None


def escape_md(text) -> str:
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


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


# --- БАЗА ДАНИХ ---

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
        try:
            await db.execute("ALTER TABLE users ADD COLUMN selected_coin TEXT")
        except Exception:
            pass
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


# --- ФОНОВИЙ ТАЙМЕР ---

async def check_expired_trials():
    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(timezone.utc)

            async with aiosqlite.connect(DB_PATH) as db:
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
                                "➡️ Видаліть токен цього користувача зі сповіщення TradingView."
                            )
                            await bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=admin_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Failed to process expired bot access for user {user_id}: {e}")

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


def get_coin_selection_keyboard():
    rows = []
    row = []
    for ticker in AVAILABLE_COINS:
        display = ticker.replace("USDT", "")
        row.append(InlineKeyboardButton(display, callback_data=f"coin_{ticker}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Список підключених людей", callback_data="admin_users_list")],
        [InlineKeyboardButton("👑 Надати VIP", callback_data="admin_grant_vip")],
        [InlineKeyboardButton("🤖 Надати доступ до бота", callback_data="admin_grant_bot")]
    ])


def calc_days_left(end_iso: str) -> int:
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
            "• 🛡️ Обов'язково дотримуйтесь суворого **ризик- та мані-менеджменту**.\n"
            "• ⚖️ Ми **не несемо відповідальності** за ваш баланс та фінансові результати.\n\n"
            "📜 **Правила спільноти:**\n"
            "• 🚫 Без спаму, флуду, реклами та реферальних посилань.\n"
            "• 🤝 Ввічливе спілкування, без мату та токсичності.\n\n"
            "👇 **Обери потрібну дію з меню нижче:**"
        )
    return (
        "👋 **Welcome to the Kerdos community!**\n\n"
        "I am **Mireya**, your personal assistant for the **Kerdos** trading system.\n\n"
        "🎁 **Special Offers & Bonuses:**\n"
        "• 🚀 **14-Day FREE Trial:** Every new user gets 2 weeks of free trial access to our Kerdos VIP Signals Group!\n"
        "• 👥 **\"Refer a Friend\" Program:** Bring a friend and get **+14 days of free VIP access**!\n\n"
        "💎 **Services & Pricing:**\n"
        "• 📊 **Kerdos VIP Signals Group:** **$20 / 30 days**\n"
        "• 🤖 **Personal Signal Bot Setup:** **$100 / 30 days**\n\n"
        "👇 **Choose an option from the menu below:**"
    )


def get_text_support_prompt(lang="ua"):
    if lang == "ua":
        return (
            "🛟 **СЛУЖБА ПІДТРИМКИ KERDOS**\n\n"
            "Ви виявили помилку, маєте запитання щодо підписки або потребуєте допомоги з налаштуванням?\n\n"
            "📝 **Будь ласка, опишіть вашу проблему нижче в одному повідомленні:**\n"
            "*(Ви також можете додати скріншот або фото помилки)*"
        )
    return (
        "🛟 **KERDOS SUPPORT HELPDESK**\n\n"
        "Did you encounter an issue or have questions about your subscription?\n\n"
        "📝 **Please describe your issue below in a single message:**"
    )


def get_text_vip_payment(lang="ua"):
    if lang == "ua":
        return (
            "💳 **Оплата підписки на VIP-групу Kerdos ($20 / 30 днів)**\n\n"
            "Для активації підписки перекажіть **20 USDT** на один із гаманців Binance нижче:\n\n"
            f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
            f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
            f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
            "📥 **ПІДТВЕРДЖЕННЯ ОПЛАТИ:**\n"
            "Після виконання переказу **надішліть квитанцію (фото, скріншот або хеш) сюди в чат**."
        )
    return (
        "💳 **Kerdos VIP Group Subscription ($20 / 30 days)**\n\n"
        "To activate your subscription, send **20 USDT** to one of the Binance wallets below:\n\n"
        f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
        f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
        f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
        "📥 **HOW TO CONFIRM PAYMENT:**\n"
        "Send your receipt directly into this chat."
    )


def get_text_bot_payment(lang="ua"):
    if lang == "ua":
        return (
            "🤖 **Підключення Kerdos Signal Bot ($100 / 30 днів)**\n\n"
            "Персональний бот для автоматичного виконання сигналів **Kerdos** на вашому акаунті OKX.\n\n"
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
        "Price: **$100 / 30 days**\n\n"
        f"🔸 **USDT (TRC20):**\n`{WALLET_USDT_TRC20}`\n\n"
        f"🔹 **USDT (BEP20 / BNB Chain):**\n`{WALLET_USDT_BEP20}`\n\n"
        f"🟣 **USDT (Solana):**\n`{WALLET_USDT_SOLANA}`\n\n"
        "Send your receipt into this chat."
    )


def get_text_services(lang="ua"):
    if lang == "ua":
        return (
            "💎 **Наші Послуги та Прайс (Kerdos)**\n\n"
            "📊 **VIP-група з сигналами:** **$20 / 30 днів**\n\n"
            "🤖 **Персональний Signal Bot:** **$100 / 30 днів**\n\n"
            "🎁 **Бонуси:**\n"
            "• **14 днів FREE** для нових користувачів!\n"
            "• **+14 днів** за кожного запрошеного друга!"
        )
    return (
        "💎 **Services & Pricing (Kerdos)**\n\n"
        "📊 **VIP Signals Group Access:** **$20 / 30 days**\n\n"
        "🤖 **Personal Signal Bot Setup:** **$100 / 30 days**"
    )


def get_text_rules(lang="ua"):
    if lang == "ua":
        return (
            "📜 **Правила спільноти Kerdos**\n\n"
            "🚫 **Без спаму та флуду:** Масові розсилки заборонені.\n"
            "❌ **Заборона реклами:** Реклама без дозволу заборонена.\n"
            "🤝 **Повага та етика:** Образи та токсичність неприпустимі.\n"
            "🛡️ **Без шахрайства:** Спроби скаму = бан."
        )
    return (
        "📜 **Kerdos Community Rules**\n\n"
        "🚫 No Spam / Flooding\n❌ No Unapproved Ads\n🤝 Be Respectful\n🛡️ No Fraud"
    )


def get_text_choose_coin(lang="ua"):
    if lang == "ua":
        return (
            "🪙 **Оберіть монету для Signal Bot**\n\n"
            "Ваш Signal Bot працює з обраною монетою. Оберіть торгову пару з переліку:"
        )
    return (
        "🪙 **Choose a coin for your Signal Bot**\n\n"
        "Select your preferred trading pair below:"
    )


def get_text_coin_selected(ticker: str, lang="ua"):
    display = ticker.replace("USDT", "")
    if lang == "ua":
        return (
            f"✅ **Монету обрано: {display}**\n\n"
            "Тепер, будь ласка, надайте ваш **Signal Token** з OKX.\n\n"
            "📥 **Надішліть ваш токен у цей чат у такому форматі:**\n"
            "`Token: ваш_signal_token_тут`"
        )
    return (
        f"✅ **Coin selected: {display}**\n\n"
        "Now send your OKX **Signal Token** using the format:\n"
        "`Token: your_signal_token_here`"
    )


def get_text_token_saved(lang="ua"):
    if lang == "ua":
        return "✅ **Signal Token отримано!** Адміністратор підключить його найближчим часом."
    return "✅ **Signal Token received!** The admin will integrate it shortly."


def get_text_token_invalid(lang="ua"):
    if lang == "ua":
        return "⚠️ **Токен занадто короткий.** Надішліть токен у форматі:\n`Token: ваш_signal_token_тут`"
    return "⚠️ **Invalid token.** Format required:\n`Token: your_signal_token_here`"


# --- РЕФЕРАЛЬНА ПРОГРАМА ТА ТРИАЛ ---

async def get_referral_text(user_id: int, bot_username: str, lang: str = "ua") -> str:
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND trial_used = 1", (user_id,)) as cursor:
            row = await cursor.fetchone()
            active_refs = row[0] if row else 0

    if lang == "ua":
        return (
            "👥 **Реферальна програма Kerdos «Приведи друга»**\n\n"
            "Отримуйте **+14 днів безкоштовного доступу** за кожного друга, який активує безкоштовний пробний період!\n\n"
            f"🔗 **Ваше посилання:**\n`{ref_link}`\n\n"
            f"📊 **Запрошено друзів (активували FREE):** {active_refs}"
        )
    return (
        "👥 **Kerdos Referral Program**\n\n"
        "Get **+14 days of free VIP access** per referred friend!\n\n"
        f"🔗 **Your link:**\n`{ref_link}`\n\n"
        f"📊 **Active referrals:** {active_refs}"
    )


async def handle_free_trial_request(user_id: int, username: str, lang: str = "ua"):
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=14)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT trial_used, referrer_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()

        if user and user[0] == 1:
            if lang == "ua":
                return "⚠️ **Ви вже використовували безкоштовний 14-денний період.**"
            return "⚠️ **You have already used your free trial.**"

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
                        f"🎁 Вам нараховано **+14 днів доступу** до Kerdos VIP!\n"
                        f"⏰ Новий термін дії: **{new_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                        if ref_lang == "ua" else
                        f"🥳 **Your friend (@{safe_username}) claimed their free trial!**\n\n"
                        f"🎁 You received **+14 days** of Kerdos VIP access!"
                    )
                    try:
                        await bot.send_message(chat_id=referrer_id, text=bonus_msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Failed to notify referrer {referrer_id}: {e}")

            if lang == "ua":
                return (
                    f"🎉 **Вам надано 14 днів безкоштовного доступу до Kerdos VIP!**\n\n"
                    f"🔗 **Ваше посилання для входу:**\n{invite_link.invite_link}\n\n"
                    f"⏰ Доступ активний до: **{trial_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                )
            return (
                f"🎉 **You have been granted 14 days of free VIP access!**\n\n"
                f"🔗 **Your invite link:**\n{invite_link.invite_link}"
            )
        except Exception as e:
            logger.error(f"Error creating invite link for user {user_id}: {e}")
            return "❌ Помилка при створенні посилання. Переконайся, що бот доданий у групу як адмін."


async def forward_token_to_admin(user_id: int, username: str, token: str):
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
        f"📋 **Token:**\n`{escape_md(token)}`"
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

            # Введення Signal Token
            if update.message.text and update.message.text.strip().lower().startswith("token:"):
                raw_token = update.message.text.strip().split(":", 1)[1].strip()

                if len(raw_token) < 10:
                    await bot.send_message(chat_id=chat_id, text=get_text_token_invalid(user_lang), parse_mode="Markdown")
                    return {"status": "ok"}

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE users SET signal_token = ? WHERE user_id = ?", (raw_token, user_id))
                    await db.commit()

                await forward_token_to_admin(user_id, username, raw_token)
                await bot.send_message(chat_id=chat_id, text=get_text_token_saved(user_lang), parse_mode="Markdown")
                return {"status": "ok"}

            # Команди
            if update.message.text and update.message.text.startswith("/"):
                text = update.message.text.strip()
                await set_awaiting_support(user_id, 0)

                if text.startswith("/start"):
                    args = text.split()
                    if len(args) > 1 and args[1].startswith("ref_"):
                        try:
                            ref_id = int(args[1].split("_")[1])
                            if ref_id != user_id:
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute("""
                                        INSERT INTO users (user_id, username, referrer_id, lang)
                                        VALUES (?, ?, ?, ?)
                                        ON CONFLICT(user_id) DO UPDATE SET
                                            referrer_id = COALESCE(users.referrer_id, excluded.referrer_id)
                                    """, (user_id, username, ref_id, user_lang))
                                    await db.commit()
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

                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("""
                                INSERT INTO users (user_id, status, sub_end)
                                VALUES (?, 'VIP', ?)
                                ON CONFLICT(user_id) DO UPDATE SET
                                    status = excluded.status,
                                    sub_end = excluded.sub_end
                            """, (target_user_id, new_end.isoformat()))
                            await db.commit()

                            async with db.execute("SELECT lang FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
                                row = await cursor.fetchone()
                                target_lang = row[0] if row and row[0] else "ua"

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
                        await bot.send_message(chat_id=chat_id, text="Формат: `/give_vip 123456789`", parse_mode="Markdown")
                    return {"status": "ok"}

                elif text.startswith("/give_bot") and ADMIN_TELEGRAM_ID and user_id == ADMIN_TELEGRAM_ID:
                    parts = text.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        target_user_id = int(parts[1])
                        now = datetime.now(timezone.utc)
                        new_end = now + timedelta(days=30)

                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("""
                                INSERT INTO users (user_id, status, bot_sub_end)
                                VALUES (?, 'BOT', ?)
                                ON CONFLICT(user_id) DO UPDATE SET
                                    status = excluded.status,
                                    bot_sub_end = excluded.bot_sub_end
                            """, (target_user_id, new_end.isoformat()))
                            await db.commit()

                            async with db.execute("SELECT lang, signal_token FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
                                row = await cursor.fetchone()
                                target_lang = row[0] if row and row[0] else "ua"
                                user_token = row[1] if row else None

                        try:
                            if user_token:
                                safe_token_snip = f"{escape_md(user_token[:6])}...{escape_md(user_token[-4:])}"
                                user_msg = (
                                    f"🎉 **Адміністратор надав вам доступ до Kerdos Signal Bot на 30 днів!**\n\n"
                                    f"🔑 **Токен:** `{safe_token_snip}`"
                                    if target_lang == "ua" else
                                    f"🎉 **Admin granted you Kerdos Signal Bot access for 30 days!**"
                                )
                                await bot.send_message(chat_id=target_user_id, text=user_msg, parse_mode="Markdown")
                                await forward_token_to_admin(target_user_id, "", user_token)
                            else:
                                intro_msg = "🎉 **Адміністратор надав вам доступ до Kerdos Signal Bot на 30 днів!**"
                                await bot.send_message(chat_id=target_user_id, text=intro_msg, parse_mode="Markdown")
                                await bot.send_message(
                                    chat_id=target_user_id,
                                    text=get_text_choose_coin(target_lang),
                                    reply_markup=get_coin_selection_keyboard(),
                                    parse_mode="Markdown"
                                )
                            notification_status = "📤 Користувачу надіслано сповіщення."
                        except Exception as e:
                            notification_status = f"⚠️ Доступ оновлено в БД: {e}"

                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Статус **Bot** на 30 днів надано користувачу `{target_user_id}`.\n\n{notification_status}",
                            parse_mode="Markdown"
                        )
                    else:
                        await bot.send_message(chat_id=chat_id, text="Формат: `/give_bot 123456789`", parse_mode="Markdown")
                    return {"status": "ok"}

            # ОБРОБКА ЗВЕРНЕННЯ В ПІДТРИМКУ (ВИПРАВЛЕНО ТУТ)
            if is_awaiting_support == 1 and ADMIN_TELEGRAM_ID and user_id != ADMIN_TELEGRAM_ID:
                await set_awaiting_support(user_id, 0)

                # Замість tg://user?id= використовуємо посилання на username якщо він є, або callback_data
                if username and username != "no_username":
                    user_link = f"https://t.me/{username}"
                    admin_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Написати користувачу", url=user_link)]
                    ])
                else:
                    admin_keyboard = None

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
                    "🚀 **Ваше звернення успішно передано адміністратору!**"
                    if user_lang == "ua" else
                    "🚀 **Your support request has been delivered to the admin!**"
                )
                await bot.send_message(chat_id=chat_id, text=confirm_text, reply_markup=get_back_keyboard(user_lang), parse_mode="Markdown")
                return {"status": "ok"}

            # ОБРОБКА КВИТАНЦІЙ ПРО ОПЛАТУ
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
                    reply_msg = "✅ **Вашу квитанцію отримано!** Адміністратор перевірить її найближчим часом." if user_lang == "ua" else "✅ **Receipt received!**"
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
                    reply_msg = "✅ **Вашу квитанцію отримано!** Адміністратор перевірить її найближчим часом." if user_lang == "ua" else "✅ **Receipt received!**"
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
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT status, trial_end, sub_end, bot_sub_end, signal_token, selected_coin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                        row = await cursor.fetchone()

                if not row:
                    sub_info = "У вас немає активних підписок." if user_lang == "ua" else "You have no active subscriptions."
                else:
                    status, t_end, s_end, b_end, token, selected_coin = row
                    status_label = status.upper() if status else "FREE"
                    sub_info = f"📊 **Статус:** `{status_label}`\n\n" if user_lang == "ua" else f"📊 **Status:** `{status_label}`\n\n"

                    if t_end:
                        days_left = calc_days_left(t_end)
                        sub_info += f"🎁 **Триал:** залишилось {days_left} дн.\n" if user_lang == "ua" else f"🎁 **Trial:** {days_left} days left\n"

                    if s_end:
                        days_left = calc_days_left(s_end)
                        sub_info += f"💎 **VIP-група:** залишилось {days_left} дн.\n" if user_lang == "ua" else f"💎 **VIP Group:** {days_left} days left\n"

                    if b_end:
                        days_left = calc_days_left(b_end)
                        sub_info += f"🤖 **Signal Bot:** залишилось {days_left} дн.\n" if user_lang == "ua" else f"🤖 **Signal Bot:** {days_left} days left\n"

                    if selected_coin:
                        sub_info += f"🪙 **Монета:** `{selected_coin}`\n"

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

            elif data.startswith("admin_"):
                if user_id != ADMIN_TELEGRAM_ID:
                    await query.answer("Немає доступу!", show_alert=True)
                    return {"status": "ok"}

                if data == "admin_users_list":
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("""
                            SELECT user_id, username, status, trial_end, sub_end, bot_sub_end
                            FROM users ORDER BY user_id DESC LIMIT 30
                        """) as cursor:
                            users = await cursor.fetchall()

                    if not users:
                        text = "📊 База користувачів порожня."
                    else:
                        text = "📊 *Останні користувачі:*\n\n"

                        for u_id, u_name, u_status, t_end, s_end, b_end in users:
                            u_name_disp = f"@{escape_md(u_name)}" if u_name and u_name != "no_username" else f"ID: `{u_id}`"
                            services = []

                            if t_end and calc_days_left(t_end) > 0:
                                services.append(f"⏳ Тріал: {calc_days_left(t_end)} дн.")
                            if s_end and calc_days_left(s_end) > 0:
                                services.append(f"💎 VIP: {calc_days_left(s_end)} дн.")
                            if b_end and calc_days_left(b_end) > 0:
                                services.append(f"🤖 Bot: {calc_days_left(b_end)} дн.")

                            services_str = " | ".join(services) if services else "немає активних послуг"
                            text += f"👤 {u_name_disp} — Status: `{u_status}`\n└ {services_str}\n\n"

                    await query.edit_message_text(text=text, reply_markup=get_admin_panel_keyboard(), parse_mode="Markdown")

                elif data == "admin_grant_vip":
                    await query.edit_message_text(
                        text="Для надання VIP, надішліть команду:\n`/give_vip USER_ID`",
                        reply_markup=get_admin_panel_keyboard(),
                        parse_mode="Markdown"
                    )

                elif data == "admin_grant_bot":
                    await query.edit_message_text(
                        text="Для надання доступу до Signal Bot, надішліть команду:\n`/give_bot USER_ID`",
                        reply_markup=get_admin_panel_keyboard(),
                        parse_mode="Markdown"
                    )

            # АДМІНСЬКІ ДІЇ ПІДТВЕРДЖЕННЯ
            elif data.startswith(("approve_vip_", "approve_bot_", "decline_")) and user_id == ADMIN_TELEGRAM_ID:
                action_type, target_user_id = data.rsplit("_", 1)
                target_user_id = int(target_user_id)
                target_lang = await get_user_lang(target_user_id)
                now = datetime.now(timezone.utc)
                new_end = now + timedelta(days=30)

                async with aiosqlite.connect(DB_PATH) as db:
                    if action_type == "approve_vip":
                        await db.execute("UPDATE users SET status = 'active', sub_end = ? WHERE user_id = ?", (new_end.isoformat(), target_user_id))
                        await db.commit()

                        invite_link = await bot.create_chat_invite_link(chat_id=TELEGRAM_CHANNEL_ID, member_limit=1) if TELEGRAM_CHANNEL_ID else None
                        link_str = invite_link.invite_link if invite_link else "Перевірте канал."

                        user_msg = f"🎉 **Оплату VIP-групи підтверджено!**\n\n🔗 Посилання: {link_str}" if target_lang == "ua" else f"🎉 **VIP approved!**\n\n🔗 Link: {link_str}"
                        await bot.send_message(chat_id=target_user_id, text=user_msg)
                        await query.edit_message_text(text=f"✅ VIP підтверджено для ID: `{target_user_id}`")

                    elif action_type == "approve_bot":
                        await db.execute("UPDATE users SET bot_sub_end = ? WHERE user_id = ?", (new_end.isoformat(), target_user_id))
                        await db.commit()

                        approve_intro = "🎉 **Оплату Kerdos Signal Bot підтверджено!**" if target_lang == "ua" else "🎉 **Kerdos Signal Bot approved!**"
                        await bot.send_message(chat_id=target_user_id, text=approve_intro, parse_mode="Markdown")
                        await bot.send_message(
                            chat_id=target_user_id,
                            text=get_text_choose_coin(target_lang),
                            reply_markup=get_coin_selection_keyboard(),
                            parse_mode="Markdown"
                        )
                        await query.edit_message_text(text=f"✅ Signal Bot підтверджено для ID: `{target_user_id}`")

                    elif action_type == "decline":
                        user_msg = "❌ **Вашу квитанцію відхилено.**" if target_lang == "ua" else "❌ **Receipt declined.**"
                        await bot.send_message(chat_id=target_user_id, text=user_msg)
                        await query.edit_message_text(text=f"❌ Оплату відхилено для ID: `{target_user_id}`")

    except Exception as e:
        logger.error(f"Error handling Telegram webhook: {e}")

    return {"status": "ok"}
