import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Отримуємо змінні середовища, які ви налаштували на Render
BOT_TOKEN = os.getenv("NEW_BOT_TOKEN")
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")
MAIN_BOT_LINK = os.getenv("MAIN_BOT_LINK", "https://t.me/Mireya_signals_bot")

bot = Bot(token=BOT_TOKEN)
router = Router()

def get_channel_keyboard() -> InlineKeyboardMarkup:
    """Універсальна клавіатура з посиланням на головний бот (Mireya)"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
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
    return keyboard

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Привітання у приватах публічного бота"""
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
    """
    Якщо ви (як адмін) надсилаєте скріншот у особисті повідомлення цьому боту,
    він генерує готову картку з кнопкою для публікації в канал.
    """
    caption = message.caption or "📈 Черговий успішний звіт із закритої групи Kerdos Community."
    
    # Кнопки для перевірки перед відправкою в канал
    preview_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Опублікувати в канал", callback_data=f"pub_{message.photo[-1].file_id}")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_pub")]
    ])
    
    await message.answer_photo(
        photo=message.photo[-1].file_id,
        caption=f"<b>Попередній перегляд:</b>\n\n{caption}",
        reply_markup=preview_keyboard,
        parse_mode="HTML"
    )

# Обробка натискання кнопки підтвердження публікації
@router.callback_query(F.data.startswith("pub_"))
async def confirm_publishing(callback: aiogram.types.CallbackQuery):
    file_id = callback.data.split("_")[1]
    
    # Публікуємо у ваш публічний канал із кнопкою-запрошенням
    await bot.send_photo(
        chat_id=PUBLIC_CHANNEL_ID,
        photo=file_id,
        caption="📊 **Звіт із закритої VIP-групи Kerdos Community**\n\nПриєднуйтесь до нашої системи та торгуйте автоматично!",
        reply_markup=get_channel_keyboard(),
        parse_mode="Markdown"
    )
    await callback.message.edit_caption(caption="✅ **Успішно опубліковано в канал!**", parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "cancel_pub")
async def cancel_publishing(callback: aiogram.types.CallbackQuery):
    await callback.message.delete()
    await callback.answer("Скасовано.")

async def send_scheduled_report():
    """Фонова задача: автоматичний звіт за розписанням"""
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
        )
    except Exception as e:
        logging.error(f"Помилка автоматичної розсилки звіту: {e}")

async def main():
    dp = Dispatcher()
    dp.include_router(router)
    
    # Налаштування планувальника (наприклад, звіт щодня о 10:00 за UTC)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_scheduled_report, 'cron', hour=10, minute=0)
    scheduler.start()
    
    # Видаляємо вебхуки, якщо лишилися, і запускаємо polling для адміністрування через особисті повідомлення
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Публічний бот-публікатор успішно запущено!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
