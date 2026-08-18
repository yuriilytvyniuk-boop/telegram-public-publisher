import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import aiosqlite
import telegram
from fastapi import FastAPI, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

# =======================================================
# НАЛАШТУВАННЯ
# =======================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

PUBLIC_CHAT_LINK = os.getenv(
    "PUBLIC_CHAT_LINK",
    "https://t.me/kerdos_group"
)

# Render автоматично надає RENDER_EXTERNAL_URL
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

DB_PATH = "trades.db"

# =======================================================
# ГАМАНЦІ
# =======================================================

WALLET_USDT_TRC20 = "THeVYP6zqgJ3jKMhNAuBxqgK47iFno6pKL"
WALLET_USDT_BEP20 = "0x97eb6c4c2fe24798ccf24ed5d52cb228f32f5f5f"
WALLET_USDT_SOLANA = "5Pcc4WUfA1qBas6P42WDYRre8ugAenNe5UsN6c2DyUox"

# =======================================================
# МОНЕТИ
# =======================================================

AVAILABLE_COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "HYPEUSDT",
    "LINKUSDT",
    "ONDOUSDT",
    "JTOUSDT",
    "LTCUSDT",
    "APTUSDT",
    "DOTUSDT",
    "AVAXUSDT",
    "ATOMUSDT",
    "UNIUSDT",
    "FILUSDT",
    "AAVEUSDT",
    "XMRUSDT",
    "ETCUSDT",
    "VETUSDT",
    "GRTUSDT",
    "SANDUSDT",
    "MANAUSDT",
    "AXSUSDT",
    "THETAUSDT",
    "DASHUSDT",
]

# =======================================================
# BOT
# =======================================================

bot = (
    telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    if TELEGRAM_BOT_TOKEN
    else None
)

BOT_USERNAME = None


# =======================================================
# ESCAPE MARKDOWN
# =======================================================

def escape_md(text) -> str:
    if text is None:
        return ""

    text = str(text)

    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")

    return text


# =======================================================
# DATABASE
# =======================================================

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

        await db.commit()


# =======================================================
# USER FUNCTIONS
# =======================================================

async def ensure_user(
    user_id: int,
    username: str = None
):
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO users (
                user_id,
                username
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET username = excluded.username
        """, (
            user_id,
            username or "no_username"
        ))

        await db.commit()


async def get_user(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT
                user_id,
                username,
                trial_used,
                trial_start,
                trial_end,
                sub_end,
                bot_sub_end,
                signal_token,
                status,
                lang,
                referrer_id,
                awaiting_support,
                selected_coin
            FROM users
            WHERE user_id = ?
        """, (user_id,)) as cursor:

            return await cursor.fetchone()


async def get_user_lang(user_id: int):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT lang FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:

            row = await cursor.fetchone()

            if row and row[0]:
                return row[0]

    return "ua"


async def set_user_lang(user_id: int, lang: str):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO users (
                user_id,
                lang
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET lang = excluded.lang
        """, (
            user_id,
            lang
        ))

        await db.commit()


async def set_user_selected_coin(
    user_id: int,
    ticker: str
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO users (
                user_id,
                selected_coin
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET selected_coin = excluded.selected_coin
        """, (
            user_id,
            ticker
        ))

        await db.commit()


async def set_awaiting_support(
    user_id: int,
    state: int
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO users (
                user_id,
                awaiting_support
            )
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET awaiting_support = excluded.awaiting_support
        """, (
            user_id,
            state
        ))

        await db.commit()


async def save_signal_token(
    user_id: int,
    token: str
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            UPDATE users
            SET signal_token = ?
            WHERE user_id = ?
        """, (
            token,
            user_id
        ))

        await db.commit()


# =======================================================
# ROI
# =======================================================

async def get_all_coin_roi():

    result = {}

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute(
            "SELECT ticker, roi FROM coin_roi"
        ) as cursor:

            rows = await cursor.fetchall()

            for ticker, roi in rows:
                result[ticker] = roi

    return result


async def set_coin_roi(
    ticker: str,
    roi: float
):

    now = datetime.now(timezone.utc)

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT INTO coin_roi (
                ticker,
                roi,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(ticker)
            DO UPDATE SET
                roi = excluded.roi,
                updated_at = excluded.updated_at
        """, (
            ticker,
            roi,
            now.isoformat()
        ))

        await db.commit()


# =======================================================
# ACTIVE TRADES
# =======================================================

async def save_active_trade(
    symbol,
    entry_price,
    direction,
    time_str
):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            INSERT OR REPLACE INTO active_trades (
                symbol,
                entry_price,
                direction,
                time
            )
            VALUES (?, ?, ?, ?)
        """, (
            symbol,
            entry_price,
            direction,
            time_str
        ))

        await db.commit()


