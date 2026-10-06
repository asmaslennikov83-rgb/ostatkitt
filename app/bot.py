from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from .config import load_settings
from .service import DistributionService
from .price_compare import build_price_comparison
from .wb_api import WBApiError
from .stock_ops import StockManager, StockOperationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

settings = load_settings()
service = DistributionService(settings)
stock_manager = StockManager(settings)
router = Router()

BTN_DISTRIBUTE = "📦 Загрузить файл для распределения остатков"
BTN_KITS = "🧩 Загрузить шаблон комплектов"
BTN_COMPARE = "💰 Сравнить цены"
BTN_EXCLUSIONS = "🚫 Исключения ШК"
BTN_ROLLBACK = "↩️ Откатить загрузку"
BTN_HISTORY = "📂 История загрузок"
BTN_ZERO = "🛑 Аварийное обнуление FBS"
pending_runs: dict[int, dict] = {}
user_modes: dict[int, str] = {}
zero_selections: dict[int, dict] = {}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DISTRIBUTE)],
            [KeyboardButton(text=BTN_KITS)],
            [KeyboardButton(text=BTN_COMPARE)],
            [KeyboardButton(text=BTN_EXCLUSIONS)],
            [KeyboardButton(text=BTN_ROLLBACK), KeyboardButton(text=BTN_HISTORY)],
            [KeyboardButton(text=BTN_ZERO)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def allowed(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in settings.allowed_ids)


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids and user_id in settings.allowed_ids


def admin_keyboard(kind: str):
    rows = [
        [InlineKeyboardButton(text="Оба кабинета", callback_data=f"stock:scope:{kind}:both")],
        [InlineKeyboardButton(text=settings.cabinets[0].name, callback_data=f"stock:scope:{kind}:cabinet_1")],
        [InlineKeyboardButton(text=settings.cabinets[1].name, callback_data=f"stock:scope:{kind}:cabinet_2")],
        [InlineKeyboardButton(text="Отмена", callback_data="stock:cancel")],
    ]
    if kind == 'zero':
        rows.insert(-1, [InlineKeyboardButton(text='Выбрать склады', callback_data='stock:warehouses')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def warehouse_keyboard(selection: dict):
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if key in selection['selected'] else '⬜'} {label}",
        callback_data=f"stock:warehouse:{selection['id']}:{index}",
    )] for index, (key, label) in enumerate(selection['choices'].items())]
    rows.append([InlineKeyboardButton(text='Сравнить выбранные склады', callback_data='stock:scope:zero:selected')])
    rows.append([InlineKeyboardButton(text='Отмена', callback_data='stock:cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == 'stock:warehouses')
async def choose_zero_warehouses(query: CallbackQuery):
    actor = query.from_user.id
    if not is_admin(actor):
        await query.answer('Только для администраторов', show_alert=True)
        return
    await query.answer()
    try:
        selection = {'id': uuid.uuid4().hex[:12],
                     'choices': await stock_manager.warehouse_choices(actor), 'selected': set()}
        zero_selections[actor] = selection
        await query.message.answer('Отметьте склады для обнуления:', reply_markup=warehouse_keyboard(selection))
    except Exception as exc:
        await query.message.answer(f'Не удалось получить склады: {exc}')


@router.callback_query(F.data.startswith('stock:warehouse:'))
async def toggle_zero_warehouse(query: CallbackQuery):
    actor = query.from_user.id
    if not is_admin(actor):
        await query.answer('Только для администраторов', show_alert=True)
        return
    selection = zero_selections.get(actor)
    try:
        index = int(query.data.rsplit(':', 1)[1])
        if not selection or index < 0 or query.data.split(':')[-2] != selection['id']:
            raise ValueError()
        key = list(selection['choices'])[index]
    except (ValueError, IndexError):
        await query.answer('Откройте выбор складов заново', show_alert=True)
        return
    if key in selection['selected']:
        selection['selected'].remove(key)
    else:
        selection['selected'].add(key)
    stock_manager.active_previews.pop(actor, None)
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=warehouse_keyboard(selection))


def template_status() -> str:
    try:
        count = service.kits_count()
        return f"Шаблон комплектов загружен: {count} комплектов." if count else "Шаблон комплектов пока не загружен."
    except Exception:
        return "Шаблон комплектов найден, но содержит ошибку. Загрузите его заново."


def exclusions_status() -> str:
    try:
        marks, skus = service.exclusions_count()
        return f"Исключения ШК: {skus} ШК / {marks} исключений по кабинетам." if marks else "Исключения ШК пока не заданы."
    except Exception:
        return "Файл исключений найден, но содержит ошибку. Загрузите его заново."


@router.message(CommandStart())
async def start(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "🟢 Бот Wildberries работает.\n\n"
        f"{template_status()}\n"
        f"{exclusions_status()}\n\n"
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
        "📦 Пришлите Excel-файл (.xlsx или .xls) с двумя колонками: «Код» и «Доступно».\nСтарые названия «Баркод» и «Количество» тоже поддерживаются.\n\n"
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
        "🧩 Пришлите новый Excel-шаблон комплектов (.xlsx или .xls).\n\n"
        "Нужны колонки: «Название», «баркод комплекта», «баркод1», «баркод2» и далее. "
        "Одинаковые баркоды в одной строке означают кратность компонента.\n\n"
        "После успешной загрузки этот шаблон будет использоваться до следующего обновления.",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_EXCLUSIONS)
async def choose_exclusions(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes.pop(message.from_user.id, None)
    cab1_id = settings.cabinets[0].seller_id or "не удалось определить автоматически"
    cab2_id = settings.cabinets[1].seller_id or "не удалось определить автоматически"
    await message.answer(
        "🚫 Исключения ШК\n\n"
        "В шаблоне три колонки: «ШК», «ID кабинета 1», «ID кабинета 2».\n"
        "Если напротив ШК заполнен ID продавца в колонке нужного кабинета, этот ШК не участвует в распределении именно на этом кабинете.\n"
        "На втором кабинете тот же ШК продолжает работать, если там ячейка пустая.\n\n"
        f"Кабинет 1 — {settings.cabinets[0].name}: {cab1_id}\n"
        f"Кабинет 2 — {settings.cabinets[1].name}: {cab2_id}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Скачать шаблон", callback_data="exclusions:download")],
            [InlineKeyboardButton(text="📤 Загрузить исключения", callback_data="exclusions:upload")],
        ]),
    )


