import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("NEW_BOT_TOKEN")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
MAIN_BOT_LINK = os.getenv("MAIN_BOT_LINK", "https://t.me/Mireya_signals_bot")

bot = Bot(token=BOT_TOKEN)
router = Router()

admin_storage = {}

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

@router.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "👋 Welcome to **Kerdos News** — the official public channel of our community!\n\n"
        "Here we publish reports, analytics, and results of our closed algorithms.\n\n"
        "Want to join the VIP group and set up automated signals?"
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
        [InlineKeyboardButton(text="📢 Publish to Channel", callback_data="confirm_pub")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_pub")]
    ])
    
    await message.answer_photo(
        photo=file_id,
        caption=f"<b>Preview:</b>\n\n{admin_storage[user_id]['caption']}",
        reply_markup=preview_keyboard,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "confirm_pub")
async def confirm_publishing(callback: aiogram.types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in admin_storage:
        await callback.answer("Data expired, please send the photo again.", show_alert=True)
        return
    
    data = admin_storage[user_id]
    
    try:
        await bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=data["file_id"],
            caption=f"📊 **Report from Kerdos Community VIP Group**\n\n{data['caption']}\n\nJoin our system!",
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(caption="✅ **Successfully published to the channel!**", parse_mode="Markdown")
    except Exception as e:
        await callback.answer(f"Publishing error: {e}", show_alert=True)
        
    del admin_storage[user_id]
    await callback.answer()

@router.callback_query(F.data == "cancel_pub")
async def cancel_publishing(callback: aiogram.types.CallbackQuery):
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
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=report_text,
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Scheduled report error: {e}")

async def main():
    dp = Dispatcher()
    dp.include_router(router)
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_scheduled_report, 'cron', hour=10, minute=0)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Public publisher bot successfully started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
