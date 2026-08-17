import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
import aiosqlite

logging.basicConfig(level=logging.INFO)

# --- ЗМІННІ ОТОЧЕННЯ (ENVIRONMENT VARIABLES) ---
BOT_TOKEN = os.getenv("NEW_BOT_TOKEN")
VIP_GROUP_ID = os.getenv("VIP_GROUP_ID")  # ID вашої закритої VIP-групи / каналу
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")  # ID публічного каналу
MAIN_BOT_LINK = os.getenv("MAIN_BOT_LINK", "https://t.me/Mireya_signals_bot")
DB_PATH = "trades.db"

bot = Bot(token=BOT_TOKEN)
router = Router()

admin_storage = {}

# --- БАЗА ДАНИХ ТА АКТИВНІ ТРЕЙДИ ---

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, action TEXT, price REAL, roi REAL, timestamp DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_trades (
                symbol TEXT PRIMARY KEY, entry_price REAL, direction TEXT, time TEXT
            )
        """)
        await db.commit()

async def save_active_trade(symbol: str, entry_price: float, direction: str, time_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO active_trades (symbol, entry_price, direction, time)
            VALUES (?, ?, ?, ?)
        ''', (symbol, entry_price, direction, time_str))
        await db.commit()

async def get_active_trade(symbol: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT entry_price, direction, time FROM active_trades WHERE symbol = ?', (symbol,)) as cursor:
            return await cursor.fetchone()

async def delete_active_trade(symbol: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM active_trades WHERE symbol = ?', (symbol,))
        await db.commit()

# --- КЛАВІАТУРА ДЛЯ ПУБЛІЧНОГО КАНАЛУ ---

def get_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚀 Try 14 Days Free", 
                url=f"{MAIN_BOT_LINK}?start=trig_public"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Pricing & VIP Benefits", 
                url=f"{MAIN_BOT_LINK}?start=pricing"
            )
        ]
    ])

# --- HTTP СЕРВЕР (HEALTH CHECK ТА TRADINGVIEW WEBHOOK) ---

async def handle_health_check(request):
    return web.Response(text="OK", status=200)

async def handle_tradingview_webhook(request):
    try:
        data = await request.json()
        raw_ticker = data.get("ticker", "UNKNOWN")
        ticker = str(raw_ticker).upper().replace(".P", "").replace("PERP", "").strip()
        
        raw_action = str(data.get("action", "")).lower().strip()
        price = float(data.get("price", 0.0))
        
        market_pos = str(data.get("strategy") or data.get("market_position") or "").lower().strip()
        comment = str(data.get("comment", "")).lower().strip()

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

        if is_close_signal:
            active_trade = await get_active_trade(ticker)
            if active_trade:
                entry_price, saved_direction, _ = active_trade
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

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO trades (ticker, action, price, timestamp) VALUES (?, ?, ?, ?)",
                (ticker, db_action, price, now.isoformat())
            )
            await db.commit()

        # НАДСИЛАННЯ СИГНАЛУ В ЗАКРИТУ VIP-ГРУПУ
        if VIP_GROUP_ID:
            signal_text = (
                f"⚡ **KERDOS VIP SIGNAL** ⚡\n\n"
                f"🪙 **Coin:** #{ticker}\n"
                f"🎯 **Action:** {action_label}\n"
                f"{price_block}\n"
                f"{roi_text}"
                f"⏰ **Time:** {now_str}"
            )
            await bot.send_message(
                chat_id=VIP_GROUP_ID,
                text=signal_text,
                parse_mode="Markdown"
            )

        return web.json_response({"status": "ok"})
    except Exception as e:
        logging.error(f"Error processing TradingView webhook: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_post("/webhook", handle_tradingview_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server running on port {port}")

# --- AIOGRAM ХЕНДЛЕРИ (ПУБЛІКАЦІЯ В ПУБЛІЧНИЙ КАНАЛ) ---

@router.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "👋 Welcome to **Corvin** — the official public channel manager!\n\n"
        "Here we publish reports, analytics, and results of our closed algorithms."
    )
    await message.answer(
        welcome_text, 
        reply_markup=get_channel_keyboard(), 
        parse_mode="Markdown"
    )

@router.message(F.photo & (F.chat.type == "private"))
async def handle_admin_screenshot(message: Message):
    file_id = message.photo[-1].file_id
    user_id = message.from_user.id
    
    admin_storage[user_id] = {
        "file_id": file_id,
        "caption": message.caption or "📊 Report from the closed Kerdos Community VIP group."
    }
    
    preview_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Publish to Public Channel", callback_data="confirm_pub")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_pub")]
    ])
    
    await message.answer_photo(
        photo=file_id,
        caption=f"<b>Preview:</b>\n\n{admin_storage[user_id]['caption']}",
        reply_markup=preview_keyboard,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "confirm_pub")
async def confirm_publishing(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in admin_storage:
        await callback.answer("Data expired, please send the photo again.", show_alert=True)
        return
    
    data = admin_storage[user_id]
    
    try:
        # НАДСИЛАННЯ В ПУБЛІЧНИЙ КАНАЛ
        await bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=data["file_id"],
            caption=f"📊 **Report from Kerdos Community VIP Group**\n\n{data['caption']}\n\nJoin our system!",
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(caption="✅ **Successfully published to public channel!**", parse_mode="Markdown")
    except Exception as e:
        await callback.answer(f"Publishing error: {e}", show_alert=True)
        
    del admin_storage[user_id]
    await callback.answer()

@router.callback_query(F.data == "cancel_pub")
async def cancel_publishing(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in admin_storage:
        del admin_storage[user_id]
    await callback.message.delete()
    await callback.answer("Cancelled.")

async def send_scheduled_report():
    report_text = (
        "📊 **Scheduled Kerdos Community Report**\n\n"
        "Algorithms continue to process the market in normal mode. "
        "Stay tuned for updates and join our closed signals.\n\n"
        "⚡️ Get access to the trading robot using the button below:"
    )
    try:
        # НАДСИЛАННЯ ПЛАНОВОГО ЗВІТУ В ПУБЛІЧНИЙ КАНАЛ
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=report_text,
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Scheduled report error: {e}")

async def main():
    await init_db()
    
    dp = Dispatcher()
    dp.include_router(router)
    
    await start_web_server()
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_scheduled_report, 'cron', hour=10, minute=0)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Public publisher & VIP Signal bot successfully started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