@router.callback_query(F.data == "exclusions:download")
async def download_exclusions(query: CallbackQuery):
    if not query.from_user or query.from_user.id not in settings.allowed_ids:
        await query.answer("Доступ запрещён", show_alert=True)
        return
    await query.answer()
    try:
        path = service.exclusions_download_path()
        await query.bot.send_document(
            chat_id=query.message.chat.id,
            document=FSInputFile(path, filename="Исключения_ШК.xlsx"),
            caption=(
                "Шаблон исключений ШК. Отредактируйте его и загрузите обратно "
                "через кнопку «🚫 Исключения ШК»."
            ),
        )
    except Exception as exc:
        logger.exception("Не удалось отправить шаблон исключений")
        await query.message.answer(f"❌ Не удалось сформировать шаблон исключений:\n{exc}")


@router.callback_query(F.data == "exclusions:upload")
async def upload_exclusions(query: CallbackQuery):
    if not query.from_user or query.from_user.id not in settings.allowed_ids:
        await query.answer("Доступ запрещён", show_alert=True)
        return
    user_modes[query.from_user.id] = "exclusions"
    await query.answer()
    await query.message.answer(
        "📤 Пришлите заполненный Excel-файл исключений (.xlsx или .xls).\n"
        "После успешной загрузки он сохранится и будет применяться ко всем следующим распределениям.",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_COMPARE)
async def choose_price_comparison(message: Message):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return
    user_modes.pop(message.from_user.id, None)
    await message.answer(
        "💰 Сравнение текущих цен продавца и количества заказов FBO + FBS в двух кабинетах.\n"
        "В отчёт попадут только товары, присутствующие в обоих кабинетах. Выберите период:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 неделя", callback_data="compare_prices:7"),
             InlineKeyboardButton(text="2 недели", callback_data="compare_prices:14")],
        ]),
    )