async def get_active_trade(symbol):

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT
                entry_price,
                direction,
                time
            FROM active_trades
            WHERE symbol = ?
        """, (symbol,)) as cursor:

            return await cursor.fetchone()


async def delete_active_trade(symbol):

    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute(
            "DELETE FROM active_trades WHERE symbol = ?",
            (symbol,)
        )

        await db.commit()


# =======================================================
# DAYS LEFT
# =======================================================

def calc_days_left(end_iso):

    if not end_iso:
        return 0

    try:

        end_dt = datetime.fromisoformat(end_iso)

        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(
                tzinfo=timezone.utc
            )

        now = datetime.now(timezone.utc)

        delta = end_dt - now

        if delta.total_seconds() <= 0:
            return 0

        return delta.days + (
            1 if delta.seconds > 0 else 0
        )

    except Exception:
        return 0


# =======================================================
# KEYBOARDS
# =======================================================

def get_main_keyboard(lang="ua"):

    if lang == "ua":

        keyboard = [

            [
                InlineKeyboardButton(
                    "⏳ Моя підписка",
                    callback_data="btn_my_sub"
                )
            ],

            [
                InlineKeyboardButton(
                    "🎁 Отримати 14 днів FREE",
                    callback_data="btn_free_trial"
                )
            ],

            [
                InlineKeyboardButton(
                    "👥 Реферальна програма",
                    callback_data="btn_referral"
                )
            ],

            [
                InlineKeyboardButton(
                    "📊 Доступ до VIP-групи ($20 / 30 днів)",
                    callback_data="btn_buy_group"
                )
            ],

            [
                InlineKeyboardButton(
                    "🤖 Підключити Signal Bot ($100 / 30 днів)",
                    callback_data="btn_connect_bot"
                )
            ],

            [
                InlineKeyboardButton(
                    "💎 Послуги та ціни",
                    callback_data="btn_services"
                )
            ],

            [
                InlineKeyboardButton(
                    "📜 Правила спільноти",
                    callback_data="btn_rules"
                )
            ],

            [
                InlineKeyboardButton(
                    "🛟 Підтримка / Допомога",
                    callback_data="btn_support"
                )
            ],

            [
                InlineKeyboardButton(
                    "💬 Чат спільноти",
                    url=PUBLIC_CHAT_LINK
                )
            ],

            [
                InlineKeyboardButton(
                    "🇬🇧 Switch to English",
                    callback_data="lang_en"
                )
            ]
        ]

    else:

        keyboard = [

            [
                InlineKeyboardButton(
                    "⏳ My Subscription",
                    callback_data="btn_my_sub"
                )
            ],

            [
                InlineKeyboardButton(
                    "🎁 Get 14-Day Free Trial",
                    callback_data="btn_free_trial"
                )
            ],

            [
                InlineKeyboardButton(
                    "👥 Referral Program",
                    callback_data="btn_referral"
                )
            ],

            [
                InlineKeyboardButton(
                    "📊 VIP Signals Group ($20 / 30 days)",
                    callback_data="btn_buy_group"
                )
            ],

            [
                InlineKeyboardButton(
                    "🤖 Connect Signal Bot ($100 / 30 days)",
                    callback_data="btn_connect_bot"
                )
            ],

            [
                InlineKeyboardButton(
                    "💎 Services & Pricing",
                    callback_data="btn_services"
                )
            ],

            [
                InlineKeyboardButton(
                    "📜 Community Rules",
                    callback_data="btn_rules"
                )
            ],

            [
                InlineKeyboardButton(
                    "🛟 Support / Help",
                    callback_data="btn_support"
                )
            ],

            [
                InlineKeyboardButton(
                    "💬 Community Chat",
                    url=PUBLIC_CHAT_LINK
                )
            ],

            [
                InlineKeyboardButton(
                    "🇺🇦 Переключити на Українську",
                    callback_data="lang_ua"
                )
            ]
        ]

    return InlineKeyboardMarkup(keyboard)


def get_back_keyboard(lang="ua"):

    text = (
        "🔙 Повернутися в меню"
        if lang == "ua"
        else
        "🔙 Back to Menu"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text,
                callback_data="btn_back_main"
            )
        ]
    ])


def get_cancel_support_keyboard(lang="ua"):

    text = (
        "❌ Скасувати звернення"
        if lang == "ua"
        else
        "❌ Cancel Support Request"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text,
                callback_data="btn_cancel_support"
            )
        ]
    ])


async def get_coin_selection_keyboard(lang="ua"):

    roi_map = await get_all_coin_roi()

    rows = []
    row = []

    for ticker in AVAILABLE_COINS:

        roi = roi_map.get(ticker)

        if roi is None:

            roi_label = (
                "н/д"
                if lang == "ua"
                else
                "N/A"
            )

        else:

            sign = "+" if roi >= 0 else ""

            roi_label = f"{sign}{roi:.1f}%"

        display = ticker.replace(
            "USDT",
            ""
        )

        button_text = (
            f"{display} ({roi_label})"
        )

        row.append(
            InlineKeyboardButton(
                button_text,
                callback_data=f"coin_{ticker}"
            )
        )

        if len(row) == 2:

            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([
        InlineKeyboardButton(
            "🔙 Назад" if lang == "ua"
            else "🔙 Back",
            callback_data="btn_back_main"
        )
    ])

    return InlineKeyboardMarkup(rows)


def get_admin_panel_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "👥 Список підключених людей",
                callback_data="admin_users_list"
            )
        ],

        [
            InlineKeyboardButton(
                "👑 Надати VIP",
                callback_data="admin_grant_vip"
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 Надати доступ до бота",
                callback_data="admin_grant_bot"
            )
        ],

        [
            InlineKeyboardButton(
                "📈 Оновити ROI монет",
                callback_data="admin_roi_info"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 Закрити",
                callback_data="btn_back_main"
            )
        ]
    ])


# =======================================================
# TEXTS
# =======================================================

def get_text_start(lang="ua"):

    if lang == "ua":

        return (
            "👋 **Вітаємо у спільноті Kerdos!**\n\n"

            "Я — **Mireya**, ваш персональний помічник "
            "аналітичної торгової системи **Kerdos**.\n\n"

            "🎁 **Спеціальні пропозиції та бонуси:**\n"
            "• 🚀 **14 днів FREE-доступу** для нових користувачів.\n"
            "• 👥 За кожного друга, який активує FREE-триал — "
            "**+14 днів безкоштовного доступу**.\n\n"

            "💎 **Наші послуги:**\n"
            "• 📊 VIP-група з сигналами — **$20 / 30 днів**\n"
            "• 🤖 Персональний Signal Bot — **$100 / 30 днів**\n\n"

            "⚠️ **Управління ризиками:**\n"
            "Криптовалютна торгівля пов'язана з високими ризиками. "
            "Використовуйте безпечний розмір позиції та плече.\n\n"

            "📜 **Правила:**\n"
            "• 🚫 Без спаму та реклами.\n"
            "• 🤝 Поважайте інших учасників.\n"
            "• 🛡️ Шахрайство = бан.\n\n"

            "👇 **Оберіть потрібну дію:**"
        )

    return (
        "👋 **Welcome to Kerdos!**\n\n"

        "I am **Mireya**, your personal assistant "
        "for the **Kerdos** trading system.\n\n"

        "🎁 **Special Offers:**\n"
        "• 🚀 **14-Day FREE Trial** for new users.\n"
        "• 👥 Refer a friend and receive **+14 days**.\n\n"

        "💎 **Services:**\n"
        "• 📊 VIP Signals Group — **$20 / 30 days**\n"
        "• 🤖 Personal Signal Bot — **$100 / 30 days**\n\n"

        "⚠️ Cryptocurrency trading involves financial risk.\n\n"

        "👇 **Choose an option:**"
    )


def get_text_support_prompt(lang="ua"):

    if lang == "ua":

        return (
            "🛟 **СЛУЖБА ПІДТРИМКИ KERDOS**\n\n"
            "Опишіть вашу проблему одним повідомленням.\n\n"
            "📷 Можна також прикріпити скріншот або фото.\n\n"
            "⏳ Mireya передасть звернення адміністратору."
        )

    return (
        "🛟 **KERDOS SUPPORT**\n\n"
        "Please describe your issue in one message.\n\n"
        "📷 You can also attach a screenshot.\n\n"
        "⏳ Mireya will forward your request to the administrator."
    )


def get_text_vip_payment(lang="ua"):

    if lang == "ua":

        return (
            "💳 **VIP-група Kerdos — $20 / 30 днів**\n\n"

            "Перекажіть **20 USDT** на один із гаманців:\n\n"

            f"🔸 **USDT TRC20:**\n"
            f"`{WALLET_USDT_TRC20}`\n\n"

            f"🔹 **USDT BEP20:**\n"
            f"`{WALLET_USDT_BEP20}`\n\n"

            f"🟣 **USDT Solana:**\n"
            f"`{WALLET_USDT_SOLANA}`\n\n"

            "📥 Після оплати надішліть сюди "
            "скріншот, фото квитанції або TxID."
        )

    return (
        "💳 **Kerdos VIP Group — $20 / 30 days**\n\n"

        "Send **20 USDT** to one of these wallets:\n\n"

        f"🔸 **USDT TRC20:**\n"
        f"`{WALLET_USDT_TRC20}`\n\n"

        f"🔹 **USDT BEP20:**\n"
        f"`{WALLET_USDT_BEP20}`\n\n"

        f"🟣 **USDT Solana:**\n"
        f"`{WALLET_USDT_SOLANA}`\n\n"

        "📥 After payment send the receipt or TxID here."
    )


def get_text_bot_payment(lang="ua"):

    if lang == "ua":

        return (
            "🤖 **Kerdos Signal Bot — $100 / 30 днів**\n\n"

            "Автоматичне виконання сигналів Kerdos "
            "на вашому OKX.\n\n"

            "⚡ **Переваги:**\n"
            "• Автоматична торгівля 24/7\n"
            "• Без передачі API-ключів\n"
            "• Signal Token OKX\n\n"

            "💳 **Вартість: $100 / 30 днів**\n\n"

            "Перекажіть **100 USDT**:\n\n"

            f"🔸 **TRC20:**\n"
            f"`{WALLET_USDT_TRC20}`\n\n"

            f"🔹 **BEP20:**\n"
            f"`{WALLET_USDT_BEP20}`\n\n"

            f"🟣 **Solana:**\n"
            f"`{WALLET_USDT_SOLANA}`\n\n"

            "📥 Після оплати надішліть квитанцію або TxID."
        )

    return (
        "🤖 **Kerdos Signal Bot — $100 / 30 days**\n\n"
        "Automated Kerdos signal execution on OKX.\n\n"
        "💳 **Price: $100 / 30 days**\n\n"

        "Send **100 USDT**:\n\n"

        f"🔸 **TRC20:**\n"
        f"`{WALLET_USDT_TRC20}`\n\n"

        f"🔹 **BEP20:**\n"
        f"`{WALLET_USDT_BEP20}`\n\n"

        f"🟣 **Solana:**\n"
        f"`{WALLET_USDT_SOLANA}`\n\n"

        "📥 Send your receipt or TxID here."
    )


def get_text_services(lang="ua"):

    if lang == "ua":

        return (
            "💎 **Послуги та ціни Kerdos**\n\n"

            "📊 **VIP-група:** $20 / 30 днів\n\n"

            "🤖 **Signal Bot:** $100 / 30 днів\n\n"

            "🎁 **Бонус:**\n"
            "14 днів FREE для нових користувачів."
        )

    return (
        "💎 **Kerdos Services & Pricing**\n\n"
        "📊 **VIP Group:** $20 / 30 days\n\n"
        "🤖 **Signal Bot:** $100 / 30 days\n\n"
        "🎁 **Bonus:**\n"
        "14-Day FREE Trial."
    )


def get_text_rules(lang="ua"):

    if lang == "ua":

        return (
            "📜 **Правила спільноти Kerdos**\n\n"
            "🚫 Без спаму та флуду.\n"
            "❌ Без реклами.\n"
            "🤝 Поважайте інших.\n"
            "🤬 Без токсичності та образ.\n"
            "🛡️ Шахрайство = бан."
        )

    return (
        "📜 **Kerdos Community Rules**\n\n"
        "🚫 No spam.\n"
        "❌ No advertising.\n"
        "🤝 Respect other members.\n"
        "🤬 No toxicity.\n"
        "🛡️ Scamming = permanent ban."
    )


def get_text_choose_coin(lang="ua"):

    if lang == "ua":

        return (
            "🪙 **Оберіть монету для Signal Bot**\n\n"
            "Signal Bot працює лише з **однією монетою**.\n\n"
            "Оберіть пару, для якої хочете отримувати "
            "автоматичні сигнали.\n\n"
            "У дужках показано ROI за минулий місяць."
        )

    return (
        "🪙 **Choose a coin for Signal Bot**\n\n"
        "Signal Bot works with **one coin only**.\n\n"
        "Choose the pair for automated signals.\n\n"
        "ROI from the previous month is shown in brackets."
    )


def get_text_coin_selected(
    ticker,
    lang="ua"
):

    display = ticker.replace(
        "USDT",
        ""
    )

    if lang == "ua":

        return (
            f"✅ **Монету обрано: {display}**\n\n"

            "Тепер надайте ваш **Signal Token**.\n\n"

            "📍 **Де знайти Signal Token в OKX:**\n"
            "1. Trade → Trading Bots.\n"
            "2. Signal Bot.\n"
            "3. Create Custom Signal.\n"
            "4. Скопіюйте Signal Token.\n\n"

            "📥 **Надішліть токен у форматі:**\n"
            "`Token: ваш_signal_token_тут`\n\n"

            "⏳ Після отримання токен буде передано адміністратору."
        )

    return (
        f"✅ **Coin selected: {display}**\n\n"

        "Now send your **Signal Token**.\n\n"

        "📍 **Where to find it:**\n"
        "1. Trade → Trading Bots.\n"
        "2. Signal Bot.\n"
        "3. Create Custom Signal.\n"
        "4. Copy Signal Token.\n\n"

        "📥 **Send it as:**\n"
        "`Token: your_signal_token_here`"
    )


# =======================================================
# REFERRAL
# =======================================================

async def get_referral_text(
    user_id,
    lang="ua"
):

    if not BOT_USERNAME:
        return (
            "❌ Реферальне посилання ще недоступне."
            if lang == "ua"
            else
            "❌ Referral link is not available yet."
        )

    ref_link = (
        f"https://t.me/{BOT_USERNAME}"
        f"?start=ref_{user_id}"
    )

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT COUNT(*)
            FROM users
            WHERE referrer_id = ?
            AND trial_used = 1
        """, (user_id,)) as cursor:

            row = await cursor.fetchone()

    count = row[0] if row else 0

    if lang == "ua":

        return (
            "👥 **Реферальна програма Kerdos**\n\n"

            "Запрошуйте друзів та отримуйте "
            "**+14 днів безкоштовного доступу** "
            "за кожного друга, який активує FREE-триал.\n\n"

            f"🔗 **Ваше посилання:**\n"
            f"`{ref_link}`\n\n"

            f"📊 **Запрошених друзів:** {count}"
        )

    return (
        "👥 **Kerdos Referral Program**\n\n"

        "Invite friends and receive "
        "**+14 free days** for every friend "
        "who activates the free trial.\n\n"

        f"🔗 **Your link:**\n"
        f"`{ref_link}`\n\n"

        f"📊 **Friends:** {count}"
    )


