\
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import (
    ALLOWED_TELEGRAM_USER_IDS,
    CABINETS,
    CONTROL_EXPECTED_FBO,
    CONTROL_NM_IDS,
    HTTP_TIMEOUT_SECONDS,
    TELEGRAM_BOT_TOKEN,
    WB_APP_TYPE,
    WB_CURRENCY,
    WB_DEST,
    WB_SPP,
    validate,
)
from excel_export import export_workbook
from wb_client import WBApiError, WBClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wb-real-stock-bot")

router = Router()
_export_lock = asyncio.Lock()


def is_allowed(user_id: int | None) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id is not None and user_id in ALLOWED_TELEGRAM_USER_IDS


def client_for(cabinet_key: str) -> WBClient:
    cabinet = CABINETS[cabinet_key]
    return WBClient(
        cabinet.token,
        dest=WB_DEST,
        currency=WB_CURRENCY,
        app_type=WB_APP_TYPE,
        spp=WB_SPP,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📦 {CABINETS['cab1'].name}",
                    callback_data="export:cab1",
                ),
                InlineKeyboardButton(
                    text=f"📦 {CABINETS['cab2'].name}",
                    callback_data="export:cab2",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Оба кабинета",
                    callback_data="export:both",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧪 Проверить контрольные артикула",
                    callback_data="control",
                )
            ],
        ]
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not is_allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Бот реальных остатков WB.\n\n"
        "• физический FBO — официальный отчёт WB;\n"
        "• FBS — склады продавца выбранного кабинета;\n"
        "• реальный FBO — покупательская витрина WB;\n"
        "• скрытый FBO = физический FBO − реальный FBO.",
        reply_markup=main_keyboard(),
    )


async def generate_for_keys(keys: list[str]) -> tuple[Path, dict[str, int]]:
    sheets = []
    counts: dict[str, int] = {}

    for key in keys:
        cabinet = CABINETS[key]
        rows = await client_for(key).build_stock_rows()
        sheets.append((cabinet.name, rows))
        counts[cabinet.name] = len(rows)

    tmp_dir = Path(tempfile.gettempdir()) / "wb_real_stock_bot"
    filename = "wb_real_stocks.xlsx"
    if len(keys) == 1:
        filename = f"wb_real_stocks_{keys[0]}.xlsx"

    path = export_workbook(tmp_dir / filename, sheets)
    return path, counts


@router.callback_query(F.data.startswith("export:"))
async def export_handler(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    mode = callback.data.split(":", 1)[1]
    keys = ["cab1", "cab2"] if mode == "both" else [mode]

    if _export_lock.locked():
        await callback.answer(
            "Выгрузка уже выполняется. Дождитесь её завершения.",
            show_alert=True,
        )
        return

    await callback.answer()
    status = await callback.message.answer(
        "⏳ Собираю остатки WB, FBS и покупательскую доступность..."
    )

    try:
        async with _export_lock:
            path, counts = await generate_for_keys(keys)

        summary = "\n".join(
            f"• {name}: {count} товаров"
            for name, count in counts.items()
        )
        await status.edit_text(
            "✅ Выгрузка готова.\n" + summary
        )
        await callback.message.answer_document(
            FSInputFile(path),
            caption=(
                "Реальные остатки Wildberries.\n"
                f"Регион витрины (dest): {WB_DEST}"
            ),
        )
    except WBApiError as exc:
        log.exception("WB API error")
        await status.edit_text(
            "❌ Ошибка WB API.\n\n"
            f"{str(exc)[:1200]}\n\n"
            "Проверьте категории доступа токена: "
            "Контент + Маркетплейс + Аналитика."
        )
    except Exception as exc:
        log.exception("Export failed")
        await status.edit_text(
            f"❌ Не удалось сформировать файл:\n{str(exc)[:1200]}"
        )


@router.callback_query(F.data == "control")
async def control_handler(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer(
        "🧪 Проверяю контрольные артикула по витрине WB..."
    )

    # Витрина общая, поэтому для проверки достаточно любого валидного клиента.
    try:
        actual = await client_for("cab1").check_nm_ids(CONTROL_NM_IDS)
        lines = [
            f"Регион витрины dest={WB_DEST}",
            "",
        ]
        ok_all = True

        for nm_id in CONTROL_NM_IDS:
            value = actual.get(nm_id, 0)
            expected = CONTROL_EXPECTED_FBO.get(nm_id)
            if expected is None:
                marker = "ℹ️"
                exp_text = "ожидание не задано"
            else:
                match = value == expected
                ok_all = ok_all and match
                marker = "✅" if match else "⚠️"
                exp_text = f"ожидалось {expected}"

            lines.append(
                f"{marker} {nm_id}: реальный FBO = {value} ({exp_text})"
            )

        if not ok_all:
            lines.extend([
                "",
                "Если значения отличаются от 0 / 0 / 4, "
                "в первую очередь проверьте WB_DEST: покупательская "
                "доступность зависит от региона/адреса доставки.",
            ])

        await status.edit_text("\n".join(lines))
    except Exception as exc:
        log.exception("Control check failed")
        await status.edit_text(
            f"❌ Ошибка проверки:\n{str(exc)[:1200]}"
        )


async def main() -> None:
    validate()
    bot = Bot(TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