@router.callback_query(F.data.startswith("compare_prices:"))
async def run_price_comparison(query: CallbackQuery):
    if not query.from_user or query.from_user.id not in settings.allowed_ids:
        await query.answer("Доступ запрещён", show_alert=True)
        return
    try:
        days = int(query.data.split(":", 1)[1])
        if days not in (7, 14):
            raise ValueError("Неподдерживаемый период")
    except (ValueError, AttributeError, IndexError):
        await query.answer("Неправильный период", show_alert=True)
        return
    await query.answer()
    status = await query.message.answer("⏳ Получаю каталог, цены и заказы двух кабинетов…")
    try:
        with tempfile.TemporaryDirectory(prefix="wb_prices_") as tmp:
            out = Path(tmp) / f"Сравнение_цен_и_заказов_{days}_дней.xlsx"
            result = await build_price_comparison(settings, days, out)
            await query.message.answer_document(
                FSInputFile(out), caption=(
                    f"✅ Сравнение готово: {result['products']} общих товаров.\n"
                    f"Заказов: {settings.cabinets[0].name} — {result['orders_1']}, "
                    f"{settings.cabinets[1].name} — {result['orders_2']}.\n"
                    f"Без однозначной цены: {result['missing_prices']}; "
                    f"неоднозначных сопоставлений исключено: {result['ambiguous']}.\n"
                    "Цена текущая, заказы — за выбранные полные дни; скидки WB не учитываются."
                ),
            )
        await status.edit_text("✅ Отчёт сформирован. Выберите следующее действие.")
        await query.message.answer("Главное меню:", reply_markup=main_menu())
    except WBApiError as exc:
        logger.exception("Price comparison WB API failed")
        await status.edit_text(f"❌ Не удалось получить данные WB для сравнения цен:\n{exc}\n"
                               "Проверьте разрешения токенов: Контент, Цены и скидки, Статистика.")
    except Exception as exc:
        logger.exception("Price comparison failed")
        await status.edit_text(f"❌ Не удалось сформировать сравнение цен: {exc}")


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
        f"{template_status()}\n"
        f"{exclusions_status()}",
        reply_markup=main_menu(),
    )


@router.message(F.document)
async def document_handler(message: Message, bot: Bot):
    if not allowed(message):
        await message.answer("⛔ Доступ к боту запрещён.")
        return

    doc = message.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith((".xlsx", ".xls")):
        await message.answer("Нужен файл Excel в формате .xlsx или .xls", reply_markup=main_menu())
        return

    mode = user_modes.get(message.from_user.id)
    if mode not in {"distribution", "kits", "exclusions"}:
        await message.answer(
            "Сначала выберите в главном меню, что именно вы хотите загрузить.",
            reply_markup=main_menu(),
        )
        return

    if mode == "exclusions":
        status = await message.answer("⏳ Проверяю и сохраняю исключения ШК…")
        try:
            with tempfile.TemporaryDirectory(prefix="wb_exclusions_") as tmp:
                local_path = Path(tmp) / ("exclusions.xls" if filename.endswith(".xls") else "exclusions.xlsx")
                file = await bot.get_file(doc.file_id)
                await bot.download_file(file.file_path, destination=local_path)
                marks, skus = service.update_exclusions_template(local_path)
            user_modes.pop(message.from_user.id, None)
            await status.edit_text(
                f"✅ Исключения обновлены.\n\nУникальных ШК: {skus}.\n"
                f"Исключений по кабинетам: {marks}.\n"
                "Файл сохранён и будет применяться при следующих распределениях."
            )
            await message.answer("Выберите следующее действие:", reply_markup=main_menu())
        except ValueError as exc:
            await status.edit_text(f"❌ Ошибка файла исключений:\n{exc}")
        except Exception as exc:
            logger.exception("Exclusions template update failed")
            await status.edit_text(f"❌ Не удалось сохранить исключения:\n{exc}")
        return

    if mode == "kits":
        status = await message.answer("⏳ Проверяю и сохраняю шаблон комплектов…")
        try:
            with tempfile.TemporaryDirectory(prefix="wb_kits_") as tmp:
                local_path = Path(tmp) / ("kits.xls" if filename.endswith(".xls") else "kits.xlsx")
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
            local_path = Path(tmp) / ("input.xls" if filename.endswith(".xls") else "input.xlsx")
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
                f"Комплектов не найдены в WB: {len(summary.not_found_kit_barcodes)}\n"
                f"ШК пропущено по !: {len(summary.excluded_barcodes)}"
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

            pending_runs[message.from_user.id] = result
            user_modes.pop(message.from_user.id, None)
            await message.answer(
                "Что сделать с готовым распределением?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Получить Excel-файлы", callback_data="dist:download")],
                    [InlineKeyboardButton(text="🚀 Загрузить на WB", callback_data="dist:upload")],
                    [InlineKeyboardButton(text="📄 Скачать и загрузить", callback_data="dist:both")],
                    [InlineKeyboardButton(text="Отмена", callback_data="dist:cancel")],
                ]),
            )

    except ValueError as exc:
        await status.edit_text(f"❌ Ошибка входного Excel или шаблона комплектов:\n{exc}")
    except WBApiError as exc:
        await status.edit_text(f"❌ Ошибка WB API. Распределение не сформировано:\n{exc}")
    except Exception as exc:
        logger.exception("Processing failed")
        await status.edit_text(f"❌ Не удалось сформировать распределение:\n{exc}")



