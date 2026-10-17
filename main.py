import asyncio
import os
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from functions.db.db import Database
from functions.db.db import Database, User
from functions.payment import CBPayment
from functions.orderCode import generate_secure_random_string
from functions.smmway_api import SMMWayAPI
from functions.account_manager import AccountManager
from functions.funpay_manager import FunPayManager

API_TOKEN = ''

# ======================== DEBUG MODE =========================
DEBUG = False  # При True заказы создаются только в БД, без отправки на smmway.ru

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
db = Database()
payment_handler = CBPayment()
smmway_api = None  # Инициализируется при запуске, если нужен API ключ
account_manager = None  # Инициализируется при запуске
funpay_manager = None  # Инициализируется при запуске, если задан funpay_config.json

# ======================== settings =========================

TECHNICAL_BREAK = True  # Если True — работать могут только админы, остальным "техработы"
ITEMS_PER_PAGE = 5
GLOBAL_MARKUP_PERCENT = 250.0  # Глобальная наценка в процентах
REFERRAL_PERCENT = 5.0  # Процент от покупок рефералов (настраивается через админку)
SUPPORT_USERNAMES = ["diwo_fp"]  # Deprecated: используйте аккаунты из админки (Аккаунты поддержки)
CATALOG_SYNC_INTERVAL = 3600  # Интервал синхронизации каталога в секундах (1 час)
ORDER_STATUS_CHECK_INTERVAL = 300 # Интервал проверки статусов заказов в секундах (5 минут)

# Ссылки проекта / политика (если example.com — кнопка не показывается)
PROJECT_INFO_URL = "https://example.com/about"  # TODO: заменить на актуальный URL о проекте
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialno-03-01"

def _is_valid_url(url: str) -> bool:
    """Не показывать кнопку для example.com"""
    return bool(url and "example.com" not in url.lower())

# ======================== cache =========================
admins = []
_banned_cache = {}  # tg_id -> (banned: bool, ts)
_BAN_CACHE_TTL = 30  # секунд

class TechnicalBreakMiddleware(BaseMiddleware):
    """Блокирует неадминов при TECHNICAL_BREAK"""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not TECHNICAL_BREAK:
            return await handler(event, data)
        user = getattr(event, 'from_user', None)
        if not user:
            return await handler(event, data)
        if await is_admin(user.id):
            return await handler(event, data)
        msg = "⚠️ В данный момент проводятся технические работы. Пожалуйста, вернитесь позже."
        try:
            from aiogram.types import CallbackQuery
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg, parse_mode="HTML")
        except Exception:
            pass
        return

async def _is_user_banned_cached(tg_id: int) -> bool:
    """Проверка бана с кэшем (снижает нагрузку на БД)"""
    import time
    now = time.time()
    if tg_id in _banned_cache:
        banned, ts = _banned_cache[tg_id]
        if now - ts < _BAN_CACHE_TTL:
            return banned
    banned = await db.is_user_banned(tg_id)
    _banned_cache[tg_id] = (banned, now)
    return banned

def _invalidate_banned_cache(tg_id: int):
    _banned_cache.pop(tg_id, None)

class BanCheckMiddleware(BaseMiddleware):
    """Блокирует забаненных пользователей (кроме админов)"""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, 'from_user', None)
        if not user:
            return await handler(event, data)
        if await is_admin(user.id):
            return await handler(event, data)
        if await _is_user_banned_cached(user.id):
            msg = "❌ Ваш аккаунт заблокирован."
            try:
                from aiogram.types import CallbackQuery
                if isinstance(event, CallbackQuery):
                    await event.answer(msg, show_alert=True)
                else:
                    await event.answer(msg, parse_mode="HTML")
            except Exception:
                pass
            return
        return await handler(event, data)

# ======================== FSM States ========================
class AdminStates(StatesGroup):
    get_user_id = State()
    add_money_amount = State()
    broadcast_message = State()
    add_admin_user = State()
    set_global_markup = State()
    set_referral_percent = State()
    find_payment = State()
    find_order = State()
    create_promocode_code = State()
    create_promocode_discount = State()
    create_promocode_activations = State()
    user_search_query = State()
    user_send_message_text = State()
    user_add_balance_amount = State()

class PaymentStates(StatesGroup):
    waiting_amount = State()

class ManualPaymentStates(StatesGroup):
    entering_amount = State()

class CatalogStates(StatesGroup):
    selecting_app = State()
    selecting_service = State()
    entering_link = State()
    entering_quantity = State()
    catalog_search = State()

class UserSearchStates(StatesGroup):
    find_order = State()
    find_payment = State()

class PromoCodeStates(StatesGroup):
    entering_code = State()

class AccountManagerStates(StatesGroup):
    waiting_forum_id = State()
    waiting_phone = State()
    waiting_user_id_ban = State()
    waiting_user_id_unban = State()
    waiting_autoreply = State()

class SupportAccountsStates(StatesGroup):
    waiting_add_username = State()
    waiting_delete_username = State()

class PrivacyStates(StatesGroup):
    waiting_accept = State()

# ======================== Functions =========================
async def cache_admins():
    users = await db.get_admins()
    admins.clear()
    for user in users:
        if user.tg_id not in admins:
            admins.append(user.tg_id)

async def is_admin(id: int) -> bool:
    return id in admins

async def format_order_status(status: str) -> str:
    status_emoji = {
        'new': '🆕',
        'processing': '⏳',
        'completed': '✅',
        'cancelled': '❌',
        'failed': '⚠️',
        'api_error': '⚙️'
    }
    status_text = {
        'new': 'Новый',
        'processing': 'В обработке',
        'completed': 'Завершен',
        'cancelled': 'Отменен',
        'failed': 'Ошибка',
        'api_error': 'Ошибка API'
    }
    emoji = status_emoji.get(status, '❓')
    text = status_text.get(status, status)
    return f"{emoji} {text}"

async def format_payment_status(status: str) -> str:
    status_emoji = {
        'paid': '✅',
        'expired': '⏰',
        'pending': '⏳',
        'error': '❌'
    }
    status_text = {
        'paid': 'Оплачено',
        'expired': 'Истекло',
        'pending': 'Ожидание',
        'error': 'Ошибка'
    }
    emoji = status_emoji.get(status, '❓')
    text = status_text.get(status, status)
    return f"{emoji} {text}"

def format_balance(balance: int) -> str:
    """Форматирование баланса с разделителями"""
    balance_str = str(balance)
    # Разделяем по 3 цифры с пробелами
    formatted = ' '.join(balance_str[::-1][i:i+3] for i in range(0, len(balance_str), 3))
    return formatted[::-1]

def format_profile_text(user_id: int, first_name: str, user_db: User) -> str:
    """Формирует текст для сообщения в профиле."""
    balance = user_db.balance if user_db else 0
    text = f"""
    👤 <b>Ваш профиль</b>

    🆔 ID: <code>{user_id}</code>
    👤 Имя: {first_name}
    💰 Баланс: <b>{format_balance(balance)}₽</b>
"""
    if user_db and user_db.discount_percent > 0:
        text += f"\n🔥 Ваша персональная скидка: <b>{user_db.discount_percent}%</b>"

    text += """

    Выберите действие:
    """
    return text

# ======================== Keyboards =========================
def get_main_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text='🛒 Каталог'), KeyboardButton(text='👤 Профиль'), KeyboardButton(text='ℹ️ О проекте')]
    ]
    if user_id in admins:
        kb.append([KeyboardButton(text='👑 Админ')])
    return ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True
    )

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"), # 📊 Статистика
        InlineKeyboardButton(text="📩 Рассылка", callback_data="admin_broadcast") # 📩 Рассылка
    )
    builder.row(
        InlineKeyboardButton(text='🛍 Заказы', callback_data='admin_orders_page_1'), # 🛍 Все заказы
        InlineKeyboardButton(text='💳 Пополнения', callback_data='admin_payments_page_1') # 💳 Все пополнения
    )
    builder.row(
        InlineKeyboardButton(text='👥 Пользователи', callback_data='admin_users'), # Управление пользователями
        InlineKeyboardButton(text='📈 Наценка', callback_data='admin_global_markup') # 📈 Общая наценка
    )
    builder.row(
        InlineKeyboardButton(text='👥 Рефералка', callback_data='admin_referral'), # 👥 Рефералы
        InlineKeyboardButton(text='🔄 Сброс статы', callback_data='admin_reset_stats') # 🔄 Сброс статистики
    )
    builder.row(
        InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin_create_promocode")
    )
    builder.row(
        InlineKeyboardButton(text="🆘 Аккаунты поддержки", callback_data="admin_support_accounts")
    )
    builder.row(
        InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main')
    )
    return builder.as_markup()

def get_cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True
    )

async def get_profile_kb(balance: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='📥 Пополнить', callback_data='payment_method_select'),
        InlineKeyboardButton(text=f'💰 {balance}₽', callback_data='balance_info')
    )
    builder.row( # 🛍 Мои заказы, 💳 Мои пополнения
        InlineKeyboardButton(text='🛍 Мои заказы', callback_data='my_orders_page_1'), 
        InlineKeyboardButton(text='💳 Мои пополнения', callback_data='my_payments_page_1')
    )
    builder.row(
        InlineKeyboardButton(text='👥 Реферальная программа', callback_data='referral_info')
    )
    builder.row(
        InlineKeyboardButton(text='🎁 Ввести промокод', callback_data='enter_promocode')
    )
    builder.row(
        InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main')
    )
    return builder.as_markup()

def get_back_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main'))
    return builder.as_markup()

# ======================== Handlers =========================

@dp.callback_query(F.data == "noop")
async def noop_callback(callback: types.CallbackQuery):
    """Callback for pagination buttons that do nothing (e.g., page number)."""
    await callback.answer()

@dp.callback_query(F.data == "accept_privacy")
async def accept_privacy(callback: types.CallbackQuery):
    """Пользователь принимает политику конфиденциальности"""
    await db.set_privacy_accepted(callback.from_user.id)
    await callback.answer("✅ Политика конфиденциальности принята")
    await callback.message.edit_text(
        "✅ Политика конфиденциальности принята. Теперь вы можете пользоваться ботом.\n\n"
        "Откройте меню и выберите нужный раздел.",
        parse_mode="HTML"
    )

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == 'private':
        await message.delete() # Удаляем команду /start
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    referrer_code = args[0] if args else None
    
    user = await db.get_user(tg_id=message.from_user.id)
    referrer_id = None
    
    if not user:
        # Проверяем реферальный код
        if referrer_code:
            referrer = await db.get_user_by_referral_code(referrer_code)
            if referrer:
                referrer_id = referrer.id
        
        user = await db.create_user(
            tg_id=message.from_user.id,
            referrer_id=referrer_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )
        
        if referrer_id:
            await message.answer(
                f"🎉 Вы зарегистрированы по реферальной ссылке!\n"
                f"Пригласивший получит {REFERRAL_PERCENT}% с ваших покупок."
            )
    else:
        # Если пользователь уже существует, но пришел по реферальной ссылке
        if referrer_code and not user.referrer_id:
            referrer = await db.get_user_by_referral_code(referrer_code)
            if referrer:
                user.referrer_id = referrer.id
                from functions.db.db import Session
                async with Session() as session:
                    session.add(user)
                    await session.commit()
        # Обновляем username/имя только для существующих (для новых уже в create_user)
        await db.update_user_names(
            tg_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )

    await cache_admins()

    # Проверка принятия политики конфиденциальности
    if not getattr(user, "privacy_accepted", 0):
        kb = InlineKeyboardBuilder()
        if _is_valid_url(PRIVACY_POLICY_URL):
            kb.row(InlineKeyboardButton(text="📜 Открыть политику", url=PRIVACY_POLICY_URL))
        kb.row(InlineKeyboardButton(text="✅ Принимаю политику", callback_data="accept_privacy"))
        await message.answer(
            "📜 <b>Политика конфиденциальности</b>\n\n"
            "Перед использованием бота необходимо принять политику конфиденциальности.",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )
        return
    
    welcome_text = f"""
👋 Добро пожаловать, {message.from_user.first_name}!

🤖 Я бот для автоматизации заказов подписок.

📋 Доступные функции:
• 🛒 Каталог услуг
• 👤 Профиль и баланс
• 👥 Реферальная программа
• ℹ️ О проекте

Выберите действие из меню ниже 👇
"""
    await message.answer(welcome_text, reply_markup=get_main_keyboard(message.from_user.id))

def _page_app_cb(page: int, q: str = "") -> str:
    """callback_data для пагинации: page_app_2 или page_app_2_q"""
    if q:
        return f"page_app_{page}_{q[:20]}"  # ограничение длины
    return f"page_app_{page}"

async def _build_catalog_apps_keyboard(apps: list, page: int = 1, search_query: str = ""):
    """Клавиатура каталога приложений: Telegram первым + поиск"""
    builder = InlineKeyboardBuilder()
    start_index = (page - 1) * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    for app in apps[start_index:end_index]:
        builder.row(InlineKeyboardButton(text=f"📱 {app.name}", callback_data=f"app_{app.id}"))
    nav = []
    total_pages = max(1, (len(apps) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=_page_app_cb(page - 1, search_query)))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=_page_app_cb(page + 1, search_query)))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔍 Поиск", callback_data="catalog_search_start"))
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main'))
    return builder.as_markup()

