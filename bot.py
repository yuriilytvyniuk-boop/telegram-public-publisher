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

# Тимчасове сховище для file_id скріншотів адміна
admin_storage = {}

def get_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚀 Спробувати 14 днів безкоштовно", 
                url=f"{MAIN_BOT_LINK}?start=trig_public"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Тарифи та переваги VIP", 
                url=f"{MAIN_BOT_LINK}?start=pricing"
            )
        ]
    ])

@router.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = (
        "👋 Вітаємо у **Kerdos News** — офіційному публічному каналі нашої спільноти!\n\n"
        "Тут публікуються звіти, аналітика та результати роботи закритих алгоритмів.\n\n"
        "Бажаєте приєднатися до VIP-групи та налаштувати автоматичні сигнали?"
    )
    await message.answer(
        welcome_text, 
        reply_markup=get_channel_keyboard(), 
        parse_mode="Markdown"
    )

@router.message(F.photo & (F.chat.type == "private"))
async def handle_admin_screenshot(message: Message):
    """Зберігаємо фото у словник та даємо коротку callback_data"""
    file_id = message.photo[-1].file_id
    user_id = message.from_user.id
    
    # Зберігаємо file_id для конкретного адміна
    admin_storage[user_id] = {
        "file_id": file_id,
        "caption": message.caption or "📊 Звіт із закритої VIP-групи Kerdos Community."
    }
    
    preview_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Опублікувати в канал", callback_data="confirm_pub")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_pub")]
    ])
    
    await message.answer_photo(
        photo=file_id,
        caption=f"<b>Попередній перегляд:</b>\n\n{admin_storage[user_id]['caption']}",
        reply_markup=preview_keyboard,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "confirm_pub")
async def confirm_publishing(callback: aiogram.types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in admin_storage:
        await callback.answer("Дані застаріли, надішліть фото знову.", show_alert=True)
        return
    
    data = admin_storage[user_id]
    
    try:
        await bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=data["file_id"],
            caption=f"📊 **Звіт із закритої VIP-групи Kerdos Community**\n\n{data['caption']}\n\nПриєднуйтесь до нашої системи!",
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(caption="✅ **Успішно опубліковано в канал!**", parse_mode="Markdown")
    except Exception as e:
        await callback.answer(f"Помилка публікації: {e}", show_alert=True)
        
    del admin_storage[user_id]
    await callback.answer()

@router.callback_query(F.data == "cancel_pub")
async def cancel_publishing(callback: aiogram.types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in admin_storage:
        del admin_storage[user_id]
    await callback.message.delete()
    await callback.answer("Скасовано.")

async def send_scheduled_report():
    report_text = (
        "📊 **Плановий звіт Kerdos Community**\n\n"
        "Алгоритми продовжують відпрацьовувати ринок у штатному режимі. "
        "Слідкуйте за оновленнями та приєднуйтесь до закритих сигналів.\n\n"
        "⚡️ Отримайте доступ до торгового робота за кнопкою нижче:"
    )
    try:
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=report_text,
            reply_markup=get_channel_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Помилка автоматичної розсилки звіту: {e}")

async def main():
    dp = Dispatcher()
    dp.include_router(router)
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_scheduled_report, 'cron', hour=10, minute=0)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Публічний бот-публікатор успішно запущено!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main)()