@router.callback_query(F.data.startswith("dist:"))
async def distribution_action(query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    if user_id not in settings.allowed_ids:
        await query.answer("Доступ запрещён", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    await query.answer()
    if action == "cancel":
        pending_runs.pop(user_id, None)
        await query.message.answer("Операция отменена. Данные WB не изменены.")
        return
    result = pending_runs.get(user_id)
    if not result:
        await query.message.answer("Результат распределения не найден. Загрузите Excel заново.")
        return
    if action in {"download", "both"}:
        try:
            await query.message.answer_document(FSInputFile(result["zip"]), caption="Архив всех FBS-складов")
            for file_path in result["files"]:
                await query.message.answer_document(FSInputFile(file_path))
        except Exception as exc:
            logger.exception("Distribution download failed")
            await query.message.answer(f"❌ Не удалось отправить Excel: {exc}")
            return
    if action in {"upload", "both"}:
        if not is_admin(user_id):
            await query.message.answer("⛔ Загрузка на WB разрешена только администратору. Добавьте ваш ID в TELEGRAM_ADMIN_IDS.")
            return
        await query.message.answer("Выберите кабинеты для загрузки:", reply_markup=admin_keyboard("distribution"))
    else:
        await query.message.answer("Файлы отправлены. При необходимости можете выбрать загрузку на WB выше.")


@router.message(F.text == BTN_HISTORY)
async def stock_history(message: Message):
    if not allowed(message):
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ История операций с WB доступна администраторам.")
        return
    records = stock_manager.list_records()
    if not records:
        await message.answer("История загрузок пока пуста.")
        return
    lines = ["📂 Последние операции с остатками WB:"]
    kind_names = {"distribution": "Загрузка", "zero": "Обнуление", "rollback": "Откат"}
    for item in records:
        lines.append(f"{item['created_at'][:19]} — {kind_names.get(item['kind'], item['kind'])}, "
                     f"{item.get('status', '?')}, подтверждено: {item.get('verified_count', 0)}. "
                     f"ID: {item['id'][:10]}")
    await message.answer("\n".join(lines))
    await message.answer("Резервные копии последних операций:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text=f"📥 {item['created_at'][:19]} — {item['id'][:8]}",
                                                   callback_data=f"stock:backup:{item['id']}")]
                             for item in records
                         ]))


@router.message(F.text == BTN_ROLLBACK)
async def rollback_menu(message: Message):
    if not allowed(message):
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Откат доступен только администратору.")
        return
    source = stock_manager.latest_rollback_source()
    if not source:
        await message.answer("Нет последнего обновления, которое можно откатить.")
        return
    await message.answer(
        f"↩️ Последнее обновление: {source['created_at'][:19]} UTC, "
        f"тип: {source['kind']}.\n"
        "Восстановим только реально изменённые ботом позиции, которые с тех пор не изменились на WB. "
        "При изменениях из-за заказов эти позиции будут пропущены.",
        reply_markup=admin_keyboard("rollback"),
    )


@router.message(F.text == BTN_ZERO)
async def zero_menu(message: Message):
    if not allowed(message):
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Обнуление доступно только администратору.")
        return
    await message.answer(
        "🛑 АВАРИЙНОЕ ОБНУЛЕНИЕ FBS\n\n"
        "Будут обнулены ненулевые остатки всех доступных вариантов АКТИВНОГО каталога "
        "на выбранных складах, включая товары вне последнего Excel. "
        "FBO не затрагивается.\n\n"
        "⚠️ API WB не позволяет перечислить все остатки без списка chrtID. "
        "Товары вне активного каталога могут не попасть в операцию. "
        "Перед отправкой будет сохранена резервная копия.",
        reply_markup=admin_keyboard("zero"),
    )


