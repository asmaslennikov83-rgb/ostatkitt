from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message, BotCommand

from .config import load_settings
from .service import DistributionService
from .wb_api import WBApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = load_settings()
service = DistributionService(settings)
router = Router()


def allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in settings.allowed_ids)


@router.message(CommandStart())
async def start(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    await message.answer(
        "Пришлите Excel-файл (.xlsx) с двумя колонками: «Баркод» и «Количество».\n\n"
        "Я получу FBS-склады обоих кабинетов, возьму заказы за последние 14 дней, "
        "распределю общий физический остаток и верну отдельный Excel для каждого склада."
    )


@router.message(Command("id"))
async def my_id(message: Message):
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(F.document)
async def document_handler(message: Message, bot: Bot):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return

    doc = message.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await message.answer("Нужен файл Excel в формате .xlsx")
        return

    status = await message.answer("⏳ Получил файл. Проверяю WB и распределяю остатки…")

    try:
        with tempfile.TemporaryDirectory(prefix="wb_fbs_") as tmp:
            local_path = Path(tmp) / "input.xlsx"
            file = await bot.get_file(doc.file_id)
            await bot.download_file(file.file_path, destination=local_path)

            result = await service.process(local_path, message.from_user.id)
            summary = result["summary"]

            text = (
                "✅ Распределение готово\n\n"
                f"Строк во входном файле: {summary.input_lines}\n"
                f"Товара во входном файле: {summary.input_units} шт.\n"
                f"Распределено: {summary.allocated_units} шт.\n"
                f"Осталось в резерве: {summary.reserve_units} шт.\n"
                f"Файлов складов: {summary.output_files}\n"
                f"ШК без истории заказов: {len(summary.no_sales_barcodes)}\n"
                f"ШК не найдены в WB: {len(summary.not_found_barcodes)}"
            )
            await status.edit_text(text)

            if summary.not_found_barcodes:
                chunks = [summary.not_found_barcodes[i:i+40] for i in range(0, len(summary.not_found_barcodes), 40)]
                for chunk in chunks:
                    await message.answer("⚠️ Не найдены в обоих кабинетах:\n" + "\n".join(f"• {x}" for x in chunk))

            # Основной удобный вариант — ZIP, плюс отдельные Excel по запросу пользователя.
            await message.answer_document(
                FSInputFile(result["zip"]),
                caption="Все файлы FBS-складов одним архивом",
            )
            for file_path in result["files"]:
                await message.answer_document(FSInputFile(file_path))

    except ValueError as exc:
        await status.edit_text(f"❌ Ошибка входного Excel:\n{exc}")
    except WBApiError as exc:
        await status.edit_text(f"❌ Ошибка WB API. Распределение не сформировано:\n{exc}")
    except Exception as exc:
        logger.exception("Processing failed")
        await status.edit_text(f"❌ Не удалось сформировать распределение:\n{exc}")


@router.message()
async def fallback(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    await message.answer("Пришлите .xlsx с колонками «Баркод» и «Количество».")


async def notify_startup(bot: Bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Проверить, что бот работает"),
            BotCommand(command="status", description="Статус бота"),
            BotCommand(command="id", description="Показать мой Telegram ID"),
        ])
    except Exception:
        logger.exception("Failed to set bot commands")

    for user_id in settings.allowed_ids:
        try:
            await bot.send_message(
                user_id,
                "🟢 Бот Wildberries запущен и работает.\n\n"
                "Можно отправлять Excel-файл с колонками «Баркод» и «Количество»."
            )
        except Exception:
            logger.warning("Could not send startup notification to Telegram ID %s", user_id, exc_info=True)


@router.message(Command("status"))
async def status_command(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    await message.answer(
        "🟢 Бот работает.\n"
        "Готов принять Excel-файл с остатками и сформировать распределение по FBS-складам."
    )


async def main():
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(router)
    await notify_startup(bot)
    logger.info("WB FBS Distributor bot started successfully")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
