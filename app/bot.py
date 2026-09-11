from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

from .config import load_settings
from .service import DistributionService
from .wb_api import WBApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = load_settings()
service = DistributionService(settings)
router = Router()

BTN_DISTRIBUTE = "📦 Загрузить файл для распределения остатков"
BTN_KITS = "🧩 Загрузить шаблон комплектов"
user_modes: dict[int, str] = {}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DISTRIBUTE)],
            [KeyboardButton(text=BTN_KITS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in settings.allowed_ids)


def template_status() -> str:
    try:
        count = service.kits_count()
        return f"Шаблон комплектов загружен: {count} комплектов." if count else "Шаблон комплектов пока не загружен."
    except Exception:
        return "Шаблон комплектов найден, но содержит ошибку. Загрузите его заново."


@router.message(CommandStart())
async def start(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "🟢 Бот Wildberries работает.\n\n"
        f"{template_status()}\n\n"
        "Выберите действие в главном меню:",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_DISTRIBUTE)
async def choose_distribution(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes[message.from_user.id] = "distribution"
    await message.answer(
        "📦 Пришлите Excel-файл (.xlsx) с двумя колонками: «Баркод» и «Количество».\n\n"
        "При распределении я учту актуальный шаблон комплектов.",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_KITS)
async def choose_kits(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes[message.from_user.id] = "kits"
    await message.answer(
        "🧩 Пришлите новый Excel-шаблон комплектов (.xlsx).\n\n"
        "Нужны колонки: «Название», «баркод комплекта», «баркод1», «баркод2» и далее. "
        "Одинаковые баркоды в одной строке означают кратность компонента.\n\n"
        "После успешной загрузки этот шаблон будет использоваться до следующего обновления.",
        reply_markup=main_menu(),
    )


@router.message(Command("id"))
async def my_id(message: Message):
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("status"))
async def status_command(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    await message.answer(
        "🟢 Бот работает.\n"
        f"{template_status()}",
        reply_markup=main_menu(),
    )


@router.message(F.document)
async def document_handler(message: Message, bot: Bot):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return

    doc = message.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await message.answer("Нужен файл Excel в формате .xlsx", reply_markup=main_menu())
        return

    mode = user_modes.get(message.from_user.id)
    if mode not in {"distribution", "kits"}:
        await message.answer(
            "Сначала выберите в главном меню, что именно вы хотите загрузить.",
            reply_markup=main_menu(),
        )
        return

    if mode == "kits":
        status = await message.answer("⏳ Проверяю и сохраняю шаблон комплектов…")
        try:
            with tempfile.TemporaryDirectory(prefix="wb_kits_") as tmp:
                local_path = Path(tmp) / "kits.xlsx"
                file = await bot.get_file(doc.file_id)
                await bot.download_file(file.file_path, destination=local_path)
                count = service.update_kits_template(local_path)
            user_modes.pop(message.from_user.id, None)
            await status.edit_text(
                f"✅ Шаблон комплектов обновлён.\n\nЗагружено комплектов: {count}.\n"
                "Он будет использоваться при следующих распределениях до новой загрузки."
            )
            await message.answer("Выберите следующее действие:", reply_markup=main_menu())
        except ValueError as exc:
            await status.edit_text(f"❌ Ошибка шаблона комплектов:\n{exc}")
        except Exception as exc:
            logger.exception("Kit template update failed")
            await status.edit_text(f"❌ Не удалось сохранить шаблон комплектов:\n{exc}")
        return

    status = await message.answer("⏳ Получил остатки. Проверяю WB, комплекты и распределяю товар…")
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
                f"Физического товара во входном файле: {summary.input_units} шт.\n"
                f"Использовано/распределено физического товара: {summary.allocated_units} шт.\n"
                f"Осталось в резерве: {summary.reserve_units} шт.\n"
                f"Сформировано комплектов: {summary.kit_units} шт. ({summary.kit_skus} SKU)\n"
                f"Файлов складов: {summary.output_files}\n"
                f"Одиночных ШК без истории заказов: {len(summary.no_sales_barcodes)}\n"
                f"Одиночных ШК не найдены в WB: {len(summary.not_found_barcodes)}\n"
                f"Комплектов без истории заказов: {len(summary.no_sales_kit_barcodes)}\n"
                f"Комплектов не найдены в WB: {len(summary.not_found_kit_barcodes)}"
            )
            await status.edit_text(text)

            if summary.not_found_barcodes:
                chunks = [summary.not_found_barcodes[i:i + 40] for i in range(0, len(summary.not_found_barcodes), 40)]
                for chunk in chunks:
                    await message.answer(
                        "⚠️ Одиночные ШК не найдены в обоих кабинетах:\n" +
                        "\n".join(f"• {x}" for x in chunk)
                    )

            if summary.not_found_kit_barcodes:
                chunks = [summary.not_found_kit_barcodes[i:i + 40] for i in range(0, len(summary.not_found_kit_barcodes), 40)]
                for chunk in chunks:
                    await message.answer(
                        "⚠️ Баркоды комплектов не найдены в обоих кабинетах (компоненты на них не резервировались):\n" +
                        "\n".join(f"• {x}" for x in chunk)
                    )

            await message.answer_document(
                FSInputFile(result["zip"]),
                caption="Все файлы FBS-складов одним архивом",
            )
            for file_path in result["files"]:
                await message.answer_document(FSInputFile(file_path))

        user_modes.pop(message.from_user.id, None)
        await message.answer("Готов к следующей операции:", reply_markup=main_menu())

    except ValueError as exc:
        await status.edit_text(f"❌ Ошибка входного Excel или шаблона комплектов:\n{exc}")
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
    await message.answer("Выберите действие в главном меню.", reply_markup=main_menu())


async def notify_startup(bot: Bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="status", description="Статус бота и шаблона комплектов"),
            BotCommand(command="id", description="Показать мой Telegram ID"),
        ])
    except Exception:
        logger.exception("Failed to set bot commands")

    for user_id in settings.allowed_ids:
        try:
            await bot.send_message(
                user_id,
                "🟢 Бот Wildberries запущен и работает.\n\n"
                f"{template_status()}\n"
                "Откройте /start для главного меню."
            )
        except Exception:
            logger.warning("Could not send startup notification to Telegram ID %s", user_id, exc_info=True)


async def main():
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(router)
    await notify_startup(bot)
    logger.info("WB FBS Distributor bot started successfully")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