def _preview_text(data: dict) -> str:
    summary = data['summary']
    label = {'distribution': 'Загрузка на WB', 'zero': 'Аварийное обнуление',
             'rollback': 'Восстановление из резервной копии'}[data['kind']]
    warning = ""
    if data['kind'] == 'distribution':
        changes = summary.get('changed', 0)
        zeroed = summary.get('zeroed', 0)
        if changes and zeroed / changes >= 0.5:
            warning = "\n⚠️ ВНИМАНИЕ: половина или больше изменений — обнуления! Проверьте Excel.\n"
    return (f"🔎 {label} — предпросмотр\n"
            f"Кабинетов: {len(data['cabinets'])}; складов: {len(data['targets'])}\n"
            f"Позиций к изменению: {summary.get('changed', 0)}\n"
            f"Увеличится: {summary.get('increased', 0)}\n"
            f"Уменьшится: {summary.get('decreased', 0)}\n"
            f"Обнулится: {summary.get('zeroed', 0)}\n"
            f"Без изменений: {summary.get('unchanged', 0)}\n"
            f"Пропущено конфликтующих позиций: {len(data['conflicts'])}\n\n"
            f"Защищено исключениями: {len(data.get('excluded_positions', []))}\n"
            f"{warning}\n"
            "⚠️ Это предварительные данные. Перед PUT бот ещё раз сверит WB. "
            "Операция будет сохранена в истории с резервной копией.")


@router.callback_query(F.data.startswith("stock:scope:"))
async def stock_scope(query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    if not is_admin(user_id):
        await query.answer("Только для администраторов", show_alert=True)
        return
    parts = query.data.split(":")
    if len(parts) != 4:
        await query.answer("Некорректный запрос", show_alert=True)
        return
    _, _, kind, scope = parts
    if kind not in {"distribution", "zero", "rollback"} or scope not in {"both", "cabinet_1", "cabinet_2", "selected"} or (scope == 'selected' and kind != 'zero'):
        await query.answer("Некорректный запрос", show_alert=True)
        return
    await query.answer()
    status = await query.message.answer("⏳ Считываю текущие остатки и готовлю сравнение с WB…")
    try:
        keys = {"cabinet_1", "cabinet_2"} if scope == "both" else {scope}
        warehouse_keys = None
        if scope == 'selected':
            warehouse_keys = set(zero_selections.get(user_id, {}).get('selected', set()))
            if not warehouse_keys:
                raise StockOperationError('Выберите хотя бы один склад')
            keys = {key.split(':', 1)[0] for key in warehouse_keys}
        files = None
        source_id = None
        if kind == "distribution":
            run = pending_runs.get(user_id)
            if not run:
                raise StockOperationError("Нет готового распределения. Загрузите Excel заново.")
            files = run['files']
        if kind == "rollback":
            source = stock_manager.latest_rollback_source()
            if not source:
                raise StockOperationError("Нет последнего обновления для отката")
            source_id = source['id']
        data = await stock_manager.preview(kind, user_id, keys, files=files, source_id=source_id,
                                           warehouse_keys=warehouse_keys)
        await status.edit_text(_preview_text(data))
        await query.message.answer_document(FSInputFile(stock_manager.preview_report(data)),
                                            caption="Сравнение остатков: было на WB → станет")
        if data['summary'].get('changed', 0) == 0:
            await query.message.answer("Изменений нет. Отправка на WB не требуется.")
            return
        await query.message.answer(
            "Перейти к подтверждению операции?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Продолжить", callback_data=f"stock:confirm:{data['id']}")],
                [InlineKeyboardButton(text="Отмена", callback_data="stock:cancel")],
            ]),
        )
    except Exception as exc:
        logger.exception("Stock preview failed")
        await status.edit_text(f"❌ Предпросмотр не удался: {exc}. Остатки WB не изменены.")