# =======================================================
# FREE TRIAL
# =======================================================

async def handle_free_trial_request(
    user_id,
    username,
    lang="ua"
):

    now = datetime.now(timezone.utc)

    trial_end = now + timedelta(days=14)

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT
                trial_used,
                referrer_id
            FROM users
            WHERE user_id = ?
        """, (user_id,)) as cursor:

            user = await cursor.fetchone()

        if user and user[0] == 1:

            return (
                "⚠️ **Ви вже використовували FREE-триал.**"
                if lang == "ua"
                else
                "⚠️ **You already used your FREE trial.**"
            )

        referrer_id = (
            user[1]
            if user
            else None
        )

        if not TELEGRAM_CHANNEL_ID:

            return (
                "❌ TELEGRAM_CHANNEL_ID не налаштований."
            )

        try:

            invite_link = await bot.create_chat_invite_link(

                chat_id=TELEGRAM_CHANNEL_ID,

                member_limit=1,

                expire_date=int(
                    (
                        now + timedelta(hours=24)
                    ).timestamp()
                )
            )

            await db.execute("""
                INSERT INTO users (
                    user_id,
                    username,
                    trial_used,
                    trial_start,
                    trial_end,
                    status,
                    lang
                )
                VALUES (?, ?, 1, ?, ?, 'trial', ?)

                ON CONFLICT(user_id)
                DO UPDATE SET
                    username = excluded.username,
                    trial_used = 1,
                    trial_start = excluded.trial_start,
                    trial_end = excluded.trial_end,
                    status = 'trial',
                    lang = excluded.lang
            """, (
                user_id,
                username or "no_username",
                now.isoformat(),
                trial_end.isoformat(),
                lang
            ))

            await db.commit()

            # REFERRAL BONUS

            if referrer_id:

                async with db.execute("""
                    SELECT
                        trial_end,
                        sub_end,
                        status,
                        lang
                    FROM users
                    WHERE user_id = ?
                """, (
                    referrer_id,
                )) as cursor:

                    ref_user = await cursor.fetchone()

                if ref_user:

                    ref_trial_end = ref_user[0]
                    ref_sub_end = ref_user[1]
                    ref_status = ref_user[2]
                    ref_lang = ref_user[3] or "ua"

                    if (
                        ref_status == "active"
                        and ref_sub_end
                    ):

                        curr_end = datetime.fromisoformat(
                            ref_sub_end
                        )

                        if curr_end.tzinfo is None:
                            curr_end = curr_end.replace(
                                tzinfo=timezone.utc
                            )

                        base_time = max(
                            now,
                            curr_end
                        )

                        new_end = (
                            base_time
                            + timedelta(days=14)
                        )

                        await db.execute("""
                            UPDATE users
                            SET sub_end = ?
                            WHERE user_id = ?
                        """, (
                            new_end.isoformat(),
                            referrer_id
                        ))

                    else:

                        curr_end = None

                        if ref_trial_end:

                            curr_end = datetime.fromisoformat(
                                ref_trial_end
                            )

                            if curr_end.tzinfo is None:
                                curr_end = curr_end.replace(
                                    tzinfo=timezone.utc
                                )

                        base_time = (
                            max(now, curr_end)
                            if curr_end
                            else now
                        )

                        new_end = (
                            base_time
                            + timedelta(days=14)
                        )

                        await db.execute("""
                            UPDATE users
                            SET
                                trial_end = ?,
                                status = 'trial'
                            WHERE user_id = ?
                        """, (
                            new_end.isoformat(),
                            referrer_id
                        ))

                    await db.commit()

                    try:

                        await bot.send_message(

                            chat_id=referrer_id,

                            text=(
                                "🥳 **Ваш друг взяв FREE-триал!**\n\n"
                                "🎁 Вам нараховано **+14 днів** "
                                "безкоштовного доступу до Kerdos VIP!"
                            ),

                            parse_mode="Markdown"
                        )

                    except Exception as e:

                        logger.error(
                            f"Referral notification error: {e}"
                        )

            if lang == "ua":

                return (
                    "🎉 **Вам надано 14 днів FREE-доступу!**\n\n"

                    f"🔗 **Посилання на VIP-групу:**\n"
                    f"{invite_link.invite_link}\n\n"

                    f"⏰ Доступ до: "
                    f"**{trial_end.strftime('%Y-%m-%d %H:%M UTC')}**"
                )

            return (
                "🎉 **You received 14 days FREE access!**\n\n"

                f"🔗 **VIP group invite:**\n"
                f"{invite_link.invite_link}\n\n"

                f"⏰ Until: "
                f"**{trial_end.strftime('%Y-%m-%d %H:%M UTC')}**"
            )

        except Exception as e:

            logger.exception(
                f"Trial error for {user_id}"
            )

            return (
                "❌ Помилка створення запрошення.\n"
                "Переконайтеся, що бот є адміністратором VIP-групи."
            )


# =======================================================
# SIGNAL TOKEN → ADMIN
# =======================================================

async def forward_token_to_admin(
    user_id,
    username,
    token
):

    if not ADMIN_TELEGRAM_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT selected_coin
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )) as cursor:

            row = await cursor.fetchone()

    selected_coin = (
        row[0]
        if row and row[0]
        else "не обрано"
    )

    user_disp = (
        f"@{escape_md(username)}"
        if username and username != "no_username"
        else
        f"ID: {user_id}"
    )

    admin_text = (
        "🔑 **НОВИЙ SIGNAL TOKEN**\n\n"

        f"👤 **Користувач:** {user_disp}\n"
        f"🆔 **ID:** `{user_id}`\n"
        f"🪙 **Монета:** `{selected_coin}`\n\n"

        "📋 **Signal Token:**\n"
        f"`{escape_md(token)}`\n\n"

        f"➡️ Додайте цей токен в Alert Message "
        f"TradingView для `{selected_coin}`."
    )

    try:

        await bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=admin_text,
            parse_mode="Markdown"
        )

    except Exception as e:

        logger.error(
            f"Token forwarding error: {e}"
        )


# =======================================================
# MY SUBSCRIPTION
# =======================================================

async def get_subscription_text(
    user_id,
    lang
):

    user = await get_user(user_id)

    if not user:

        return (
            "❌ Дані користувача не знайдено."
        )

    (
        _user_id,
        username,
        trial_used,
        trial_start,
        trial_end,
        sub_end,
        bot_sub_end,
        signal_token,
        status,
        _lang,
        referrer_id,
        awaiting_support,
        selected_coin
    ) = user

    if lang == "ua":

        text = "⏳ **Моя підписка Kerdos**\n\n"

        if status == "trial" and trial_end:

            text += (
                "🎁 **FREE Trial**\n"
                f"Залишилось: **{calc_days_left(trial_end)} днів**\n"
                f"До: `{trial_end}`\n\n"
            )

        elif status == "active" and sub_end:

            text += (
                "📊 **VIP-доступ**\n"
                f"Залишилось: **{calc_days_left(sub_end)} днів**\n"
                f"До: `{sub_end}`\n\n"
            )

        else:

            text += (
                "📊 **VIP-доступ:** ❌ Неактивний\n\n"
            )

        if bot_sub_end:

            text += (
                "🤖 **Signal Bot**\n"
                f"Залишилось: **{calc_days_left(bot_sub_end)} днів**\n"
                f"До: `{bot_sub_end}`\n"
            )

        else:

            text += (
                "🤖 **Signal Bot:** ❌ Неактивний\n"
            )

        if selected_coin:

            text += (
                f"\n🪙 Монета: **{selected_coin}**"
            )

        return text

    else:

        text = "⏳ **My Kerdos Subscription**\n\n"

        if status == "trial" and trial_end:

            text += (
                "🎁 **FREE Trial**\n"
                f"Remaining: **{calc_days_left(trial_end)} days**\n\n"
            )

        elif status == "active" and sub_end:

            text += (
                "📊 **VIP Access**\n"
                f"Remaining: **{calc_days_left(sub_end)} days**\n\n"
            )

        else:

            text += (
                "📊 **VIP Access:** ❌ Inactive\n\n"
            )

        if bot_sub_end:

            text += (
                "🤖 **Signal Bot**\n"
                f"Remaining: **{calc_days_left(bot_sub_end)} days**"
            )

        else:

            text += (
                "🤖 **Signal Bot:** ❌ Inactive"
            )

        return text


# =======================================================
# START
# =======================================================

async def handle_start(update: Update):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    username = (
        user.username
        if user.username
        else
        "no_username"
    )

    await ensure_user(
        user.id,
        username
    )

    # REFERRAL

    text = update.message.text or ""

    if text.startswith("/start"):

        parts = text.split()

        if len(parts) > 1:

            payload = parts[1]

            if payload.startswith("ref_"):

                try:

                    referrer_id = int(
                        payload.replace(
                            "ref_",
                            ""
                        )
                    )

                    if referrer_id != user.id:

                        async with aiosqlite.connect(DB_PATH) as db:

                            await db.execute("""
                                UPDATE users
                                SET referrer_id = ?
                                WHERE user_id = ?
                                AND referrer_id IS NULL
                            """, (
                                referrer_id,
                                user.id
                            ))

                            await db.commit()

                except Exception as e:

                    logger.error(
                        f"Referral parse error: {e}"
                    )

    lang = await get_user_lang(
        user.id
    )

    await update.message.reply_text(

        get_text_start(lang),

        reply_markup=get_main_keyboard(lang),

        parse_mode="Markdown"
    )


# =======================================================
# MESSAGE HANDLER
# =======================================================

async def handle_message(update: Update):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    await ensure_user(
        user.id,
        user.username or "no_username"
    )

    text = update.message.text or ""

    # /start

    if text.startswith("/start"):

        await handle_start(update)
        return

    # ADMIN COMMAND

    if (
        text == "/admin"
        and user.id == ADMIN_TELEGRAM_ID
    ):

        await update.message.reply_text(
            "👑 **Адмін-панель Kerdos**",
            reply_markup=get_admin_panel_keyboard(),
            parse_mode="Markdown"
        )

        return

    # SUPPORT

    awaiting = 0

    async with aiosqlite.connect(DB_PATH) as db:

        async with db.execute("""
            SELECT awaiting_support
            FROM users
            WHERE user_id = ?
        """, (
            user.id,
        )) as cursor:

            row = await cursor.fetchone()

            if row:
                awaiting = row[0] or 0

    if awaiting == 1:

        lang = await get_user_lang(
            user.id
        )

        await set_awaiting_support(
            user.id,
            0
        )

        if ADMIN_TELEGRAM_ID:

            user_disp = (
                f"@{escape_md(user.username)}"
                if user.username
                else
                f"ID: {user.id}"
            )

            admin_text = (
                "🛟 **НОВЕ ЗВЕРНЕННЯ В ПІДТРИМКУ**\n\n"
                f"👤 **Користувач:** {user_disp}\n"
                f"🆔 **ID:** `{user.id}`\n\n"
                "💬 **Повідомлення:**\n"
                f"{escape_md(text)}"
            )

            try:

                await bot.send_message(
                    chat_id=ADMIN_TELEGRAM_ID,
                    text=admin_text,
                    parse_mode="Markdown"
                )

            except Exception as e:

                logger.error(
                    f"Support forwarding error: {e}"
                )

        await update.message.reply_text(

            (
                "✅ **Ваше звернення передано адміністратору.**"
                if lang == "ua"
                else
                "✅ **Your request was forwarded to the administrator.**"
            ),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # TOKEN

    if text.lower().startswith("token:"):

        token = text.split(
            ":",
            1
        )[1].strip()

        lang = await get_user_lang(
            user.id
        )

        if len(token) < 10:

            await update.message.reply_text(

                (
                    "⚠️ **Токен занадто короткий.**\n\n"
                    "Надішліть повний Signal Token."
                    if lang == "ua"
                    else
                    "⚠️ **Token is too short.**\n\n"
                    "Send the complete Signal Token."
                ),

                parse_mode="Markdown"
            )

            return

        await save_signal_token(
            user.id,
            token
        )

        await forward_token_to_admin(
            user.id,
            user.username or "no_username",
            token
        )

        await update.message.reply_text(

            (
                "✅ **Signal Token отримано!**\n\n"
                "Токен передано адміністратору.\n"
                "Налаштування буде завершено вручну."
                if lang == "ua"
                else
                "✅ **Signal Token received!**\n\n"
                "The token was forwarded to the administrator."
            ),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return


# =======================================================
# CALLBACKS
# =======================================================

async def handle_callback(update: Update):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    await ensure_user(
        user.id,
        user.username or "no_username"
    )

    lang = await get_user_lang(
        user.id
    )

    data = query.data

    # ---------------------------------------------------
    # LANGUAGE
    # ---------------------------------------------------

    if data == "lang_en":

        await set_user_lang(
            user.id,
            "en"
        )

        await query.edit_message_text(

            get_text_start("en"),

            reply_markup=get_main_keyboard("en"),

            parse_mode="Markdown"
        )

        return

    if data == "lang_ua":

        await set_user_lang(
            user.id,
            "ua"
        )

        await query.edit_message_text(

            get_text_start("ua"),

            reply_markup=get_main_keyboard("ua"),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # BACK
    # ---------------------------------------------------

    if data == "btn_back_main":

        await query.edit_message_text(

            get_text_start(lang),

            reply_markup=get_main_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # MY SUB
    # ---------------------------------------------------

    if data == "btn_my_sub":

        text = await get_subscription_text(
            user.id,
            lang
        )

        await query.edit_message_text(

            text,

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # FREE TRIAL
    # ---------------------------------------------------

    if data == "btn_free_trial":

        result = await handle_free_trial_request(

            user.id,

            user.username or "no_username",

            lang
        )

        await query.edit_message_text(

            result,

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # REFERRAL
    # ---------------------------------------------------

    if data == "btn_referral":

        text = await get_referral_text(
            user.id,
            lang
        )

        await query.edit_message_text(

            text,

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # VIP
    # ---------------------------------------------------

    if data == "btn_buy_group":

        await query.edit_message_text(

            get_text_vip_payment(lang),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # BOT
    # ---------------------------------------------------

    if data == "btn_connect_bot":

        await query.edit_message_text(

            get_text_bot_payment(lang),

            reply_markup=InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(
                        (
                            "🪙 Обрати монету"
                            if lang == "ua"
                            else
                            "🪙 Choose Coin"
                        ),
                        callback_data="btn_choose_coin"
                    )
                ],

                [
                    InlineKeyboardButton(
                        (
                            "🔙 Назад"
                            if lang == "ua"
                            else
                            "🔙 Back"
                        ),
                        callback_data="btn_back_main"
                    )
                ]

            ]),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # CHOOSE COIN
    # ---------------------------------------------------

    if data == "btn_choose_coin":

        keyboard = await get_coin_selection_keyboard(
            lang
        )

        await query.edit_message_text(

            get_text_choose_coin(lang),

            reply_markup=keyboard,

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # COIN
    # ---------------------------------------------------

    if data.startswith("coin_"):

        ticker = data.replace(
            "coin_",
            "",
            1
        )

        if ticker not in AVAILABLE_COINS:

            return

        await set_user_selected_coin(
            user.id,
            ticker
        )

        await query.edit_message_text(

            get_text_coin_selected(
                ticker,
                lang
            ),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # SERVICES
    # ---------------------------------------------------

    if data == "btn_services":

        await query.edit_message_text(

            get_text_services(lang),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # RULES
    # ---------------------------------------------------

    if data == "btn_rules":

        await query.edit_message_text(

            get_text_rules(lang),

            reply_markup=get_back_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # SUPPORT
    # ---------------------------------------------------

    if data == "btn_support":

        await set_awaiting_support(
            user.id,
            1
        )

        await query.edit_message_text(

            get_text_support_prompt(lang),

            reply_markup=get_cancel_support_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ---------------------------------------------------
    # CANCEL SUPPORT
    # ---------------------------------------------------

    if data == "btn_cancel_support":

        await set_awaiting_support(
            user.id,
            0
        )

        await query.edit_message_text(

            get_text_start(lang),

            reply_markup=get_main_keyboard(lang),

            parse_mode="Markdown"
        )

        return

    # ===================================================
    # ADMIN
    # ===================================================

    if user.id != ADMIN_TELEGRAM_ID:

        return

    if data == "admin_users_list":

        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("""
                SELECT
                    user_id,
                    username,
                    status,
                    selected_coin,
                    bot_sub_end
                FROM users
                ORDER BY user_id DESC
                LIMIT 50
            """) as cursor:

                rows = await cursor.fetchall()

        if not rows:

            text = "👥 Користувачів поки немає."

        else:

            text = (
                "👥 **ОСТАННІ 50 КОРИСТУВАЧІВ**\n\n"
            )

            for row in rows:

                uid, username, status, coin, bot_end = row

                name = (
                    f"@{username}"
                    if username and username != "no_username"
                    else
                    str(uid)
                )

                text += (
                    f"👤 `{name}`\n"
                    f"🆔 `{uid}`\n"
                    f"📊 `{status}`\n"
                    f"🪙 `{coin or '-'}`\n"
                    f"🤖 `{bot_end or '-'}`\n\n"
                )

        await query.edit_message_text(

            text,

            reply_markup=get_admin_panel_keyboard(),

            parse_mode="Markdown"
        )

        return

    if data == "admin_roi_info":

        roi = await get_all_coin_roi()

        if not roi:

            text = (
                "📈 ROI ще не додано в базу."
            )

        else:

            text = "📈 **ROI MONET**\n\n"

            for ticker in AVAILABLE_COINS:

                if ticker in roi:

                    text += (
                        f"{ticker}: "
                        f"**{roi[ticker]:+.1f}%**\n"
                    )

        await query.edit_message_text(

            text,

            reply_markup=get_admin_panel_keyboard(),

            parse_mode="Markdown"
        )

        return

    if data == "admin_grant_vip":

        await query.edit_message_text(

            "👑 **Надання VIP вручну**\n\n"
            "Цю функцію краще виконувати через окрему "
            "адмін-команду або форму.",

            reply_markup=get_admin_panel_keyboard(),

            parse_mode="Markdown"
        )

        return

    if data == "admin_grant_bot":

        await query.edit_message_text(

            "🤖 **Надання Signal Bot вручну**\n\n"
            "Цю функцію краще виконувати через окрему "
            "адмін-команду або форму.",

            reply_markup=get_admin_panel_keyboard(),

            parse_mode="Markdown"
        )

        return


# =======================================================
# UPDATE PROCESSOR
# =======================================================

async def process_update(update: Update):

    try:

        if update.callback_query:

            await handle_callback(
                update
            )

        elif update.message:

            await handle_message(
                update
            )

    except Exception as e:

        logger.exception(
            f"Update processing error: {e}"
        )


# =======================================================
# TELEGRAM WEBHOOK
# =======================================================

@app.post("/webhook")
async def telegram_webhook(
    request: Request
):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            bot
        )

        await process_update(
            update
        )

        return {
            "ok": True
        }

    except Exception as e:

        logger.exception(
            f"Webhook error: {e}"
        )

        return {
            "ok": False,
            "error": str(e)
        }


# =======================================================
# HEALTH CHECK
# =======================================================

@app.get("/")
async def root():

    return {
        "status": "live",
        "telegram_bot": bool(bot),
        "webhook_url": (
            f"{RENDER_EXTERNAL_URL}/webhook"
            if RENDER_EXTERNAL_URL
            else None
        )
    }


@app.get("/health")
async def health():

    return {
        "status": "ok"
    }


# =======================================================
# EXPIRED SUBSCRIPTIONS
# =======================================================

async def check_expired_trials():

    while True:

        try:

            await asyncio.sleep(3600)

            now = datetime.now(timezone.utc)

            async with aiosqlite.connect(DB_PATH) as db:

                # -------------------------------
                # TRIAL
                # -------------------------------

                async with db.execute("""
                    SELECT
                        user_id,
                        username,
                        lang
                    FROM users
                    WHERE status = 'trial'
                    AND trial_end IS NOT NULL
                    AND trial_end <= ?
                """, (
                    now.isoformat(),
                )) as cursor:

                    expired_trials = await cursor.fetchall()

                for (
                    user_id,
                    username,
                    lang
                ) in expired_trials:

                    try:

                        if TELEGRAM_CHANNEL_ID:

                            await bot.ban_chat_member(
                                chat_id=TELEGRAM_CHANNEL_ID,
                                user_id=user_id
                            )

                            await bot.unban_chat_member(
                                chat_id=TELEGRAM_CHANNEL_ID,
                                user_id=user_id
                            )

                        await db.execute("""
                            UPDATE users
                            SET status = 'expired'
                            WHERE user_id = ?
                        """, (
                            user_id,
                        ))

                        await db.commit()

                        await bot.send_message(

                            chat_id=user_id,

                            text=(
                                "⏳ **Ваш 14-денний FREE-триал завершився.**\n\n"
                                "Щоб продовжити користуватися Kerdos, "
                                "оформіть підписку."
                            ),

                            reply_markup=get_main_keyboard(
                                lang or "ua"
                            ),

                            parse_mode="Markdown"
                        )

                    except Exception as e:

                        logger.error(
                            f"Trial expiration error: {e}"
                        )

                # -------------------------------
                # VIP SUBSCRIPTION
                # -------------------------------

                async with db.execute("""
                    SELECT
                        user_id,
                        username,
                        lang
                    FROM users
                    WHERE status = 'active'
                    AND sub_end IS NOT NULL
                    AND sub_end <= ?
                """, (
                    now.isoformat(),
                )) as cursor:

                    expired_subs = await cursor.fetchall()

                for (
                    user_id,
                    username,
                    lang
                ) in expired_subs:

                    try:

                        if TELEGRAM_CHANNEL_ID:

                            await bot.ban_chat_member(
                                chat_id=TELEGRAM_CHANNEL_ID,
                                user_id=user_id
                            )

                            await bot.unban_chat_member(
                                chat_id=TELEGRAM_CHANNEL_ID,
                                user_id=user_id
                            )

                        await db.execute("""
                            UPDATE users
                            SET status = 'expired'
                            WHERE user_id = ?
                        """, (
                            user_id,
                        ))

                        await db.commit()

                        await bot.send_message(

                            chat_id=user_id,

                            text=(
                                "⏳ **Термін VIP-підписки завершився.**\n\n"
                                "Оформіть підписку повторно через меню."
                            ),

                            reply_markup=get_main_keyboard(
                                lang or "ua"
                            ),

                            parse_mode="Markdown"
                        )

                    except Exception as e:

                        logger.error(
                            f"VIP expiration error: {e}"
                        )

                # -------------------------------
                # SIGNAL BOT
                # -------------------------------

                async with db.execute("""
                    SELECT
                        user_id,
                        username,
                        lang,
                        selected_coin,
                        signal_token
                    FROM users
                    WHERE bot_sub_end IS NOT NULL
                    AND bot_sub_end <= ?
                """, (
                    now.isoformat(),
                )) as cursor:

                    expired_bots = await cursor.fetchall()

                for (
                    user_id,
                    username,
                    lang,
                    selected_coin,
                    signal_token
                ) in expired_bots:

                    try:

                        await db.execute("""
                            UPDATE users
                            SET bot_sub_end = NULL
                            WHERE user_id = ?
                        """, (
                            user_id,
                        ))

                        await db.commit()

                        await bot.send_message(

                            chat_id=user_id,

                            text=(
                                "⏳ **Термін Signal Bot завершився.**\n\n"
                                "Автоматичні сигнали для вашого OKX "
                                "більше не надсилаються.\n\n"
                                "Щоб продовжити, поновіть підписку."
                            ),

                            reply_markup=get_main_keyboard(
                                lang or "ua"
                            ),

                            parse_mode="Markdown"
                        )

                        if ADMIN_TELEGRAM_ID:

                            token_short = "немає"

                            if signal_token:

                                token_short = (
                                    f"`{escape_md(signal_token[:6])}"
                                    f"..."
                                    f"{escape_md(signal_token[-4:])}`"
                                )

                            admin_text = (
                                "⏰ **SIGNAL BOT ЗАВЕРШИВСЯ**\n\n"

                                f"👤 Username: "
                                f"`{escape_md(username or '-')}`\n"

                                f"🆔 ID: `{user_id}`\n"

                                f"🪙 Coin: "
                                f"`{selected_coin or '-'}`\n"

                                f"🔑 Token: {token_short}\n\n"

                                "➡️ Перевірте та видаліть токен "
                                "користувача з TradingView Alert, "
                                "якщо він не продовжив підписку."
                            )

                            await bot.send_message(

                                chat_id=ADMIN_TELEGRAM_ID,

                                text=admin_text,

                                parse_mode="Markdown"
                            )

                    except Exception as e:

                        logger.error(
                            f"Bot expiration error: {e}"
                        )

        except asyncio.CancelledError:

            raise

        except Exception as e:

            logger.exception(
                f"Expiration loop error: {e}"
            )


# =======================================================
# LIFESPAN
# =======================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    global BOT_USERNAME

    await init_db()

    if not bot:

        logger.error(
            "TELEGRAM_BOT_TOKEN не встановлений!"
        )

    else:

        try:

            me = await bot.get_me()

            BOT_USERNAME = me.username

            logger.info(
                f"Telegram bot: @{BOT_USERNAME}"
            )

            # -------------------------------------------
            # WEBHOOK
            # -------------------------------------------

            if not RENDER_EXTERNAL_URL:

                logger.error(
                    "RENDER_EXTERNAL_URL не встановлений!"
                )

            else:

                webhook_url = (
                    f"{RENDER_EXTERNAL_URL.rstrip('/')}"
                    f"/webhook"
                )

                await bot.set_webhook(
                    url=webhook_url
                )

                logger.info(
                    f"Telegram webhook встановлено: "
                    f"{webhook_url}"
                )

                info = await bot.get_webhook_info()

                logger.info(
                    f"Webhook info: "
                    f"url={info.url}, "
                    f"pending={info.pending_update_count}, "
                    f"error={info.last_error_message}"
                )

        except Exception as e:

            logger.exception(
                f"Помилка запуску Telegram: {e}"
            )

    bg_task = asyncio.create_task(
        check_expired_trials()
    )

    try:

        yield

    finally:

        bg_task.cancel()

        try:

            await bg_task

        except asyncio.CancelledError:

            pass

        if bot:

            try:

                await bot.delete_webhook()

                logger.info(
                    "Telegram webhook видалено"
                )

            except Exception as e:

                logger.error(
                    f"Webhook delete error: {e}"
                )


# =======================================================
# FASTAPI
# =======================================================

app = FastAPI(
    lifespan=lifespan
)