@dp.message(F.text == "🛒 Каталог")
async def catalog_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.delete()
    apps = await db.get_apps_sorted_telegram_first()
    if not apps:
        await message.answer(
            "📦 <b>Каталог услуг</b>\n\nКаталог пуст. Обратитесь к администратору.",
            parse_mode="HTML", reply_markup=get_back_kb()
        )
        return
    await message.answer(
        "📦 <b>Каталог услуг</b>\n\nВыберите приложение (Telegram, Instagram и др.):",
        parse_mode="HTML",
        reply_markup=await _build_catalog_apps_keyboard(apps, 1)
    )

@dp.callback_query(F.data.startswith("page_app_"))
async def page_app_handler(callback: types.CallbackQuery):
    parts = callback.data.split("_", 3)
    page = int(parts[2])
    search_q = parts[3] if len(parts) > 3 else ""
    apps = await db.get_apps_sorted_telegram_first(search_q or None)
    sub = " (поиск)" if search_q else ""
    await callback.message.edit_text(
        f"📦 <b>Каталог услуг</b>{sub}\n\nВыберите приложение:",
        parse_mode="HTML",
        reply_markup=await _build_catalog_apps_keyboard(apps, page, search_q)
    )
    await callback.answer()

@dp.callback_query(F.data == "catalog_search_start")
async def catalog_search_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔍 <b>Поиск по каталогу</b>\n\nВведите название приложения (например: Telegram, Instagram):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="catalog_back")]])
    )
    await state.set_state(CatalogStates.catalog_search)
    await callback.answer()

@dp.message(CatalogStates.catalog_search)
async def catalog_search_process(message: types.Message, state: FSMContext):
    await state.clear()
    await message.delete()
    q = (message.text or "").strip()
    apps = await db.get_apps_sorted_telegram_first(q or None)
    if not apps:
        await message.answer(
            "🔍 Ничего не найдено. Попробуйте другой запрос.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="catalog_back")]])
        )
        return
    await message.answer(
        f"📦 <b>Каталог услуг</b> (поиск)\n\nВыберите приложение:",
        parse_mode="HTML",
        reply_markup=await _build_catalog_apps_keyboard(apps, 1, q)
    )