@router.callback_query(F.data.startswith("stock:backup:"))
async def download_stock_backup(query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    if not is_admin(user_id):
        await query.answer("Только для администраторов", show_alert=True)
        return
    ident = query.data.split(":")[-1]
    path = stock_manager._record_path(ident)
    if not ident.isalnum() or not path.is_file():
        await query.answer("Резервная копия не найдена", show_alert=True)
        return
    await query.answer()
    await query.message.answer_document(FSInputFile(path, filename=f"WB_backup_{ident[:10]}.json"),
                                        caption="Резервная копия и журнал операции. Храните файл в безопасном месте.")


@router.callback_query(F.data == "stock:cancel")
async def stock_cancel(query: CallbackQuery):
    await query.answer()
    if query.from_user:
        stock_manager.active_previews.pop(query.from_user.id, None)
        zero_selections.pop(query.from_user.id, None)
        user_modes.pop(query.from_user.id, None)
    await query.message.answer("Предпросмотр отменён. Уже запущенную запись эта кнопка не останавливает; её результат будет в истории.")


@router.callback_query(F.data.startswith("stock:confirm:"))
async def stock_confirm(query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    if not is_admin(user_id):
        await query.answer("Только для администраторов", show_alert=True)
        return
    await query.answer()
    try:
        data = stock_manager.get_preview(query.data.split(":")[-1], user_id)
    except StockOperationError as exc:
        await query.message.answer(f"❌ {exc}")
        return
    if data['kind'] == 'zero':
        user_modes[user_id] = f"zero_confirm:{data['id']}"
        await query.message.answer(
            "🛑 Для подтверждения аварийного обнуления отправьте отдельным сообщением точное слово ОБНУЛИТЬ. "
            "Это НЕ отменяет необходимость финального подтверждения кнопкой."
        )
        return
    await query.message.answer(
        "⚠️ Последнее подтверждение. Изменения будут отправлены в WB. "
        "При ошибке возможна частичная загрузка. Продолжить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, выполнить", callback_data=f"stock:execute:{data['id']}")],
            [InlineKeyboardButton(text="Отмена", callback_data="stock:cancel")],
        ]),
    )


@router.message(F.text == "ОБНУЛИТЬ")
async def zero_phrase(message: Message):
    if not allowed(message) or not is_admin(message.from_user.id):
        return
    mode = user_modes.pop(message.from_user.id, '')
    if not mode.startswith('zero_confirm:'):
        await message.answer("Сначала выберите аварийное обнуление в меню и подтвердите предпросмотр.")
        return
    ident = mode.split(':', 1)[1]
    try:
        data = stock_manager.get_preview(ident, message.from_user.id)
        if data['kind'] != 'zero':
            raise StockOperationError('Некорректная операция')
    except StockOperationError as exc:
        await message.answer(f"❌ {exc}")
        return
    stock_manager.zero_authorized.add((message.from_user.id, ident))
    await message.answer(
        "🛑 ПОСЛЕДНЕЕ ПОДТВЕРЖДЕНИЕ: отправить нули на WB?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 ДА, ОБНУЛИТЬ", callback_data=f"stock:execute:{ident}")],
            [InlineKeyboardButton(text="Отмена", callback_data="stock:cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("stock:execute:"))
async def stock_execute(query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    if not is_admin(user_id):
        await query.answer("Только для администраторов", show_alert=True)
        return
    ident = query.data.split(":")[-1]
    await query.answer()
    status = await query.message.answer("⏳ Обновляю остатки WB и проверяю результат…")
    try:
        record = await stock_manager.execute(ident, user_id)
        text = (f"{'✅' if record['status'] == 'verified' else '⚠️'} Операция: {record['status']}\n"
                f"Проверено успешных изменений: {record['verified_count']} из {record['expected_count']}\n"
                f"Ошибок: {sum(len(v) if isinstance(v, list) else 1 for v in record['errors'].values())}\n"
                "Резервная копия и журнал сохранены в data/stock_operations/records.")
        await status.edit_text(text)
        await query.message.answer_document(FSInputFile(stock_manager._record_path(ident),
                                                        filename=f"WB_result_{ident[:10]}.json"),
                                            caption="Результат и резервная копия (включая ошибки)")
    except Exception as exc:
        logger.exception("Stock operation failed")
        await status.edit_text(f"❌ Операция не завершена: {exc}. Проверьте историю и фактические остатки WB.")


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
                f"{exclusions_status()}\n"
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
