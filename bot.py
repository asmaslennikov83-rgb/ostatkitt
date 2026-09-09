\
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from agent_client import AgentAuthError, AgentClient, AgentUnavailable
from config import (
    ALLOWED_TELEGRAM_USER_IDS,
    CABINETS,
    CONTROL_EXPECTED_FBO,
    CONTROL_NM_IDS,
    HTTP_TIMEOUT_SECONDS,
    TELEGRAM_BOT_TOKEN,
    WB_AGENT_SECRET,
    WB_AGENT_URL,
    WB_APP_TYPE,
    WB_CURRENCY,
    WB_DEST,
    WB_SPP,
    validate,
)
from excel_export import export_workbook
from wb_client import StockRow, WBApiError, WBClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("wb-real-stock-hybrid")

router = Router()
_lock = asyncio.Lock()


def allowed(user_id: int | None) -> bool:
    return not ALLOWED_TELEGRAM_USER_IDS or (
        user_id is not None and user_id in ALLOWED_TELEGRAM_USER_IDS
    )


def wb_client(key: str) -> WBClient:
    return WBClient(CABINETS[key].token, HTTP_TIMEOUT_SECONDS)


def agent_client() -> AgentClient:
    return AgentClient(WB_AGENT_URL, WB_AGENT_SECRET, HTTP_TIMEOUT_SECONDS)


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"📦 {CABINETS['cab1'].name}", callback_data="export:cab1"),
            InlineKeyboardButton(text=f"📦 {CABINETS['cab2'].name}", callback_data="export:cab2"),
        ],
        [InlineKeyboardButton(text="📊 Оба кабинета", callback_data="export:both")],
        [InlineKeyboardButton(text="🔌 Проверить Windows-агент", callback_data="agent-health")],
        [InlineKeyboardButton(text="🧪 Проверить контрольные артикула", callback_data="control")],
    ])


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "WB Real Stock Bot — гибридный режим.\n\n"
        "Seller API работает на сервере.\n"
        "Реальный FBO получает Windows-агент через ваш локальный интернет.",
        reply_markup=keyboard(),
    )


async def build_rows_for(key: str) -> list[StockRow]:
    products, physical_by_nm, fbs_by_chrt = await wb_client(key).base_data()
    if not products:
        return []

    real_fbo = await agent_client().get_real_fbo(
        [p.nm_id for p in products],
        dest=WB_DEST,
        currency=WB_CURRENCY,
        app_type=WB_APP_TYPE,
        spp=WB_SPP,
    )

    rows = []
    for p in products:
        fbs = sum(fbs_by_chrt.get(chrt, 0) for chrt in p.chrt_ids)
        physical = int(physical_by_nm.get(p.nm_id, 0))
        real = int(real_fbo.get(p.nm_id, 0))
        rows.append(StockRow(
            nm_id=p.nm_id,
            vendor_code=p.vendor_code,
            title=p.title,
            barcodes=", ".join(p.barcodes),
            physical_fbo=physical,
            real_fbo=real,
            hidden_fbo=max(physical - real, 0),
            fbs=fbs,
            total_physical=physical + fbs,
        ))
    rows.sort(key=lambda x: x.nm_id)
    return rows


@router.callback_query(F.data == "agent-health")
async def health_handler(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        return
    await callback.answer()
    status = await callback.message.answer("🔌 Проверяю Windows-агент...")
    try:
        data = await agent_client().health()
        await status.edit_text(
            "✅ Windows-агент доступен.\n"
            f"Версия: {data.get('version', '?')}\n"
            f"Витрина WB: {data.get('storefront', 'unknown')}"
        )
    except Exception as exc:
        await status.edit_text(
            "❌ Windows-агент недоступен.\n\n"
            f"{str(exc)[:1000]}\n\n"
            "Проверьте, что agent.py и Cloudflare Tunnel запущены, "
            "а WB_AGENT_URL на сервере содержит текущий HTTPS URL туннеля."
        )


@router.callback_query(F.data == "control")
async def control_handler(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        return
    await callback.answer()
    status = await callback.message.answer("🧪 Проверяю контрольные артикула через Windows-агент...")
    try:
        actual = await agent_client().get_real_fbo(
            CONTROL_NM_IDS,
            dest=WB_DEST,
            currency=WB_CURRENCY,
            app_type=WB_APP_TYPE,
            spp=WB_SPP,
        )
        lines = [f"dest={WB_DEST}", ""]
        all_ok = True
        for nm in CONTROL_NM_IDS:
            val = actual.get(nm, 0)
            exp = CONTROL_EXPECTED_FBO.get(nm)
            ok = exp is None or val == exp
            all_ok = all_ok and ok
            lines.append(f"{'✅' if ok else '⚠️'} {nm}: {val} (ожидалось {exp})")
        if not all_ok:
            lines += ["", "Если агент работает, но значения отличаются, проверим WB_DEST/структуру витрины."]
        await status.edit_text("\n".join(lines))
    except Exception as exc:
        await status.edit_text(f"❌ Ошибка агента:\n{str(exc)[:1200]}")


@router.callback_query(F.data.startswith("export:"))
async def export_handler(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        return
    if _lock.locked():
        await callback.answer("Выгрузка уже выполняется.", show_alert=True)
        return

    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    keys = ["cab1", "cab2"] if mode == "both" else [mode]
    status = await callback.message.answer("⏳ Получаю Seller API + реальные FBO через Windows-агент...")

    try:
        async with _lock:
            sheets = []
            counts = []
            for key in keys:
                rows = await build_rows_for(key)
                sheets.append((CABINETS[key].name, rows))
                counts.append(f"• {CABINETS[key].name}: {len(rows)} товаров")

            tmp = Path(tempfile.gettempdir()) / "wb_real_stock_hybrid"
            name = "wb_real_stocks.xlsx" if len(keys) == 2 else f"wb_real_stocks_{keys[0]}.xlsx"
            path = export_workbook(tmp / name, sheets)

        await status.edit_text("✅ Готово.\n" + "\n".join(counts))
        await callback.message.answer_document(
            FSInputFile(path),
            caption=f"Реальные остатки WB. Витрина dest={WB_DEST}",
        )

    except AgentUnavailable as exc:
        log.warning("Agent unavailable: %s", exc)
        await status.edit_text(
            "❌ Не могу получить реальный FBO: Windows-агент недоступен.\n\n"
            f"{str(exc)[:900]}\n\n"
            "Seller API может работать, но Excel без реального FBO не формирую, "
            "чтобы не выдавать неверные данные."
        )
    except AgentAuthError:
        await status.edit_text("❌ WB_AGENT_SECRET на сервере и Windows-агенте не совпадают.")
    except WBApiError as exc:
        log.exception("WB API error")
        await status.edit_text(f"❌ Ошибка Seller API WB:\n{str(exc)[:1200]}")
    except Exception as exc:
        log.exception("Export failed")
        await status.edit_text(f"❌ Ошибка:\n{str(exc)[:1200]}")


async def main() -> None:
    validate()
    bot = Bot(TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