@dp.callback_query(F.data.startswith("app_"))
async def select_app(callback: types.CallbackQuery):
    app_id = int(callback.data.split("_")[1])
    app = await db.get_app_by_id(app_id)
    
    if not app:
        await callback.answer("❌ Приложение не найдено", show_alert=True)
        return
    
    services = await db.get_services_by_app(app_id)
    
    if not services:
        await callback.message.edit_text(
            f"📱 <b>{app.name}</b>\n\nВ этом приложении пока нет услуг.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад к приложениям', callback_data='catalog_back')]])
        )
        await callback.answer()
        return
    
    page = 1
    builder = InlineKeyboardBuilder()
    start_index = (page - 1) * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    
    for service in services[start_index:end_index]:
        builder.row(
            InlineKeyboardButton(text=service.name, callback_data=f"service_{service.id}")
        )
    
    nav_buttons = []
    total_pages = (len(services) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"page_service_{app_id}_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад к приложениям', callback_data='catalog_back'))
    
    await callback.message.edit_text(
        f"📱 <b>{app.name}</b>\n\n"
        "Выберите услугу:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("page_service_"))
async def page_service_handler(callback: types.CallbackQuery):
    _, _, app_id_str, page_str = callback.data.split("_")
    app_id = int(app_id_str)
    page = int(page_str)
    
    app = await db.get_app_by_id(app_id)
    if not app:
        await callback.answer("❌ Приложение не найдено", show_alert=True)
        return
        
    services = await db.get_services_by_app(app_id)
    
    builder = InlineKeyboardBuilder()
    start_index = (page - 1) * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    
    for service in services[start_index:end_index]:
        builder.row(
            InlineKeyboardButton(text=service.name, callback_data=f"service_{service.id}")
        )
    
    nav_buttons = []
    total_pages = (len(services) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_service_{app_id}_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"page_service_{app_id}_{page+1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад к приложениям', callback_data='catalog_back'))
    
    await callback.message.edit_text(
        f"📱 <b>{app.name}</b>\n\nВыберите услугу:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("service_"))
async def select_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    service = await db.get_service_by_id(service_id)
    
    if not service:
        await callback.answer("❌ Услуга не найдена", show_alert=True)
        return
    
    app = await db.get_app_by_id(service.app_id)
    prices = await db.get_prices_by_service(service_id)
    
    if not prices:
        await callback.message.edit_text(
            f"🔧 <b>{service.name}</b>\n\nДля этой услуги пока нет пакетов.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад к услугам', callback_data=f"app_{app.id}")]]),
        )
        await callback.answer()
        return
    
    # Since we have 1 price per service now
    price_obj = prices[0]
    
    await state.update_data(
        price_id=price_obj.id,
        service_id=service.id,
        app_id=app.id,
        price_per_item_sell=price_obj.priceSell,
        price_per_item_buy=price_obj.priceBuy,
        min_quantity=price_obj.min_quantity,
        max_quantity=price_obj.max_quantity,
        service_name=service.name,
        app_name=app.name,
    )
    
    price_per_item_formatted = f"{price_obj.priceSell:.4f}".rstrip('0').rstrip('.')
    
    await callback.message.edit_text(
        f"🔧 <b>Выбранная услуга</b>\n\n"
        f"📱 Приложение: {app.name}\n\n"
        f"🔧 Услуга: {service.name}\n"
        f"💰 Цена за 1 шт: <b>{price_per_item_formatted}₽</b>\n\n"
        f"Введите ссылку для выполнения заказа:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад к услугам', callback_data=f"app_{app.id}")]]),
    )
    await state.set_state(CatalogStates.entering_link)
    await callback.answer()

@dp.message(CatalogStates.entering_link)
async def process_link(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Заказ отменен.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    link = message.text.strip()
    
    # Простая валидация ссылки
    if not (link.startswith("http://") or link.startswith("https://")):
        await message.answer("❌ Пожалуйста, введите корректную ссылку (начинается с http:// или https://):", reply_markup=get_cancel_kb())
        return

    await state.update_data(link=link)
    
    data = await state.get_data()
    min_q = data.get('min_quantity')
    max_q = data.get('max_quantity')
    
    await message.answer(
        f"✅ Ссылка принята.\n\n"
        f"Теперь введите желаемое количество.\n"
        f"Минимум: <b>{min_q}</b>, Максимум: <b>{max_q}</b>",
        parse_mode="HTML",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(CatalogStates.entering_quantity)

@dp.message(CatalogStates.entering_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Заказ отменен.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        quantity = int(message.text)
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число.", reply_markup=get_cancel_kb())
        return

    data = await state.get_data()
    min_q = data.get('min_quantity')
    max_q = data.get('max_quantity')

    if min_q and quantity < min_q:
        await message.answer(f"❌ Минимальное количество для заказа: {min_q}. Попробуйте еще раз.", reply_markup=get_cancel_kb())
        return
    if max_q and quantity > max_q:
        await message.answer(f"❌ Максимальное количество для заказа: {max_q}. Попробуйте еще раз.", reply_markup=get_cancel_kb())
        return

    user = await db.get_user(tg_id=message.from_user.id)
    discount_percent = user.discount_percent if user else 0.0

    price_per_item_sell = data['price_per_item_sell']
    price_per_item_buy = data['price_per_item_buy']
    
    total_sell_price_original = price_per_item_sell * quantity
    total_buy_price = price_per_item_buy * quantity

    discount_amount = 0
    if discount_percent > 0:
        discount_amount = total_sell_price_original * (discount_percent / 100.0)

    final_sell_price = total_sell_price_original - discount_amount
    
    await state.update_data(
        quantity=quantity,
        total_sell_price=final_sell_price, # Это финальная цена к оплате
        total_buy_price=total_buy_price,
        original_sell_price=total_sell_price_original,
        discount_applied=discount_percent
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Подтвердить и оплатить", callback_data="confirm_order_payment"))
    builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order"))
    
    price_text = f"💰 Итоговая цена: <b>{final_sell_price:.2f}₽</b>"
    if discount_percent > 0:
        price_text = (f"💰 Итоговая цена: <s>{total_sell_price_original:.2f}₽</s> <b>{final_sell_price:.2f}₽</b>\n"
                      f"✨ (Скидка {discount_percent}%)")

    await message.answer(
        f"📝 <b>Подтверждение заказа</b>\n\n"
        f"📱 Приложение: {data['app_name']}\n"
        f"🔧 Услуга: {data['service_name']}\n"
        f"🔗 Ссылка: {data['link']}\n"
        f"🔢 Количество: <b>{quantity}</b>\n\n"
        f"{price_text}\n\n"
        f"Ваш баланс: {format_balance(user.balance)}₽",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "confirm_order_payment")
async def confirm_order_payment_handler(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    total_sell_price = data['total_sell_price']
    user = await db.get_user(tg_id=callback.from_user.id)

    if user.balance >= total_sell_price:
        await process_order_payment(
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            state=state,
            data=data,
            use_balance=True
        )
    else:
        needed = total_sell_price - user.balance
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="💳 CryptoBot (0%)", callback_data=f"topup_{needed}"))
        builder.row(InlineKeyboardButton(text="🧾 По реквизитам (4%)", callback_data="profile_back"))
        builder.row(InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order"))
        
        await callback.message.edit_text(
            f"💰 <b>Недостаточно средств</b>\n\n"
            f"Ваш баланс: <b>{format_balance(user.balance)}₽</b>\n"
            f"Требуется: <b>{total_sell_price:.2f}₽</b>\n"
            f"Не хватает: <b>{needed:.2f}₽</b>\n\n"
            f"CryptoBot — без комиссии. По реквизитам — 4% комиссия.",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("topup_"))
async def handle_topup_for_order(callback: types.CallbackQuery, state: FSMContext):
    try:
        needed_amount_str = callback.data.split("_", 1)[1]
        needed_amount = float(needed_amount_str)
    except (IndexError, ValueError):
        await callback.answer("Ошибка: неверные данные", show_alert=True)
        return

    min_crypto_amount = 10.0
    amount_to_pay = max(needed_amount, min_crypto_amount)
    
    # Создаем пополнение
    order_code = generate_secure_random_string()
    payment_data = {
        'user_id': callback.from_user.id,
        'description': 'Пополнение для заказа',
        'amount': f'{amount_to_pay}R',
        'order': order_code
    }
    
    await callback.message.edit_text("⏳ Создаю счет для пополнения...", reply_markup=None)
    
    success, result = await payment_handler.create_invoice(payment_data)
    
    if success:
        text = (f"💳 <b>Счет для пополнения</b>\n\n"
                f"📋 Номер: <code>{order_code}</code>\n"
                f"💰 Сумма к оплате: <b>{amount_to_pay:.2f}₽</b>\n\n")

        if amount_to_pay > needed_amount:
            text += f"ℹ️ Вам не хватало {needed_amount:.2f}₽, но минимальная сумма авто-пополнения {min_crypto_amount:.2f}₽. Остаток будет зачислен на ваш баланс.\n\n"

        text += f"Ссылка для оплаты:\n{result}\n\n"
        text += "После оплаты заказ будет автоматически создан."
        
        await callback.message.answer(
            text,
            parse_mode="HTML"
        )
        
        # Сохраняем данные заказа для создания после оплаты
        order_data = await state.get_data()
        await state.update_data(
            pending_order=True,
            payment_order_code=order_code,
            **order_data
        )
        # Clear reply keyboard
        await callback.message.answer("...", reply_markup=ReplyKeyboardRemove())

        # Запускаем проверку оплаты
        asyncio.create_task(check_payment_and_create_order(order_code, payment_data, callback.from_user.id, state))
    else:
        await callback.message.answer(f"❌ Ошибка: {result}")
    
    await callback.answer()

@dp.callback_query(F.data == "cancel_order")
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Заказ отменен.", reply_markup=None)
    await callback.message.answer("Выберите действие:", reply_markup=get_main_keyboard(callback.from_user.id))
    await callback.answer()

async def check_order_statuses_periodically():
    """Периодически проверяет статусы заказов в обработке через API."""
    while True:
        await asyncio.sleep(ORDER_STATUS_CHECK_INTERVAL)
        if not smmway_api:
            continue

        print("🔄 Проверка статусов заказов...")
        try:
            processing_orders = await db.get_processing_orders()

            if not processing_orders:
                print("✅ Нет заказов в обработке для проверки.")
                continue

            print(f"🔍 Найдено {len(processing_orders)} заказов для проверки статуса.")
            for order in processing_orders:
                api_status_result = await smmway_api.get_order_status(order.api_order_id)

                if "error" in api_status_result:
                    print(f"❌ Ошибка при проверке статуса заказа {order.id} (API ID: {order.api_order_id}): {api_status_result['error']}")
                    continue

                # SMMWay API статусы: Pending, In progress, Processing, Completed, Partial, Canceled
                api_status = api_status_result.get('status')
                if not api_status:
                    continue

                new_status = None
                notify_user = False
                notification_text = ""

                if api_status in ['Completed', 'Partial']:
                    new_status = 'completed'
                    notify_user = True
                    notification_text = f"✅ Ваш заказ <code>{order.order_code}</code> был выполнен."
                elif api_status == 'Canceled':
                    new_status = 'cancelled'
                    notify_user = True
                    # Возврат средств
                    user = await db.get_user_by_db_id(order.user_id)
                    if user:
                        await db.add_balance(user.tg_id, order.price_sell)
                        notification_text = f"❌ Ваш заказ <code>{order.order_code}</code> был отменен. Средства в размере {order.price_sell:.2f}₽ возвращены на ваш баланс."
                    else:
                        notification_text = f"❌ Ваш заказ <code>{order.order_code}</code> был отменен. Свяжитесь с поддержкой для возврата средств."

                if new_status:
                    await db.update_order_status(order.id, new_status)
                    print(f"✅ Статус заказа {order.id} (API ID: {order.api_order_id}) обновлен на '{new_status}' по данным API.")
                    if notify_user:
                        try:
                            user = await db.get_user_by_db_id(order.user_id)
                            if user:
                                await bot.send_message(user.tg_id, notification_text, parse_mode="HTML")
                        except Exception as e:
                            print(f"⚠️ Не удалось уведомить пользователя {user.tg_id} о смене статуса заказа {order.id}: {e}")
                
                await asyncio.sleep(1)  # Небольшая задержка между запросами

        except Exception as e:
            print(f"❌ Критическая ошибка в планировщике проверки статусов: {e}")

async def process_order_payment(user_id: int, chat_id: int, state: FSMContext, data: dict, use_balance: bool = False):
    """Обработка оплаты и создание заказа"""
    total_sell_price = data['total_sell_price']
    total_buy_price = data['total_buy_price']
    quantity = data['quantity']
    link = data['link']
    service_name = data['service_name']
    app_name = data['app_name']
    
    user = await db.get_user(tg_id=user_id)
    
    if use_balance:
        # Списываем с баланса
        await db.add_balance(tg_id=user_id, amount=-total_sell_price)
        user = await db.get_user(tg_id=user_id)

    # Создаем заказ через API (если не DEBUG)
    api_order_id = None
    api_response = None

    # Получаем service_api_id из базы данных
    service = await db.get_service_by_id(data.get('service_id', 0))
    service_api_id = service.service_api_id if service and service.service_api_id else None
    
    if not DEBUG and smmway_api:
        try:
            # Получаем service_api_id из базы данных
            service = await db.get_service_by_id(data.get('service_id', 0))
            service_api_id = service.service_api_id if service and service.service_api_id else None
            
            if service_api_id:
                api_response = await smmway_api.create_order(
                    service_id=service_api_id,
                    link=link,
                    quantity=quantity
                )
                
                if api_response.get("success") or api_response.get("order"):
                    api_order_id = api_response.get("order_id") or api_response.get("order")
                    print(f"✅ Заказ создан в API smmway.ru: {api_order_id}")
                else:
                    error_text = api_response.get('error', '').lower()
                    print(f"❌ Ошибка API: {error_text}")
                    # Проверяем на ошибку нехватки средств
                    if 'funds' in error_text or 'balance' in error_text or 'средств' in error_text:
                        for admin_id in admins:
                            try:
                                await bot.send_message(admin_id, f"⚠️ Недостаточно средств на балансе SMMWay API для выполнения заказа. Заказ потребует ручного повтора.")
                            except Exception as e:
                                print(f"Не удалось уведомить админа {admin_id}: {e}")
        except Exception as e:
            print(f"Ошибка при создании заказа через API: {e}")
            api_response = {"success": False, "error": str(e)}
    
    # Создаем заказ в БД
    order_code = generate_secure_random_string()
    order = await db.create_order(
        tg_id=user_id,
        service=f"{app_name} - {service_name}",
        order_code=order_code,
        quantity=quantity,
        service_api_id=service_api_id,
        link=link,
        api_order_id=api_order_id,
        price_buy=total_buy_price,
        price_sell=total_sell_price
    )
    
    # Начисляем рефералу если есть
    if user.referrer_id:
        await db.add_referral_earnings(
            referrer_id=user.referrer_id,
            amount=float(total_sell_price),
            referral_percent=REFERRAL_PERCENT
        )
        # Уведомляем реферера
        try:
            referrer = await db.get_user(tg_id=user.referrer_id)
            if referrer:
                earnings = float(total_sell_price) * (REFERRAL_PERCENT / 100)
                await bot.send_message(
                    referrer.tg_id,
                    f"💰 <b>Начисление с реферала!</b>\n\n"
                    f"Сумма: <b>+{earnings:.2f}₽</b>\n"
                    f"От покупки на {total_sell_price:.2f}₽",
                    parse_mode="HTML"
                )
        except:
            pass
    
    # Обновляем статус заказа
    if DEBUG:
        order = await db.update_order_status(order.id, "completed")
        status_text = "✅ Заказ создан (DEBUG режим)"
    elif api_response and ("error" in api_response):
        error_text = api_response.get('error', '').lower()
        if 'funds' in error_text or 'balance' in error_text or 'средств' in error_text:
            order = await db.update_order_status(order.id, "api_error")
            status_text = "⏳ Заказ обрабатывается"
        else:
            order = await db.update_order_status(order.id, "failed")
            status_text = f"❌ Ошибка API: {api_response.get('error')}"
    else:
        order = await db.update_order_status(order.id, "processing")
        status_text = "⏳ Заказ обрабатывается"
    
    user = await db.get_user(tg_id=user_id)
    formatted_balance = format_balance(user.balance)
    
    await bot.send_message(
        chat_id,
        f"✅ <b>Заказ создан!</b>\n\n"
        f"📋 Номер заказа: <b>{order.order_code}</b>\n"
        f"🔧 Услуга: {service_name}\n"
        f"🔢 Количество: {quantity}\n"
        f"💰 Сумма: {total_sell_price:.2f}₽\n"
        f"💵 Баланс: {formatted_balance}₽\n\n"
        f"{status_text}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(user_id)
    )

    # Сбрасываем скидку после успешного заказа
    if data.get('discount_applied', 0) > 0:
        await db.reset_user_discount(tg_id=user_id)
        print(f"Скидка для пользователя {user_id} была сброшена после заказа.")

    await state.clear()

async def check_payment_and_create_order(order_code: str, payment_data: dict, user_id: int, state: FSMContext):
    """Проверка оплаты и создание заказа после пополнения"""
    for _ in range(120):
        await asyncio.sleep(5)
        try:
            is_paid = await payment_handler.check_payment(payment_data)
            if is_paid:
                payments = await db.get_payments(user_id)
                if payments:
                    latest = payments[0]
                    if latest.order == order_code and latest.status == 'paid':
                        # Получаем данные заказа из state
                        data = await state.get_data()
                        if data.get('pending_order'):
                            # Автоматически создаем заказ
                            user = await db.get_user(tg_id=user_id)
                            total_sell_price = data.get('total_sell_price', 0)
                            total_buy_price = data.get('total_buy_price', 0)
                            quantity = data.get('quantity', 0)
                            
                            service = await db.get_service_by_id(data.get('service_id', 0))
                            service_api_id = service.service_api_id if service else None

                            if user.balance >= total_sell_price:
                                # Списываем с баланса
                                await db.add_balance(tg_id=user_id, amount=-total_sell_price)
                                
                                # Создаем заказ через API (если не DEBUG)
                                api_order_id = None
                                api_response = None
                                if not DEBUG and smmway_api and service_api_id:
                                    try:
                                        if service_api_id:
                                            api_response = await smmway_api.create_order(
                                                service_id=service_api_id,
                                                link=data.get('link', ''),
                                                quantity=quantity
                                            )
                                            if api_response.get("success") or api_response.get("order"):
                                                api_order_id = api_response.get("order_id") or api_response.get("order")
                                                print(f"Заказ создан в API smmway.ru: {api_order_id}")
                                    except Exception as e:
                                        print(f"Ошибка при создании заказа через API: {e}")
                                
                                # Создаем заказ в БД
                                order_code_new = generate_secure_random_string()
                                order = await db.create_order(
                                    tg_id=user_id,
                                    service=f"{data.get('app_name', '')} - {data.get('service_name', '')}",
                                    order_code=order_code_new,
                                    quantity=quantity,
                                    link=data.get('link', ''),
                                    service_api_id=service_api_id,
                                    api_order_id=api_order_id,
                                    price_buy=total_buy_price,
                                    price_sell=total_sell_price
                                )
                                
                                # Начисляем рефералу если есть
                                if user.referrer_id:
                                    await db.add_referral_earnings(
                                        referrer_id=user.referrer_id,
                                        amount=float(total_sell_price),
                                        referral_percent=REFERRAL_PERCENT
                                    )
                                    # Уведомляем реферера
                                    try:
                                        referrer = await db.get_user(tg_id=user.referrer_id)
                                        if referrer:
                                            earnings = float(total_sell_price) * (REFERRAL_PERCENT / 100)
                                            await bot.send_message(
                                                referrer.tg_id,
                                                f"💰 <b>Начисление с реферала!</b>\n\n"
                                                f"Сумма: <b>+{earnings:.2f}₽</b>\n"
                                                f"От покупки на {total_sell_price:.2f}₽",
                                                parse_mode="HTML"
                                            )
                                    except:
                                        pass
                                
                                # Обновляем статус
                                status_text = ""
                                if DEBUG:
                                    order = await db.update_order_status(order.id, "completed")
                                    status_text = "✅ Заказ создан (DEBUG режим)"
                                elif api_response and ("error" in api_response):
                                    error_text = api_response.get('error', '').lower()
                                    if 'funds' in error_text or 'balance' in error_text or 'средств' in error_text:
                                        order = await db.update_order_status(order.id, "api_error")
                                        status_text = "⏳ Заказ обрабатывается"
                                    else:
                                        order = await db.update_order_status(order.id, "failed")
                                        status_text = f"❌ Ошибка API: {api_response.get('error')}"
                                else:
                                    order = await db.update_order_status(order.id, "processing")
                                    status_text = "⏳ Заказ обрабатывается"
                                
                                user = await db.get_user(tg_id=user_id)
                                await bot.send_message(
                                    user_id,
                                    f"✅ <b>Платеж получен, заказ автоматически создан!</b>\n\n"
                                    f"📋 Номер заказа: <b>{order.order_code}</b>\n"
                                    f"🔧 Услуга: {data.get('service_name', '')}\n"
                                    f"🔢 Количество: {quantity}\n"
                                    f"💰 Сумма: {total_sell_price:.2f}₽\n"
                                    f"💵 Баланс: {format_balance(user.balance)}₽\n\n"
                                    f"{status_text}",
                                    parse_mode="HTML"
                                )
                                
                                # Сбрасываем скидку после успешного заказа
                                if data.get('discount_applied', 0) > 0:
                                    await db.reset_user_discount(tg_id=user_id)
                                    print(f"Скидка для пользователя {user_id} была сброшена после заказа (авто-пополнение).")

                                await state.clear()
                            else:
                                await bot.send_message(
                                    user_id,
                                    "✅ Платеж получен! Теперь вы можете создать заказ через каталог.",
                                    parse_mode="HTML"
                                )
                        break
        except Exception as e:
            print(f"Ошибка при проверке платежа: {e}")
            continue

@dp.callback_query(F.data == "catalog_back")
async def catalog_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    apps = await db.get_apps_sorted_telegram_first()
    if not apps:
        await callback.message.edit_text(
            "📦 <b>Каталог услуг</b>\n\nКаталог пуст. Обратитесь к администратору.",
            parse_mode="HTML", reply_markup=get_back_kb()
        )
        await callback.answer()
        return
    await callback.message.edit_text(
        "📦 <b>Каталог услуг</b>\n\nВыберите приложение (Telegram, Instagram и др.):",
        parse_mode="HTML",
        reply_markup=await _build_catalog_apps_keyboard(apps, 1)
    )
    await callback.answer()
@dp.message(F.text == "👤 Профиль")
async def profile_handler(message: types.Message, state: FSMContext):
    await state.clear() # Сбрасываем состояние FSM
    await message.delete() # Удаляем сообщение пользователя
    user = await db.get_user(tg_id=message.from_user.id)
    if not user:
        user = await db.create_user(
            tg_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )
    else:
        await db.update_user_names(
            tg_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name
        )
    
    text = format_profile_text(message.from_user.id, message.from_user.first_name, user)
    await message.answer(
        text,
        reply_markup=await get_profile_kb(user.balance),
        parse_mode="HTML"
    )

@dp.message(F.text == "ℹ️ О проекте")
async def about_handler(message: types.Message, state: FSMContext):
    await state.clear() # Сбрасываем состояние FSM
    await message.delete() # Удаляем сообщение пользователя
    
    support_list = await db.get_support_accounts() or SUPPORT_USERNAMES
    support_links = "\n".join([f"@{username}" for username in support_list])

    kb = InlineKeyboardBuilder()
    if _is_valid_url(PROJECT_INFO_URL):
        kb.row(InlineKeyboardButton(text="🌐 О проекте", url=PROJECT_INFO_URL))
    if _is_valid_url(PRIVACY_POLICY_URL):
        kb.row(InlineKeyboardButton(text="📜 Политика конфиденциальности", url=PRIVACY_POLICY_URL))
    if support_list:
        kb.row(InlineKeyboardButton(text="🆘 Написать в поддержку", url=f"https://t.me/{support_list[0]}"))
    kb.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_main'))
    
    await message.answer(
        "ℹ️ <b>О проекте</b>\n\n"
        "Контакты поддержки:\n"
        f"{support_links}\n\n"
        "Перед использованием бота убедитесь, что вы ознакомились с политикой конфиденциальности.",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )

@dp.message(F.text == "👑 Админ")
async def admin_handler(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return
    
    await state.clear() # Сбрасываем состояние FSM
    await message.delete() # Удаляем сообщение пользователя
    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear() # Сбрасываем состояние FSM
    await callback.message.delete()
    await callback.message.answer(
        "🏠 <b>Главное меню</b>\n\nВыберите действие из меню:",
        reply_markup=get_main_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "payment_method_select")
async def topup_method_select_handler(callback: types.CallbackQuery, state: FSMContext):
    """Выбор способа пополнения: CryptoBot (0%) или по реквизитам (4%)"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text='💳 CryptoBot (0% комиссия)', callback_data='dodep'),
        InlineKeyboardButton(text='🧾 По реквизитам (4% комиссия)', callback_data='manual_payment')
    )
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back'))
    await callback.message.edit_text(
        "💳 <b>Способ пополнения</b>\n\n"
        "• <b>CryptoBot</b> — комиссия 0%\n"
        "• <b>По реквизитам</b> — комиссия 4%",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "dodep")
async def start_payment(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Введите сумму пополнения (в рублях) в чат ниже.\n\n"
        "Пример: <code>1000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back')]])
    )
    await callback.message.answer("Введите сумму или нажмите '❌ Отменить'", reply_markup=get_cancel_kb())
    await state.set_state(PaymentStates.waiting_amount)
    await callback.answer()

@dp.callback_query(F.data == "manual_payment")
async def start_manual_payment(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🧾 <b>Пополнение по реквизитам</b>\n\n"
        "Введите сумму, которую хотите пополнить (в рублях), в чат ниже.\n"
        "К сумме будет добавлена комиссия <b>4%</b>.\n\n"
        "Пример: <code>1000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back')]])
    )
    await callback.message.answer("Введите сумму или нажмите '❌ Отменить'", reply_markup=get_cancel_kb())
    await state.set_state(ManualPaymentStates.entering_amount)
    await callback.answer()

@dp.message(ManualPaymentStates.entering_amount)
async def process_manual_payment_amount(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Пополнение отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    try:
        amount = float(message.text)
        if amount < 100:
            await message.answer("❌ Минимальная сумма для этого способа: 100₽", reply_markup=get_cancel_kb())
            return

        fee = amount * 0.04
        total_amount = amount + fee
        order_code = generate_secure_random_string()

        await db.create_manual_payment(message.from_user.id, amount, fee, order_code)

        await message.answer(
            f"✅ <b>Заявка на пополнение создана!</b>\n\n"
            f"Номер заявки: <code>{order_code}</code>\n"
            f"Сумма к оплате (с комиссией): <b>{total_amount:.2f}₽</b>\n\n"
            f"Пожалуйста, свяжитесь с поддержкой ({", ".join([f"@{u}" for u in (await db.get_support_accounts()) or SUPPORT_USERNAMES])}), чтобы получить реквизиты для оплаты. "
            f"После оплаты отправьте менеджеру чек и номер вашей заявки.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число.", reply_markup=get_cancel_kb())

@dp.message(PaymentStates.waiting_amount)
async def process_payment_amount(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Пополнение отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        amount = float(message.text)
        if amount < 10:
            await message.answer("❌ Минимальная сумма пополнения: 10₽", reply_markup=get_cancel_kb())
            return
        if amount > 100000:
            await message.answer("❌ Максимальная сумма пополнения: 100,000₽", reply_markup=get_cancel_kb())
            return
        
        order_code = generate_secure_random_string()
        payment_data = {
            'user_id': message.from_user.id,
            'description': 'Пополнение баланса',
            'amount': f'{int(amount)}R',
            'order': order_code
        }
        
        await message.answer("⏳ Создаю счет для оплаты...", reply_markup=ReplyKeyboardRemove())
        
        success, result = await payment_handler.create_invoice(payment_data)
        
        if success:
            await message.answer(
                f"💳 <b>Счет для оплаты создан</b>\n\n"
                f"📋 Номер заказа: <code>{order_code}</code>\n"
                f"💰 Сумма: <b>{int(amount)}₽</b>\n\n"
                f"Ссылка для оплаты:\n{result}",
                parse_mode="HTML",
                reply_markup=get_back_kb()
            )
            
            # Запускаем проверку оплаты в фоне
            asyncio.create_task(check_payment_background(order_code, payment_data))
        else:
            await message.answer(
                f"❌ Ошибка при создании счета:\n{result}",
                reply_markup=get_back_kb()
            )
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число.", reply_markup=get_cancel_kb())

async def check_payment_background(order_code: str, payment_data: dict):
    """Фоновая проверка оплаты"""
    for _ in range(120):  # Проверяем 10 минут (120 * 5 сек)
        await asyncio.sleep(5)
        try:
            is_paid = await payment_handler.check_payment(payment_data)
            if is_paid:
                payments = await db.get_payments(payment_data['user_id'])
                if payments:
                    latest = payments[0]
                    if latest.order == order_code and latest.status == 'paid':
                        user = await db.get_user(tg_id=payment_data['user_id'])
                        if user:
                            try:
                                await bot.send_message(
                                    payment_data['user_id'],
                                    f"✅ <b>Платеж успешно обработан!</b>\n\n"
                                    f"💰 Сумма: <b>{payment_data['amount'][:-1]}₽</b>\n"
                                    f"📋 Заказ: <code>{order_code}</code>\n\n"
                                    f"Ваш баланс пополнен!",
                                    parse_mode="HTML"
                                )
                            except:
                                pass
                        break
        except Exception as e:
            print(f"Ошибка при проверке платежа: {e}")
            continue

@dp.callback_query(F.data == "balance_info")
async def balance_info(callback: types.CallbackQuery):
    user = await db.get_user(tg_id=callback.from_user.id)
    balance = user.balance if user else 0
    
    await callback.answer(
        f"Ваш текущий баланс: {format_balance(balance)}₽",
        show_alert=True
    )

@dp.callback_query(F.data.startswith("my_orders_page_"))
async def show_my_orders(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[-1])
    await callback.message.edit_text("🛍 <b>Мои заказы</b>\n\nЗагружаю...", parse_mode="HTML")
    orders = await db.get_orders(callback.from_user.id)
    
    if not orders:
        await callback.message.edit_text(
            "🛍 <b>Мои заказы</b>\n\n"
            "У вас пока нет заказов.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back')]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔍 Найти заказ', callback_data='user_find_order'))
    
    for order in orders[(page - 1) * ITEMS_PER_PAGE:page * ITEMS_PER_PAGE]:
        builder.row(InlineKeyboardButton(text=f"Заказ {order.order_code}", callback_data=f"user_view_order_{order.id}"))
    
    nav_buttons = []
    total_pages = (len(orders) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"my_orders_page_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"my_orders_page_{page+1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back'))
    
    await callback.message.edit_text("🛍 <b>Мои заказы</b>\n\nВыберите заказ для просмотра или воспользуйтесь поиском:", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("user_view_order_"))
async def user_view_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = await db.get_order_by_id(order_id)
    
    user = await db.get_user(callback.from_user.id)
    if not order or order.user_id != user.id:
        await callback.answer("❌ Заказ не найден.", show_alert=True)
        return

    status = await format_order_status(order.status)
    date = (order.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"📦 <b>Детали заказа {order.order_code}</b>\n\n"
            f"Услуга: {order.service}\n"
            f"Количество: {order.quantity}\n"
            f"Ссылка: {order.link or 'Нет'}\n"
            f"Сумма: {order.price_sell:.2f}₽\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад к заказам', callback_data='my_orders_page_1'))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "user_find_order")
async def user_find_order_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔍 <b>Поиск заказа</b>\n\n"
        "Введите код вашего заказа для поиска (например, #ABC12345):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='my_orders_page_1')]])
    )
    await state.set_state(UserSearchStates.find_order)
    await callback.answer()

@dp.message(UserSearchStates.find_order)
async def user_find_order_process(message: types.Message, state: FSMContext):
    await state.clear()
    await message.delete() # Clean up
    order_code = message.text.strip()

    order = await db.get_order_by_code(order_code)

    user = await db.get_user(message.from_user.id)
    if not order or order.user_id != user.id:
        await message.answer(
            f"❌ Заказ с кодом <code>{order_code}</code> не найден в вашей истории.",
            parse_mode="HTML"
        )
        return

    status = await format_order_status(order.status)
    date = (order.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"📦 <b>Детали заказа {order.order_code}</b>\n\n"
            f"Услуга: {order.service}\n"
            f"Количество: {order.quantity}\n"
            f"Ссылка: {order.link or 'Нет'}\n"
            f"Сумма: {order.price_sell:.2f}₽\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад к заказам', callback_data='my_orders_page_1'))
    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("my_payments_page_"))
async def show_my_payments(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[-1])
    await callback.message.edit_text("💳 <b>Мои пополнения</b>\n\nЗагружаю...", parse_mode="HTML")
    payments = await db.get_payments(callback.from_user.id)
    
    if not payments:
        await callback.message.edit_text(
            "💳 <b>Мои пополнения</b>\n\n"
            "У вас пока нет пополнений.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back')]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔍 Найти пополнение', callback_data='user_find_payment'))
    
    for payment in payments[(page - 1) * ITEMS_PER_PAGE:page * ITEMS_PER_PAGE]:
        builder.row(InlineKeyboardButton(text=f"Пополнение {payment.order}", callback_data=f"user_view_payment_{payment.id}"))
    
    nav_buttons = []
    total_pages = (len(payments) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"my_payments_page_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"my_payments_page_{page+1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back'))
    
    await callback.message.edit_text("💳 <b>Мои пополнения</b>\n\nВыберите пополнение для просмотра или воспользуйтесь поиском:", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("user_view_payment_"))
async def user_view_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split("_")[3])
    payment = await db.get_payment_by_id(payment_id)
    
    if not payment or payment.tg_id != callback.from_user.id:
        await callback.answer("❌ Пополнение не найдено.", show_alert=True)
        return

    status = await format_payment_status(payment.status)
    date = (payment.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"💵 <b>Детали пополнения {payment.order}</b>\n\n"
            f"Сумма: <b>{payment.amount}</b>\n"
            f"Метод: {payment.method or 'N/A'}\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад к пополнениям', callback_data='my_payments_page_1'))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "user_find_payment")
async def user_find_payment_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔍 <b>Поиск пополнения</b>\n\n"
        "Введите код вашего пополнения для поиска (например, #ABC12345):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='my_payments_page_1')]])
    )
    await state.set_state(UserSearchStates.find_payment)
    await callback.answer()

@dp.message(UserSearchStates.find_payment)
async def user_find_payment_process(message: types.Message, state: FSMContext):
    await state.clear()
    await message.delete() # Clean up
    payment_code = message.text.strip()

    payment = await db.get_payment_by_order_code(payment_code)

    if not payment or payment.tg_id != message.from_user.id:
        await message.answer(
            f"❌ Пополнение с кодом <code>{payment_code}</code> не найдено в вашей истории.",
            parse_mode="HTML"
        )
        return

    status = await format_payment_status(payment.status)
    date = (payment.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"💵 <b>Детали пополнения {payment.order}</b>\n\n"
            f"Сумма: <b>{payment.amount}</b>\n"
            f"Метод: {payment.method or 'N/A'}\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад к пополнениям', callback_data='my_payments_page_1'))
    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "enter_promocode")
async def enter_promocode_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🎁 <b>Ввод промокода</b>\n\n"
        "Введите ваш промокод:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='profile_back')]])
    )
    await state.set_state(PromoCodeStates.entering_code)
    await callback.answer()

@dp.message(PromoCodeStates.entering_code)
async def process_promocode(message: types.Message, state: FSMContext):
    await message.delete()
    await state.clear()
    code_text = message.text.strip().upper()

    promo = await db.get_promocode_by_code(code_text)
    if not promo:
        await message.answer("❌ Промокод не найден.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    if promo.used_activations >= promo.total_activations:
        await message.answer("❌ К сожалению, лимит активаций этого промокода исчерпан.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    user = await db.get_user(tg_id=message.from_user.id)
    used_codes = user.used_promocodes.split(',') if user.used_promocodes else []

    if promo.code in used_codes:
        await message.answer("❌ Вы уже использовали этот промокод.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    # Применяем промокод
    await db.apply_promocode_to_user(message.from_user.id, promo)

    await message.answer(
        f"✅ <b>Промокод успешно активирован!</b>\n\n"
        f"Вам предоставлена скидка <b>{promo.discount}%</b>.\n"
        f"Она будет автоматически применяться к вашим заказам.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

    # Обновляем сообщение профиля, чтобы показать скидку
    user = await db.get_user(tg_id=message.from_user.id)
    text = format_profile_text(message.from_user.id, message.from_user.first_name, user)
    await message.answer(
        text,
        reply_markup=await get_profile_kb(user.balance),
        parse_mode="HTML"
    )

# ======================== Admin Handlers =========================

USERS_PER_PAGE = 8

async def _build_users_keyboard(users: list, page: int, total_pages: int, search_q: str = ""):
    builder = InlineKeyboardBuilder()
    for u in users:
        label = f"{u.tg_id}"
        if getattr(u, 'username', None):
            label = f"@{u.username}"
        elif getattr(u, 'first_name', None):
            label = f"{u.first_name[:15]}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"admin_user_{u.tg_id}"))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_page_{page-1}_{search_q}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_page_{page+1}_{search_q}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_users_search"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin"))
    return builder.as_markup()

@dp.callback_query(F.data == "admin_users")
@dp.callback_query(F.data.startswith("admin_users_page_"))
async def admin_users_handler(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    page = 1
    search_q = ""
    if callback.data.startswith("admin_users_page_"):
        parts = callback.data.replace("admin_users_page_", "").split("_", 1)
        page = int(parts[0])
        search_q = parts[1] if len(parts) > 1 else ""
    if search_q:
        users = await db.search_users(search_q)
    else:
        users = await db.get_all_users()
    if not users:
        await callback.message.edit_text(
            "👥 <b>Пользователи</b>\n\nНичего не найдено.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]])
        )
        await callback.answer()
        return
    total_pages = max(1, (len(users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = min(page, total_pages)
    start = (page - 1) * USERS_PER_PAGE
    chunk = users[start:start + USERS_PER_PAGE]
    sub = f" (поиск: {search_q})" if search_q else ""
    await callback.message.edit_text(
        f"👥 <b>Пользователи</b>{sub}\n\nВыберите пользователя:",
        parse_mode="HTML",
        reply_markup=await _build_users_keyboard(chunk, page, total_pages, search_q)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_users_search")
async def admin_users_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🔍 Введите ID или @username для поиска:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_users")]])
    )
    await state.set_state(AdminStates.user_search_query)
    await callback.answer()

@dp.message(AdminStates.user_search_query)
async def admin_users_search_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.delete()
    q = (message.text or "").strip()
    users = await db.search_users(q)
    if not users:
        await message.answer(
            "Ничего не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 К списку", callback_data="admin_users")]])
        )
        return
    total_pages = max(1, (len(users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    chunk = users[:USERS_PER_PAGE]
    await message.answer(
        f"👥 <b>Пользователи</b> (поиск: {q})\n\nВыберите пользователя:",
        parse_mode="HTML",
        reply_markup=await _build_users_keyboard(chunk, 1, total_pages, q)
    )

@dp.callback_query(F.data.startswith("admin_user_"))
async def admin_user_card(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    # Ожидаем формат admin_user_<tg_id>, игнорируем другие (admin_user_msg_, admin_user_balance_ и т.п.)
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer()
        return
    tg_id = int(parts[2])
    user = await db.get_user(tg_id=tg_id)
    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    banned = bool(getattr(user, 'is_banned', 0))
    role = user.role or "user"
    uname = f"@{getattr(user, 'username', None) or '—'}"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔨 Забанить" if not banned else "✅ Разбанить", callback_data=f"admin_user_ban_{tg_id}"),
        InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_user_msg_{tg_id}")
    )
    builder.row(
        InlineKeyboardButton(text="💰 Добавить баланс", callback_data=f"admin_user_balance_{tg_id}"),
        InlineKeyboardButton(text="👑 Админ" if role != "admin" else "👤 Снять админа", callback_data=f"admin_user_admin_{tg_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🛍 Заказы", callback_data=f"admin_user_orders_{tg_id}"),
        InlineKeyboardButton(text="💳 Пополнения", callback_data=f"admin_user_payments_{tg_id}")
    )
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="admin_users"))
    text = (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Username: {uname}\n"
        f"Баланс: {format_balance(user.balance)}₽\n"
        f"Роль: {role}\n"
        f"Бан: {'Да' if banned else 'Нет'}\n"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_user_ban_"))
async def admin_user_ban_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    user = await db.get_user(tg_id=tg_id)
    if not user:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    banned = bool(getattr(user, 'is_banned', 0))
    await db.set_user_ban(tg_id, not banned)
    _invalidate_banned_cache(tg_id)
    await callback.answer(f"{'Разбанен' if banned else 'Забанен'}", show_alert=True)
    callback.data = f"admin_user_{tg_id}"
    await admin_user_card(callback, state)

@dp.callback_query(F.data.startswith("admin_user_msg_"))
async def admin_user_msg_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    await state.update_data(admin_msg_target=tg_id)
    await callback.message.edit_text(
        f"✉️ Введите сообщение для пользователя {tg_id} (будет с префиксом «Поддержка:»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"admin_user_{tg_id}")]])
    )
    await state.set_state(AdminStates.user_send_message_text)
    await callback.answer()

@dp.message(AdminStates.user_send_message_text)
async def admin_user_msg_send(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = data.get("admin_msg_target")
    await state.clear()
    await message.delete()
    if not tg_id:
        await message.answer("Ошибка", reply_markup=get_admin_keyboard())
        return
    text = f"Поддержка: {message.text or ''}"
    try:
        await bot.send_message(tg_id, text)
        await message.answer(f"✅ Сообщение отправлено пользователю {tg_id}", reply_markup=get_admin_keyboard())
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("admin_user_balance_"))
async def admin_user_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    await state.update_data(admin_balance_target=tg_id)
    await callback.message.edit_text(
        f"💰 Введите сумму для добавления пользователю {tg_id}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"admin_user_{tg_id}")]])
    )
    await state.set_state(AdminStates.user_add_balance_amount)
    await callback.answer()

@dp.message(AdminStates.user_add_balance_amount)
async def admin_user_balance_process(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = data.get("admin_balance_target")
    await state.clear()
    await message.delete()
    if not tg_id:
        await message.answer("Ошибка", reply_markup=get_admin_keyboard())
        return
    try:
        amount = int(float(message.text or 0))
        if amount <= 0:
            await message.answer("❌ Введите положительное число")
            return
        await db.add_balance(tg_id=tg_id, amount=amount)
        await db.log_action(message.from_user.id, tg_id, "add_balance", f"Добавлено {amount}₽", amount)
        try:
            await bot.send_message(tg_id, f"Поддержка: Ваш баланс пополнен на {amount}₽")
        except: pass
        await message.answer(f"✅ Баланс пользователя {tg_id} пополнен на {amount}₽", reply_markup=get_admin_keyboard())
    except ValueError:
        await message.answer("❌ Введите число", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("admin_user_admin_"))
async def admin_user_admin_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    user = await db.get_user(tg_id=tg_id)
    if not user:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    new_role = "user" if user.role == "admin" else "admin"
    await db.set_user_role(tg_id, new_role)
    await cache_admins()
    await callback.answer(f"Роль: {new_role}", show_alert=True)
    callback.data = f"admin_user_{tg_id}"
    await admin_user_card(callback, state)

@dp.callback_query(F.data.startswith("admin_user_orders_"))
async def admin_user_orders(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    user = await db.get_user(tg_id=tg_id)
    if not user:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    orders = await db.get_orders(tg_id)
    if not orders:
        await callback.answer("Заказов нет", show_alert=True)
        return
    text = f"🛍 Заказы пользователя {tg_id}:\n\n"
    for o in orders[:10]:
        text += f"• {o.order_code} — {o.service} — {o.status}\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти заказ", callback_data="admin_find_order"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_{tg_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_user_payments_"))
async def admin_user_payments(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    tg_id = int(callback.data.split("_")[3])
    payments = await db.get_payments(tg_id)
    if not payments:
        await callback.answer("Пополнений нет", show_alert=True)
        return
    text = f"💳 Пополнения пользователя {tg_id}:\n\n"
    for p in payments[:10]:
        text += f"• {p.order} — {p.amount} — {p.status}\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Найти пополнение", callback_data="admin_find_payment"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_{tg_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    await state.clear() # Сбрасываем состояние FSM
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    stats = await db.get_statistics()
    formatted_revenue = format_balance(int(stats['revenue']))
    
    text = f"""
📊 <b>Статистика бота</b>

👥 Всего пользователей: <b>{stats['users']}</b>
👤 Активных пользователей: <b>{stats.get('active_users', 0)}</b>
🛍 Всего заказов: <b>{stats['orders']}</b>
💳 Оплаченных пополнений: <b>{stats['payments']}</b>
💰 Общий доход: <b>{formatted_revenue}₽</b>
"""
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data.startswith("admin_orders_page_"))
async def admin_orders(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[-1])
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    orders = await db.get_all_orders()
    
    if not orders:
        await callback.message.edit_text(
            "🛍 <b>Все заказы</b>\n\nЗаказов пока нет.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔍 Найти заказ', callback_data='admin_find_order'))
    start_index = (page - 1) * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    
    for order in orders[start_index:end_index]:
        builder.row(InlineKeyboardButton(text=f"Заказ {order.order_code}", callback_data=f"admin_view_order_{order.id}"))
    
    nav_buttons = []
    total_pages = (len(orders) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_orders_page_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_orders_page_{page+1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin'))
    
    await callback.message.edit_text(f"🛍 <b>Все заказы (Стр. {page})</b>\n\nВыберите заказ для просмотра или воспользуйтесь поиском:", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("admin_view_order_"))
async def admin_view_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = await db.get_order_by_id(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    status = await format_order_status(order.status)
    date = (order.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    user = await db.get_user_by_db_id(order.user_id)
    
    text = (f"📦 <b>Детали заказа {order.order_code}</b>\n\n"
            f"ID Пользователя: <code>{user.tg_id if user else 'Не найден'}</code>\n"
            f"Услуга: {order.service}\n"
            f"Кол-во: {order.quantity}\n"
            f"Ссылка: {order.link or 'Нет'}\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)\n")
    if order.price_sell is not None and order.price_buy is not None:
        profit = order.price_sell - order.price_buy
        text += f"Продажа: {order.price_sell:.2f}₽ | Покупка: {order.price_buy:.2f}₽ | <b>Профит: {profit:.2f}₽</b>\n"

    builder = InlineKeyboardBuilder()
    action_buttons = []

    if order.status == 'api_error':
        action_buttons.append(InlineKeyboardButton(text="⚙️ Повторить", callback_data=f"admin_retry_order_{order.id}"))

    if order.status not in ['completed', 'cancelled']:
        action_buttons.append(InlineKeyboardButton(text="✅ Завершить", callback_data=f"admin_complete_order_{order.id}"))

    if order.status not in ['completed', 'cancelled']:
        action_buttons.append(InlineKeyboardButton(text="❌ Отменить", callback_data=f"admin_cancel_order_{order.id}"))

    if action_buttons:
        builder.row(*action_buttons)
    
    builder.row(InlineKeyboardButton(text='🔙 Назад к заказам', callback_data='admin_orders_page_1'))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_retry_order_"))
async def admin_retry_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = await db.get_order_by_id(order_id)
    if not order or not smmway_api:
        await callback.answer("❌ Заказ не найден или API недоступен", show_alert=True)
        return
    
    await callback.message.edit_text(f"⏳ Повторяю запрос для заказа {order.order_code}...")
    api_response = await smmway_api.create_order(service_id=order.service_api_id, link=order.link, quantity=order.quantity)

    if api_response.get("success") or api_response.get("order"):
        api_order_id = api_response.get("order_id") or api_response.get("order")
        await db.update_order_status(order.id, "processing")
        await callback.message.edit_text(f"✅ Заказ {order.order_code} успешно отправлен в API. Новый ID: {api_order_id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад к заказам', callback_data='admin_orders_page_1')]]))
    else:
        await callback.message.answer(f"❌ Ошибка при повторной отправке заказа {order.order_code}: {api_response.get('error', 'Неизвестная ошибка')}", show_alert=True)

@dp.callback_query(F.data.startswith("admin_complete_order_"))
async def admin_complete_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = await db.get_order_by_id(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    await db.update_order_status(order.id, "completed")
    await callback.message.edit_text(f"✅ Заказ {order.order_code} был завершен вручную.")
    
    user = await db.get_user_by_db_id(order.user_id)
    if user:
        try:
            await bot.send_message(user.tg_id, f"✅ Ваш заказ {order.order_code} был выполнен.")
        except Exception:
            pass
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_cancel_order_"))
async def admin_cancel_order(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[3])
    order = await db.get_order_by_id(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    
    if order.status in ['completed', 'cancelled']:
        await callback.answer(f"❌ Заказ уже в финальном статусе: {order.status}", show_alert=True)
        return

    user = await db.get_user_by_db_id(order.user_id)
    if not user:
        await callback.answer("❌ Пользователь заказа не найден", show_alert=True)
        return

    # Refund
    await db.add_balance(user.tg_id, order.price_sell)
    await db.update_order_status(order.id, "cancelled")

    await callback.message.edit_text(f"❌ Заказ {order.order_code} был отменен. Средства в размере {order.price_sell:.2f}₽ возвращены пользователю {user.tg_id}.")
    
    try:
        await bot.send_message(user.tg_id, f"❌ Ваш заказ {order.order_code} был отменен. Средства в размере {order.price_sell:.2f}₽ возвращены на ваш баланс.")
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_payments_page_"))
async def admin_payments(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[3])
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    payments = await db.get_all_payments()
    
    if not payments:
        await callback.message.edit_text(
            "💳 <b>Все пополнения</b>\n\nПополнений пока нет.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]])
        )
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔍 Найти пополнение', callback_data='admin_find_payment'))
    start_index = (page - 1) * ITEMS_PER_PAGE
    end_index = start_index + ITEMS_PER_PAGE
    
    for payment in payments[start_index:end_index]:
        builder.row(InlineKeyboardButton(text=f"Пополнение {payment.order}", callback_data=f"admin_view_payment_{payment.id}"))
    
    nav_buttons = []
    total_pages = (len(payments) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_payments_page_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_payments_page_{page+1}"))
        
    if nav_buttons:
        builder.row(*nav_buttons)
        
    builder.row(InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin'))
    
    await callback.message.edit_text(f"💳 <b>Все пополнения (Стр. {page})</b>\n\nВыберите пополнение для просмотра или воспользуйтесь поиском:", parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("admin_view_payment_"))
async def admin_view_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split("_")[3])
    payment = await db.get_payment_by_id(payment_id)
    if not payment:
        await callback.answer("❌ Платеж не найден", show_alert=True)
        return

    # Авто-отмена истекших ручных платежей
    if payment.payment_type == 'manual' and payment.status == 'pending' and (datetime.now(timezone.utc) - payment.created_at.replace(tzinfo=timezone.utc) > timedelta(seconds=3600)):
        payment = await db.update_payment_status(payment.order, 'expired')

    status = await format_payment_status(payment.status)
    date = (payment.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"💵 <b>Детали пополнения {payment.order}</b>\n\n"
            f"ID Пользователя: <code>{payment.tg_id}</code>\n"
            f"Сумма: <b>{payment.amount}</b>\n"
            f"Метод: {payment.method or 'N/A'}\n"
            f"Тип: {payment.payment_type}\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")

    builder = InlineKeyboardBuilder()
    if payment.payment_type == 'manual' and payment.status == 'pending':
        builder.row(
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_payment_{payment.id}"),
            InlineKeyboardButton(text="👎 Отклонить", callback_data=f"admin_decline_payment_{payment.id}")
        )
    
    builder.row(InlineKeyboardButton(text='🔙 Назад к пополнениям', callback_data='admin_payments_page_1'))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_confirm_payment_"))
async def admin_confirm_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split("_")[3])
    payment = await db.get_payment_by_id(payment_id)
    if not payment or payment.status != 'pending':
        await callback.answer("❌ Платеж не найден или уже обработан", show_alert=True)
        return

    amount_to_add = float(payment.amount.replace('₽', '')) - payment.fee
    await db.add_balance(payment.tg_id, amount_to_add)
    await db.update_payment_status(payment.order, 'paid')

    await callback.message.edit_text(f"✅ Платеж {payment.order} подтвержден. Баланс пользователя {payment.tg_id} пополнен на {amount_to_add}₽.")
    try:
        await bot.send_message(payment.tg_id, f"✅ Ваша заявка на пополнение {payment.order} на сумму {payment.amount} была подтверждена. Баланс пополнен.")
    except Exception as e:
        await callback.message.answer(f"⚠️ Не удалось уведомить пользователя {payment.tg_id}: {e}")

@dp.callback_query(F.data.startswith("admin_decline_payment_"))
async def admin_decline_payment(callback: types.CallbackQuery):
    payment_id = int(callback.data.split("_")[3])
    payment = await db.get_payment_by_id(payment_id)
    if not payment or payment.status != 'pending':
        await callback.answer("❌ Платеж не найден или уже обработан", show_alert=True)
        return

    await db.update_payment_status(payment.order, 'declined')
    await callback.message.edit_text(f"👎 Платеж {payment.order} отклонен.")
    try:
        await bot.send_message(payment.tg_id, f"❌ Ваша заявка на пополнение {payment.order} на сумму {payment.amount} была отклонена.")
    except: pass

@dp.callback_query(F.data == "admin_find_payment")
async def admin_find_payment_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 Введите код пополнения для поиска (например, #...):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]]))
    await state.set_state(AdminStates.find_payment)
    await callback.answer()

@dp.message(AdminStates.find_payment)
async def admin_find_payment_process(message: types.Message, state: FSMContext):
    await state.clear()
    await message.delete()

    payment = await db.get_payment_by_order_code(message.text.strip())
    if not payment:
        await message.answer("❌ Платеж не найден.", reply_markup=get_admin_keyboard())
        return

    if payment.payment_type == 'manual' and payment.status == 'pending' and (datetime.now(timezone.utc) - payment.created_at.replace(tzinfo=timezone.utc) > timedelta(seconds=3600)):
        payment = await db.update_payment_status(payment.order, 'expired')

    status = await format_payment_status(payment.status)
    date = (payment.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    text = (f"💵 <b>Детали пополнения {payment.order}</b>\n\n"
            f"ID Пользователя: <code>{payment.tg_id}</code>\n"
            f"Сумма: <b>{payment.amount}</b>\n"
            f"Метод: {payment.method or 'N/A'}\n"
            f"Тип: {payment.payment_type}\n"
            f"Статус: {status}\n"
            f"Дата: {date} (МСК)")

    builder = InlineKeyboardBuilder()
    if payment.payment_type == 'manual' and payment.status == 'pending':
        builder.row(
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_payment_{payment.id}"),
            InlineKeyboardButton(text="👎 Отклонить", callback_data=f"admin_decline_payment_{payment.id}")
        )

    builder.row(InlineKeyboardButton(text='🔙 Назад в админ-панель', callback_data='back_to_admin'))

    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "💸 <b>Добавление баланса</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML",
        reply_markup=None
    )
    await state.set_state(AdminStates.get_user_id)

@dp.message(AdminStates.get_user_id)
async def admin_get_user_id(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        user_id = int(message.text)
        user = await db.get_user(tg_id=user_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Попробуйте еще раз:", reply_markup=get_cancel_kb())
            return
        
        await state.update_data(user_id=user_id)
        await message.answer(
            f"👤 Пользователь найден: {user_id}\n\n"
            "Введите сумму для добавления (в рублях):",
            reply_markup=get_cancel_kb()
        )
        await state.set_state(AdminStates.add_money_amount)
    except ValueError:
        await message.answer("❌ Введите корректный ID пользователя (число):", reply_markup=get_cancel_kb())

@dp.message(AdminStates.add_money_amount)
async def admin_add_money_amount(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        amount = int(float(message.text))
        data = await state.get_data()
        user_id = data['user_id']
        
        await db.add_balance(tg_id=user_id, amount=amount)
        await db.log_action(
            admin_id=message.from_user.id,
            user_id=user_id,
            action="add_balance",
            description=f"Добавлено {amount}₽",
            amount=float(amount)
        )
        
        user = await db.get_user(tg_id=user_id)
        await message.answer(
            f"✅ Баланс успешно добавлен!\n\n"
            f"👤 Пользователь: {user_id}\n"
            f"💰 Добавлено: {amount}₽\n"
            f"💵 Новый баланс: {format_balance(user.balance)}₽",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"💰 <b>Баланс пополнен администратором</b>\n\n"
                f"Сумма: <b>+{amount}₽</b>\n"
                f"Текущий баланс: <b>{format_balance(user.balance)}₽</b>",
                parse_mode="HTML"
            )
        except:
            pass
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректное число:", reply_markup=get_cancel_kb())

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "📩 <b>Рассылка</b>\n\n"
        "Введите сообщение для рассылки всем пользователям:",
        parse_mode="HTML",
        reply_markup=None
    )
    await callback.message.answer(
        "Введите сообщение или нажмите '❌ Отменить':",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(AdminStates.broadcast_message)

@dp.message(AdminStates.broadcast_message)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Рассылка отменена.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    await message.answer("⏳ Начинаю рассылку...", reply_markup=ReplyKeyboardRemove())
    
    # Получаем всех пользователей
    users = await db.get_all_users()
    
    sent = 0
    failed = 0
    
    for user in users:
        try:
            await bot.send_message(user.tg_id, message.text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)  # Защита от лимитов
        except:
            failed += 1
    
    await message.answer(
        f"✅ Рассылка завершена!\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        reply_markup=get_main_keyboard(message.from_user.id)
    )
    
    await state.clear()

# ======================== Новые админ функции =========================

@dp.callback_query(F.data == "admin_add_admin")
async def admin_add_admin_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "👑 <b>Добавление администратора</b>\n\n"
        "Введите Telegram ID пользователя для назначения администратором:",
        parse_mode="HTML",
        reply_markup=None
    )
    await callback.message.answer(
        "Введите ID или нажмите '❌ Отменить':",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(AdminStates.add_admin_user)

@dp.message(AdminStates.add_admin_user)
async def admin_add_admin_process(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        user_id = int(message.text)
        user = await db.get_user(tg_id=user_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Попробуйте еще раз:", reply_markup=get_cancel_kb())
            return
        
        await db.set_user_role(tg_id=user_id, role="admin")
        await db.log_action(
            admin_id=message.from_user.id,
            user_id=user_id,
            action="add_admin",
            description=f"Назначен администратором"
        )
        await cache_admins()
        
        await message.answer(
            f"✅ Пользователь {user_id} назначен администратором!",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        
        try:
            await bot.send_message(
                user_id,
                "👑 <b>Вы назначены администратором!</b>\n\n"
                "Теперь у вас есть доступ к админ-панели.",
                parse_mode="HTML"
            )
        except:
            pass
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный ID пользователя (число):", reply_markup=get_cancel_kb())

@dp.callback_query(F.data == "admin_find_order")
async def admin_find_order_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🔍 <b>Поиск заказа</b>\n\n"
        "Введите код заказа для поиска (например, #ABC12345):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]])
    )
    await state.set_state(AdminStates.find_order)
    await callback.answer()

@dp.message(AdminStates.find_order)
async def admin_find_order_process(message: types.Message, state: FSMContext):
    await message.delete()
    await state.clear()
    order_code = message.text.strip()
    
    order = await db.get_order_by_code(order_code)
    
    if not order:
        await message.answer(
            f"❌ Заказ с кодом <code>{order_code}</code> не найден.",
            parse_mode="HTML",
            reply_markup=get_admin_keyboard()
        )
        return
    
    status = await format_order_status(order.status)
    date = (order.created_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M") if order.created_at else "N/A"
    
    text = "✅ <b>Заказ найден</b>\n\n"
    text += f"📦 <b>Заказ {order.order_code}</b> (ID: {order.id})\n"
    text += f"   👤 Пользователь: <code>{order.user_id}</code>\n"
    text += f"   Услуга: {order.service}\n"
    text += f"   Количество: {order.quantity}\n"
    text += f"   Ссылка: {order.link or 'Нет'}\n"
    if order.price_sell is not None and order.price_buy is not None:
        profit = order.price_sell - order.price_buy
        text += f"   💰 Продажа: {order.price_sell:.2f}₽ / Покупка: {order.price_buy:.2f}₽ (<b>Профит: {profit:.2f}₽</b>)\n"

    text += f"   Статус: {status}\n"
    text += f"   Дата: {date} (МСК)\n\n"
    
    await message.answer(text, parse_mode="HTML", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin_global_markup")
async def admin_global_markup_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"📈 <b>Глобальная наценка</b>\n\n"
        f"Текущая наценка: <b>{GLOBAL_MARKUP_PERCENT}%</b>\n\n"
        f"Эта наценка применяется ко всем новым товарам при синхронизации с API. "
        f"Вы также можете пересчитать цены для всех существующих товаров.\n\n"
        f"Введите новую наценку в процентах (например: 300):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]])
    )
    await state.set_state(AdminStates.set_global_markup)
    await callback.answer()

@dp.message(AdminStates.set_global_markup)
async def admin_global_markup_set(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    global GLOBAL_MARKUP_PERCENT
    
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        new_markup = float(message.text)
        if new_markup < 0:
            await message.answer("❌ Наценка не может быть отрицательной.", reply_markup=get_cancel_kb())
            return
        
        GLOBAL_MARKUP_PERCENT = new_markup
        
        await message.answer(f"⏳ Обновляю цены для всех товаров с наценкой {new_markup}%...")
        updated_count = await db.recalculate_all_prices(new_markup)
        
        await message.answer(
            f"✅ Глобальная наценка обновлена до <b>{new_markup}%</b>.\n"
            f"Обновлено цен: <b>{updated_count}</b>.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректное число:", reply_markup=get_cancel_kb())

@dp.callback_query(F.data == "admin_referral")
async def admin_referral_settings(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    global REFERRAL_PERCENT
    
    await callback.message.edit_text(
        f"👥 <b>Настройки реферальной программы</b>\n\n"
        f"Текущий процент: <b>{REFERRAL_PERCENT}%</b>\n\n"
        f"Введите новый процент от покупок рефералов (например: 10):",
        parse_mode="HTML",
        reply_markup=None
    )
    await callback.message.answer(
        "Введите процент или нажмите '❌ Отменить':",
        reply_markup=get_cancel_kb()
    )
    await state.set_state(AdminStates.set_referral_percent)

@dp.message(AdminStates.set_referral_percent)
async def admin_referral_set(message: types.Message, state: FSMContext):
    await message.delete() # Удаляем сообщение пользователя
    global REFERRAL_PERCENT
    
    if message.text == "❌ Отменить":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    
    try:
        percent = float(message.text)
        if percent < 0 or percent > 100:
            await message.answer("❌ Процент должен быть от 0 до 100. Попробуйте еще раз:", reply_markup=get_cancel_kb())
            return
        
        REFERRAL_PERCENT = percent
        await message.answer(
            f"✅ Процент реферальной программы обновлен!\n\n"
            f"Новый процент: <b>{percent}%</b>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректное число:", reply_markup=get_cancel_kb())

@dp.callback_query(F.data == "admin_reset_stats")
async def admin_reset_stats(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, сбросить", callback_data="confirm_reset"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_reset")
    )
    
    await callback.message.edit_text(
        "⚠️ <b>Сброс статистики</b>\n\n"
        "Это действие удалит:\n"
        "• Все заказы\n"
        "• Все платежи\n"
        "• Все логи\n"
        "• Обнулит балансы пользователей (кроме админов)\n\n"
        "<b>ВНИМАНИЕ: Это действие необратимо!</b>\n\n"
        "Вы уверены?",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset_stats(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text("⏳ Сбрасываю статистику...", reply_markup=None)
    
    await db.reset_statistics()
    await cache_admins()
    
    await callback.message.edit_text(
        "✅ <b>Статистика успешно сброшена!</b>\n\n"
        "Все данные очищены.",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset_stats(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "❌ Сброс статистики отменен.",
        reply_markup=get_admin_keyboard()
    )

@dp.callback_query(F.data == "admin_create_promocode")
async def admin_create_promocode_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🎁 <b>Создание промокода</b>\n\n"
        "Введите текст промокода (например, `SALE2024`):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='back_to_admin')]])
    )
    await state.set_state(AdminStates.create_promocode_code)
    await callback.answer()

@dp.message(AdminStates.create_promocode_code)
async def admin_create_promocode_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    existing = await db.get_promocode_by_code(code)
    if existing:
        await message.answer("❌ Промокод с таким названием уже существует. Попробуйте другой.")
        return
    
    await state.update_data(promo_code=code)
    await message.answer(f"✅ Код: `{code}`\n\nТеперь введите размер скидки в процентах (например, `15`):")
    await state.set_state(AdminStates.create_promocode_discount)

@dp.message(AdminStates.create_promocode_discount)
async def admin_create_promocode_discount(message: types.Message, state: FSMContext):
    try:
        discount = float(message.text)
        if not (0 < discount <= 100):
            await message.answer("❌ Скидка должна быть числом от 1 до 100.")
            return
        
        await state.update_data(promo_discount=discount)
        await message.answer(f"✅ Скидка: {discount}%\n\nТеперь введите количество активаций (например, `100`):")
        await state.set_state(AdminStates.create_promocode_activations)

    except ValueError:
        await message.answer("❌ Введите корректное число.")

@dp.message(AdminStates.create_promocode_activations)
async def admin_create_promocode_activations(message: types.Message, state: FSMContext):
    try:
        activations = int(message.text)
        if activations <= 0:
            await message.answer("❌ Количество активаций должно быть больше нуля.")
            return
        
        data = await state.get_data()
        code = data['promo_code']
        discount = data['promo_discount']

        await db.create_promocode(
            code=code,
            discount=discount,
            activations=activations
        )

        await message.answer(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"Скидка: <b>{discount}%</b>\n"
            f"Активаций: <b>{activations}</b>",
            parse_mode="HTML"
        )
        await state.clear()

        # Возвращаем админ-панель
        await message.answer(
            "👑 <b>Админ-панель</b>\n\nВыберите действие:",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )

    except ValueError:
        await message.answer("❌ Введите корректное число.")

SUPPORT_ACCOUNTS_PER_PAGE = 8

async def get_support_accounts_keyboard(page: int = 1):
    """Клавиатура управления аккаунтами поддержки"""
    accounts = await db.get_support_accounts()
    builder = InlineKeyboardBuilder()
    
    # Список аккаунтов (инлайн-кнопки, информационные)
    total_pages = max(1, (len(accounts) + SUPPORT_ACCOUNTS_PER_PAGE - 1) // SUPPORT_ACCOUNTS_PER_PAGE)
    start = (page - 1) * SUPPORT_ACCOUNTS_PER_PAGE
    end = start + SUPPORT_ACCOUNTS_PER_PAGE
    for u in accounts[start:end]:
        builder.row(InlineKeyboardButton(text=f"👤 @{u}", callback_data="noop"))
    
    # Пагинация
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_support_page_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_support_page_{page+1}"))
    if nav:
        builder.row(*nav)
    
    # Добавить / Удалить
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="admin_support_add"),
        InlineKeyboardButton(text="➖ Удалить", callback_data="admin_support_delete")
    )
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin"))
    return builder.as_markup()

@dp.callback_query(F.data == "admin_support_accounts")
@dp.callback_query(F.data.startswith("admin_support_page_"))
async def admin_support_accounts_handler(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    page = 1
    if callback.data.startswith("admin_support_page_"):
        page = int(callback.data.split("_")[-1])
    accounts = await db.get_support_accounts()
    text = "🆘 <b>Аккаунты поддержки</b>\n\nЗдесь указаны контакты, которые видят пользователи в разделе «Поддержка» и при ручном пополнении.\n\n"
    if accounts:
        text += f"Всего: <b>{len(accounts)}</b>\n\n"
    else:
        text += "Список пуст. Нажмите «➕ Добавить», чтобы добавить @username."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await get_support_accounts_keyboard(page))
    await callback.answer()

@dp.callback_query(F.data == "admin_support_add")
async def admin_support_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ <b>Добавить аккаунт поддержки</b>\n\n"
        "Отправьте @username пользователя (например, <code>diwo_fp</code> или <code>@diwo_fp</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_support_accounts")]])
    )
    await state.set_state(SupportAccountsStates.waiting_add_username)
    await callback.answer()

@dp.callback_query(F.data == "admin_support_delete")
async def admin_support_delete_start(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "➖ <b>Удалить аккаунт поддержки</b>\n\n"
        "Отправьте @username, который хотите удалить из списка:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_support_accounts")]])
    )
    await state.set_state(SupportAccountsStates.waiting_delete_username)
    await callback.answer()

@dp.message(SupportAccountsStates.waiting_add_username)
async def admin_support_add_process(message: types.Message, state: FSMContext):
    await message.delete()
    if message.text and message.text.strip().lower() in ("отмена", "cancel", "❌ отмена"):
        await state.clear()
        await message.answer("👑 Админ-панель", reply_markup=get_admin_keyboard())
        return
    username = (message.text or "").strip().lstrip("@")
    if not username:
        await message.answer("❌ Введите @username")
        return
    ok = await db.add_support_account(username)
    await state.clear()
    if ok:
        await message.answer(f"✅ Аккаунт @{username} добавлен.")
    else:
        await message.answer(f"❌ @{username} уже в списке или некорректное имя.")
    await message.answer("🆘 Аккаунты поддержки:", reply_markup=await get_support_accounts_keyboard(1))

@dp.message(SupportAccountsStates.waiting_delete_username)
async def admin_support_delete_process(message: types.Message, state: FSMContext):
    await message.delete()
    if message.text and message.text.strip().lower() in ("отмена", "cancel", "❌ отмена"):
        await state.clear()
        await message.answer("🆘 Аккаунты поддержки:", reply_markup=await get_support_accounts_keyboard(1))
        return
    username = (message.text or "").strip().lstrip("@")
    if not username:
        await message.answer("❌ Введите @username для удаления")
        return
    ok = await db.remove_support_account(username)
    await state.clear()
    if ok:
        await message.answer(f"✅ Аккаунт @{username} удалён.")
    else:
        await message.answer(f"❌ @{username} не найден в списке.")
    await message.answer("🆘 Аккаунты поддержки:", reply_markup=await get_support_accounts_keyboard(1))

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👑 <b>Админ-панель</b>\n\nВыберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "profile_back")
async def profile_back_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear() # Сбрасываем состояние FSM
    user = await db.get_user(tg_id=callback.from_user.id)
    text = format_profile_text(callback.from_user.id, callback.from_user.first_name, user)
    await callback.message.edit_text(
        text,
        reply_markup=await get_profile_kb(user.balance),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "referral_info")
async def referral_info(callback: types.CallbackQuery):
    user = await db.get_user(tg_id=callback.from_user.id)
    
    if not user:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    
    referral_code = user.referral_code if user.referral_code else "Не создан"
    total_earned = format_balance(user.total_earned) if user.total_earned else "0"
    
    bot_user = await bot.get_me()
    referral_link = f"https://t.me/{bot_user.username}?start={referral_code}"
    # Подсчитываем количество рефералов
    from functions.db.db import Session, User, select
    async with Session() as session:
        result = await session.execute(select(User).where(User.referrer_id == user.id))
        referrals = result.scalars().all()
        referrals_count = len(referrals)
    
    text = f"""
👥 <b>Реферальная программа</b>

🔗 Ваш реферальный код:
<code>{referral_code}</code>

📎 Ваша реферальная ссылка:
<code>{referral_link}</code>

👤 Приглашено пользователей: <b>{referrals_count}</b>
💰 Заработано с рефералов: <b>{total_earned}₽</b>
📊 Процент с покупок: <b>{REFERRAL_PERCENT}%</b>

💡 Приглашайте друзей и получайте {REFERRAL_PERCENT}% с их покупок!
"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='🔙 Назад в профиль', callback_data='profile_back'))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())

# ======================== API Helper Functions =========================
# Эти функции можно использовать для API интеграции

async def create_order_via_api(tg_id: int, service: str, quantity: int = 1) -> dict:
    """Создание заказа через API"""
    user = await db.get_user(tg_id=tg_id)
    if not user:
        return {"success": False, "error": "User not found"}
    
    order = await db.create_order(tg_id=tg_id, service=service, quantity=quantity)
    return {
        "success": True,
        "order_id": order.id,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None
    }

async def update_order_status_via_api(order_id: int, new_status: str) -> dict:
    """Обновление статуса заказа через API"""
    order = await db.update_order_status(order_id=order_id, new_status=new_status)
    if not order:
        return {"success": False, "error": "Order not found"}
    
    return {
        "success": True,
        "order_id": order.id,
        "status": order.status
    }

async def get_user_orders_via_api(tg_id: int) -> dict:
    """Получение заказов пользователя через API"""
    orders = await db.get_orders(tg_id=tg_id)
    return {
        "success": True,
        "orders": [
            {
                "id": o.id,
                "service": o.service,
                "quantity": o.quantity,
                "status": o.status,
                "created_at": o.created_at.isoformat() if o.created_at else None
            }
            for o in orders
        ]
    }

# ======================== Account Manager Commands =========================

@dp.message(Command("sync"))
async def cmd_sync(message: types.Message, state: FSMContext):
    """Команда /sync - синхронизация чатов с форумом"""
    global account_manager
    
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    if not account_manager:
        await message.answer("❌ Менеджер аккаунтов не инициализирован.")
        return
    
    # Проверяем, что команда вызвана в форуме
    if message.chat.type != 'supergroup' or not message.chat.is_forum:
        await message.answer("❌ Эта команда должна быть вызвана в форуме Telegram.")
        return
    
    # Получаем список активных аккаунтов
    accounts = await account_manager.get_active_accounts()
    if not accounts:
        await message.answer("❌ Нет активных аккаунтов. Добавьте аккаунт сначала.")
        return
    
    # Если аккаунт один, используем его, иначе нужно выбрать
    if len(accounts) == 1:
        session_name = accounts[0]
        forum_id = message.chat.id
        
        result = await account_manager.sync_chats(session_name, forum_id)
        if result.get("success"):
            await message.answer(f"✅ {result.get('message', 'Синхронизация завершена')}")
        else:
            await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
    else:
        # Нужно выбрать аккаунт
        await state.set_state(AccountManagerStates.waiting_forum_id)
        await message.answer(
            f"📱 Найдено {len(accounts)} аккаунтов. Отправьте ID форума для синхронизации."
        )

@dp.message(AccountManagerStates.waiting_forum_id)
async def process_forum_id(message: types.Message, state: FSMContext):
    """Обработка ID форума для синхронизации"""
    global account_manager
    
    try:
        forum_id = int(message.text)
        accounts = await account_manager.get_active_accounts()
        
        if len(accounts) == 1:
            session_name = accounts[0]
            result = await account_manager.sync_chats(session_name, forum_id)
            if result.get("success"):
                await message.answer(f"✅ {result.get('message', 'Синхронизация завершена')}")
            else:
                await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
        else:
            await message.answer("❌ Пока поддерживается только один аккаунт.")
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный ID форума (число).")

@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, state: FSMContext):
    """Команда /ban - бан пользователя"""
    global account_manager
    
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    if not account_manager:
        await message.answer("❌ Менеджер аккаунтов не инициализирован.")
        return
    
    # Проверяем, что команда вызвана в форуме
    if message.chat.type != 'supergroup' or not message.chat.is_forum:
        await message.answer("❌ Эта команда должна быть вызвана в форуме Telegram.")
        return
    
    args = message.text.split()[1:]
    if args:
        try:
            user_id = int(args[0])
            accounts = await account_manager.get_active_accounts()
            if accounts:
                session_name = accounts[0]
                result = await account_manager.ban_user(session_name, user_id)
                if result.get("success"):
                    await message.answer(f"✅ {result.get('message', 'Пользователь забанен')}")
                else:
                    await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
            else:
                await message.answer("❌ Нет активных аккаунтов.")
        except ValueError:
            await message.answer("❌ Использование: /ban <user_id>")
    else:
        await state.set_state(AccountManagerStates.waiting_user_id_ban)
        await message.answer("📝 Отправьте ID пользователя для бана:")

@dp.message(AccountManagerStates.waiting_user_id_ban)
async def process_ban_user_id(message: types.Message, state: FSMContext):
    """Обработка ID пользователя для бана"""
    global account_manager
    
    try:
        user_id = int(message.text)
        accounts = await account_manager.get_active_accounts()
        if accounts:
            session_name = accounts[0]
            result = await account_manager.ban_user(session_name, user_id)
            if result.get("success"):
                await message.answer(f"✅ {result.get('message', 'Пользователь забанен')}")
            else:
                await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
        else:
            await message.answer("❌ Нет активных аккаунтов.")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный ID пользователя (число).")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, state: FSMContext):
    """Команда /unban - разбан пользователя"""
    global account_manager
    
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    if not account_manager:
        await message.answer("❌ Менеджер аккаунтов не инициализирован.")
        return
    
    # Проверяем, что команда вызвана в форуме
    if message.chat.type != 'supergroup' or not message.chat.is_forum:
        await message.answer("❌ Эта команда должна быть вызвана в форуме Telegram.")
        return
    
    args = message.text.split()[1:]
    if args:
        try:
            user_id = int(args[0])
            accounts = await account_manager.get_active_accounts()
            if accounts:
                session_name = accounts[0]
                result = await account_manager.unban_user(session_name, user_id)
                if result.get("success"):
                    await message.answer(f"✅ {result.get('message', 'Пользователь разбанен')}")
                else:
                    await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
            else:
                await message.answer("❌ Нет активных аккаунтов.")
        except ValueError:
            await message.answer("❌ Использование: /unban <user_id>")
    else:
        await state.set_state(AccountManagerStates.waiting_user_id_unban)
        await message.answer("📝 Отправьте ID пользователя для разбана:")

@dp.message(AccountManagerStates.waiting_user_id_unban)
async def process_unban_user_id(message: types.Message, state: FSMContext):
    """Обработка ID пользователя для разбана"""
    global account_manager
    
    try:
        user_id = int(message.text)
        accounts = await account_manager.get_active_accounts()
        if accounts:
            session_name = accounts[0]
            result = await account_manager.unban_user(session_name, user_id)
            if result.get("success"):
                await message.answer(f"✅ {result.get('message', 'Пользователь разбанен')}")
            else:
                await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
        else:
            await message.answer("❌ Нет активных аккаунтов.")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный ID пользователя (число).")

@dp.message(Command("autoreply"))
async def cmd_autoreply(message: types.Message, state: FSMContext):
    """Команда /autoreply - установка автоответа"""
    global account_manager
    
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    if not account_manager:
        await message.answer("❌ Менеджер аккаунтов не инициализирован.")
        return
    
    # Проверяем, что команда вызвана в форуме
    if message.chat.type != 'supergroup' or not message.chat.is_forum:
        await message.answer("❌ Эта команда должна быть вызвана в форуме Telegram.")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        reply_text = args[1]
        accounts = await account_manager.get_active_accounts()
        if accounts:
            session_name = accounts[0]
            result = await account_manager.set_autoreply(session_name, reply_text)
            if result.get("success"):
                await message.answer(f"✅ {result.get('message', 'Автоответ установлен')}")
            else:
                await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
        else:
            await message.answer("❌ Нет активных аккаунтов.")
    elif len(args) == 1 and args[0] == "/autoreply":
        # Если просто /autoreply без текста - отключаем автоответ
        accounts = await account_manager.get_active_accounts()
        if accounts:
            session_name = accounts[0]
            result = await account_manager.set_autoreply(session_name, None)
            if result.get("success"):
                await message.answer(f"✅ {result.get('message', 'Автоответ отключен')}")
            else:
                await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
        else:
            await message.answer("❌ Нет активных аккаунтов.")
    else:
        await state.set_state(AccountManagerStates.waiting_autoreply)
        await message.answer("📝 Отправьте текст автоответа (или отправьте 'отключить' для отключения):")

@dp.message(AccountManagerStates.waiting_autoreply)
async def process_autoreply(message: types.Message, state: FSMContext):
    """Обработка текста автоответа"""
    global account_manager
    
    reply_text = None if message.text.lower() in ['отключить', 'off', 'disable'] else message.text
    accounts = await account_manager.get_active_accounts()
    if accounts:
        session_name = accounts[0]
        result = await account_manager.set_autoreply(session_name, reply_text)
        if result.get("success"):
            await message.answer(f"✅ {result.get('message', 'Автоответ установлен')}")
        else:
            await message.answer(f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}")
    else:
        await message.answer("❌ Нет активных аккаунтов.")
    await state.clear()

# ======================== Main =========================

async def sync_catalog_periodically():
    """Периодическая синхронизация каталога"""
    global smmway_api
    while True:
        try:
            await asyncio.sleep(CATALOG_SYNC_INTERVAL)
            if smmway_api:
                print("🔄 Синхронизация каталога...")
                result = await smmway_api.sync_catalog_to_db(GLOBAL_MARKUP_PERCENT)
                if result.get("success"):
                    print("✅ Каталог успешно синхронизирован")
                else:
                    error = result.get('error', 'Unknown error')
                    print(f"❌ Ошибка синхронизации: {error[:200]}")
                    # Если постоянная ошибка 404, не спамим логи
                    if "404" not in str(error) and "HTML" not in str(error):
                        print(f"💡 Проверьте подключение к API")
        except Exception as e:
            print(f"Ошибка при синхронизации каталога: {e}")

async def main():
    global smmway_api, account_manager, funpay_manager
    
    await db.init()
    await payment_handler.init()
    await cache_admins()
    
    # Инициализация менеджера аккаунтов
    account_manager = AccountManager(bot, db)
    await account_manager.init()
    print("✅ Менеджер аккаунтов инициализирован")
    
    # Инициализация API smmway.ru
    smmway_api = SMMWayAPI(
        api_key="k3bSD2bq49HUfTLP5Gey81eE9iEoJpoKZEU1BUukc4U07Z9Dvmx1GHoSSwwo",
        api_url="https://smmway.ru/api/v2"
    )
    await smmway_api.init()
    
    # Проверяем баланс для теста подключения
    print("🔍 Проверяю подключение к API...")
    balance_result = await smmway_api.get_balance()
    if "error" not in balance_result and "balance" in balance_result:
        print(f"✅ Подключение к API успешно! Баланс: {balance_result.get('balance')} {balance_result.get('currency')}")
    else:
        error_msg = str(balance_result.get('error', 'Unknown error'))
        print(f"❌ Проблема с подключением к API: {error_msg}")
        print(f"\n   Возможные решения:")
        print(f"   1. Проверьте документацию API: https://smmway.ru/developer")
        print(f"   2. Убедитесь, что API ключ правильный и активен.")
        print(f"   3. Убедитесь, что URL API в main.py (`{smmway_api.api_url}`) правильный.")
        print(f"\n💡 Синхронизация каталога будет пропущена. Бот продолжит работу в оффлайн-режиме.")
        smmway_api = None  # Отключаем API до исправления

    # Инициализация интеграции с FunPay (заказы -> топики форума, авто-отправка в smmway.ru)
    funpay_manager = FunPayManager(bot, config_path="funpay_config.json", smmway_api=smmway_api)
    funpay_ok = await funpay_manager.init()
    if funpay_ok:
        funpay_manager.start()
        print("✅ FunPay интеграция запущена")
    else:
        print("⚠️ FunPay интеграция не запущена (проверьте funpay_config.json)")

    # Для синхронизации каталога из API:
    if smmway_api:
        print("🔄 Первоначальная синхронизация каталога...")
        result = await smmway_api.sync_catalog_to_db(GLOBAL_MARKUP_PERCENT)
        if result.get("success"):
            print("✅ Каталог успешно синхронизирован")
        else:
            error = result.get('error', 'Unknown error')
            print(f"❌ Ошибка синхронизации: {error[:200]}")
            print(f"⚠️ API недоступен. Каталог можно заполнить вручную через админ-панель.")
        
        # Запускаем периодическую синхронизацию только если API работает
        if result.get("success"):
            asyncio.create_task(sync_catalog_periodically())
        
        # Запускаем проверку статусов заказов
        asyncio.create_task(check_order_statuses_periodically())
    else:
        print("⚠️ API smmway.ru отключен из-за ошибки подключения")
    
    dp.message.middleware(TechnicalBreakMiddleware())
    dp.callback_query.middleware(TechnicalBreakMiddleware())
    dp.message.middleware(BanCheckMiddleware())
    dp.callback_query.middleware(BanCheckMiddleware())
    dp.include_router(router)
    
    # Обработчик сообщений в форуме: сначала проверяем FunPay-заказы, затем account_manager
    @dp.message(F.chat.type == 'supergroup', F.chat.is_forum == True)
    async def handle_forum_message(message: types.Message):
        """Обработка сообщений в форуме для отправки пользователям"""
        global account_manager, funpay_manager
        
        # Проверяем, что сообщение не от бота и есть message_thread_id (топик)
        if message.from_user.is_bot:
            return
        
        if not message.message_thread_id:
            return
        
        topic_id = message.message_thread_id
        forum_chat_id = message.chat.id
        
        # 1) Если топик принадлежит заказу FunPay — ответ уходит покупателю на FunPay
        if funpay_manager and (message.text or message.caption):
            handled = await funpay_manager.handle_forum_reply(forum_chat_id, topic_id, message.text or message.caption)
            if handled:
                return
        
        # 2) Иначе — обычная логика Telegram-support через account_manager
        if not account_manager:
            return
        
        # Находим маппинг по forum и topic (определяем session автоматически)
        mapping = await account_manager.get_mapping_by_forum_and_topic(forum_chat_id, topic_id)
        if not mapping:
            return
        
        session_name = mapping.account_session
        user_id = mapping.user_tg_id
        
        if session_name not in account_manager.clients:
            return
        client = account_manager.clients[session_name]
        
        # Отправляем сообщение пользователю через Telethon
        try:
            message_text = message.text or ""
            if message.photo:
                # Скачиваем фото через бота и отправляем через Telethon
                photo_file = await bot.get_file(message.photo[-1].file_id)
                photo_path = f"data/temp_photo_{message.message_id}.jpg"
                await photo_file.download(photo_path)
                
                await client.send_file(
                    user_id,
                    photo_path,
                    caption=message_text
                )
                try:
                    os.remove(photo_path)
                except:
                    pass
            else:
                # Отправляем текстовое сообщение без клавиатуры
                await client.send_message(
                    user_id,
                    message_text,
                    buttons=None
                )
            
            # Помечаем сообщения прочитанными
            try:
                async for msg in client.iter_messages(user_id, limit=100):
                    await client.send_read_acknowledge(user_id, max_id=msg.id)
            except:
                pass
        except Exception as e:
            print(f"Ошибка отправки сообщения пользователю: {e}")
    
    print("🤖 Бот запущен!")
    if DEBUG:
        print("⚠️ DEBUG режим включен - заказы создаются только в БД")
    print(f"📊 Реферальный процент: {REFERRAL_PERCENT}%")
    print(f"🔄 Синхронизация каталога каждые {CATALOG_SYNC_INTERVAL // 60} минут. Проверка статусов заказов каждые {ORDER_STATUS_CHECK_INTERVAL // 60} минут.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
