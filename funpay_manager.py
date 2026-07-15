"""
Модуль интеграции с FunPay.

Идея та же, что в account_manager.py (Telethon <-> форум), только источник
сообщений — не Telegram-аккаунт, а FunPay:

1. Приходит новый заказ на FunPay -> создаём топик в форуме Telegram,
   сохраняем "продажу" в БД (номер заказа FunPay, покупатель, товар, цена).
2. Все сообщения из чата с покупателем на FunPay дублируются в топик форума.
3. Если покупатель пишет "!ссылка <url>" — ссылка сохраняется в БД и
   репостится в топик.
4. Если покупатель пишет "!поддержка" — в топике форума пингуются
   указанные в конфиге юзернеймы.
5. Если админ отвечает в топике форума — ответ уходит покупателю в чат
   FunPay (двусторонняя синхронизация).

Библиотека FunPayAPI (pip install FunPayAPI) синхронная и блокирующая,
поэтому её цикл прослушивания (Runner.listen) крутится в отдельном
потоке, а вызовы в сторону aiogram/БД (асинхронные) отправляются обратно
в основной event loop через run_coroutine_threadsafe.
"""

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from aiogram import Bot

from functions.db.db import Base, Column, Integer, String, Float, DateTime, select, Session, engine

try:
    from FunPayAPI import Account, Runner, enums
except ImportError:
    Account = None
    Runner = None
    enums = None


LINK_COMMAND_RE = re.compile(r'!ссылка\s+(\S+)', re.IGNORECASE)
SUPPORT_COMMAND = "!поддержка"


class FunPaySale(Base):
    """Заказ/продажа, пришедшая с FunPay."""
    __tablename__ = "funpay_sales"

    id = Column(Integer, primary_key=True)
    funpay_order_id = Column(String, unique=True, nullable=False)  # номер заказа на FunPay, например #ABC123
    buyer_username = Column(String, nullable=True)
    buyer_id = Column(Integer, nullable=True)
    offer_title = Column(String, nullable=True)          # краткое описание товара (как на FunPay)
    offer_category = Column(String, nullable=True)
    status = Column(String, default="new")               # new / link_received / ordered / order_failed / manual_required
    price = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    link = Column(String, nullable=True)                  # ссылка, присланная покупателем (!ссылка)
    chat_id = Column(Integer, nullable=True)              # id чата на FunPay
    forum_chat_id = Column(Integer, nullable=True)
    topic_id = Column(Integer, nullable=True)
    funpay_amount = Column(Integer, nullable=True)        # сколько единиц лота куплено на FunPay
    smmway_service_id = Column(Integer, nullable=True)    # id услуги в smmway.ru для этого лота
    smmway_quantity = Column(Integer, nullable=True)      # итоговое quantity, отправленное в smmway (уже с учётом funpay_amount)
    smmway_order_id = Column(Integer, nullable=True)      # id заказа, который вернул smmway
    created_at = Column(DateTime, default=datetime.utcnow)


class FunPayManager:
    def __init__(self, bot: Bot, config_path: str = "funpay_config.json", smmway_api=None):
        self.bot = bot
        self.config_path = config_path
        self.config: Dict = {}
        self.forum_chat_id: Optional[int] = None
        self.account = None
        self.runner = None
        self.smmway_api = smmway_api  # можно передать сразу или через set_smmway_api() позже
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False

    def set_smmway_api(self, smmway_api):
        """Позволяет подключить/обновить smmway_api уже после создания FunPayManager
        (main.py определяет доступность smmway.ru не сразу)."""
        self.smmway_api = smmway_api

    # ==================== инициализация ====================

    async def init(self) -> bool:
        """Загружает конфиг, поднимает таблицу в БД, логинится в FunPay.
        Возвращает True, если интеграция готова к запуску."""
        if Account is None:
            print("⚠️ Библиотека FunPayAPI не установлена (pip install FunPayAPI). FunPay-интеграция отключена.")
            return False

        self._load_config()

        golden_key = (self.config.get("golden_key") or "").strip()
        if not golden_key or golden_key.startswith("ВСТАВЬ"):
            print("⚠️ В funpay_config.json не указан golden_key. FunPay-интеграция отключена.")
            return False

        self.forum_chat_id = self.config.get("forum_chat_id") or None
        if not self.forum_chat_id:
            print("⚠️ В funpay_config.json не указан forum_chat_id. FunPay-интеграция отключена.")
            return False

        # Создаём таблицу для продаж (использует общий engine из functions/db/db.py)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        user_agent = self.config.get("user_agent") or None
        loop = asyncio.get_running_loop()
        try:
            # Account(...).get() — блокирующий сетевой запрос, уводим в поток,
            # чтобы не подвешивать event loop бота
            self.account = await loop.run_in_executor(
                None, lambda: Account(golden_key, user_agent=user_agent).get()
            )
        except Exception as e:
            print(f"❌ Не удалось авторизоваться в FunPay (проверь golden_key): {e}")
            return False

        self.runner = Runner(self.account)
        self._loop = loop
        print(f"✅ FunPay подключен: {getattr(self.account, 'username', '?')} (id {getattr(self.account, 'id', '?')})")
        return True

    def _load_config(self):
        if not os.path.exists(self.config_path):
            print(f"⚠️ Файл {self.config_path} не найден.")
            self.config = {}
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

    def start(self):
        """Запускает прослушивание событий FunPay в фоновом потоке."""
        if not self.account or not self.runner:
            return
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="FunPayListener")
        self._thread.start()

    def stop(self):
        self._stop = True

    # ==================== цикл прослушивания (фоновый поток) ====================

    def _listen_loop(self):
        delay = self.config.get("requests_delay", 4)
        while not self._stop:
            try:
                for event in self.runner.listen(requests_delay=delay):
                    if self._stop:
                        break
                    self._dispatch_event(event)
            except Exception as e:
                print(f"❌ Ошибка в цикле прослушивания FunPay: {e}. Переподключение через 10 секунд...")
                time.sleep(10)

    def _dispatch_event(self, event):
        if enums is None:
            return
        try:
            if event.type is enums.EventTypes.NEW_ORDER:
                self._run_in_bot_loop(self._handle_new_order(event))
            elif event.type is enums.EventTypes.NEW_MESSAGE:
                self._run_in_bot_loop(self._handle_new_message(event))
        except Exception as e:
            print(f"❌ Ошибка обработки события FunPay: {e}")

    def _run_in_bot_loop(self, coro):
        """Безопасно выполняет корутину в основном event loop бота из фонового потока."""
        if self._loop and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                fut.result(timeout=30)
            except Exception as e:
                print(f"❌ Ошибка выполнения FunPay-обработчика: {e}")
        else:
            asyncio.run(coro)

    # ==================== обработка событий FunPay ====================

    def _find_offer(self, description: str) -> Optional[dict]:
        if not description:
            return None
        offers = self.config.get("offers", [])
        description = description.strip()
        # Точное совпадение
        for o in offers:
            if (o.get("title") or "").strip() == description:
                return o
        # Совпадение по вхождению (на случай мелких расхождений в эмодзи/пробелах)
        for o in offers:
            title = (o.get("title") or "").strip()
            if title and (title in description or description in title):
                return o
        return None

    async def _handle_new_order(self, event):
        order = event.order
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            return

        if await self._get_sale(order_id):
            return  # уже обработан ранее

        description = getattr(order, "description", "") or ""
        buyer_username = getattr(order, "buyer_username", None)
        buyer_id = getattr(order, "buyer_id", None)
        price = getattr(order, "price", None)
        currency = getattr(order, "currency", None)

        offer = self._find_offer(description)
        offer_title = offer["title"] if offer else description
        offer_category = offer.get("category") if offer else None
        smmway_service_id = offer.get("smmway_service_id") if offer else None
        smmway_quantity_per_unit = offer.get("smmway_quantity") if offer else None

        loop = asyncio.get_running_loop()

        # Получаем/находим чат с покупателем на FunPay
        chat_id = None
        if buyer_username:
            try:
                chat = await loop.run_in_executor(
                    None, lambda: self.account.get_chat_by_name(buyer_username, True)
                )
                chat_id = chat.id if chat else None
            except Exception as e:
                print(f"⚠️ Не удалось получить чат FunPay с {buyer_username}: {e}")

        # Получаем полную информацию о заказе, чтобы узнать купленное количество (amount)
        funpay_amount = 1
        try:
            full_order = await loop.run_in_executor(None, lambda: self.account.get_order(order_id))
            funpay_amount = getattr(full_order, "amount", None) or 1
        except Exception as e:
            print(f"⚠️ Не удалось получить полную информацию о заказе {order_id} (amount по умолчанию = 1): {e}")

        # Создаём топик в форуме Telegram под этот заказ
        topic_id = None
        try:
            topic_name = f"FunPay #{order_id} — {buyer_username or 'покупатель'}"[:128]
            topic = await self.bot.create_forum_topic(chat_id=self.forum_chat_id, name=topic_name)
            topic_id = topic.message_thread_id
        except Exception as e:
            print(f"❌ Не удалось создать топик форума для заказа FunPay {order_id}: {e}")

        await self._create_sale(
            funpay_order_id=order_id,
            buyer_username=buyer_username,
            buyer_id=buyer_id,
            offer_title=offer_title,
            offer_category=offer_category,
            price=price,
            currency=currency,
            chat_id=chat_id,
            forum_chat_id=self.forum_chat_id,
            topic_id=topic_id,
            funpay_amount=funpay_amount,
            smmway_service_id=smmway_service_id,
            smmway_quantity=(smmway_quantity_per_unit * funpay_amount) if smmway_quantity_per_unit else None,
        )

        if topic_id:
            auto_note = (
                f"После получения ссылки заказ автоматически уйдёт в smmway.ru "
                f"(service_id {smmway_service_id}, quantity {(smmway_quantity_per_unit or 0) * funpay_amount})."
                if smmway_service_id and smmway_quantity_per_unit
                else "⚠️ Для этого товара не настроен smmway_service_id/smmway_quantity — заказ нужно будет оформить вручную."
            )
            info_text = (
                f"🛒 <b>Новый заказ FunPay</b>\n\n"
                f"Номер заказа: <code>{order_id}</code>\n"
                f"Покупатель: <b>{buyer_username or '—'}</b>\n"
                f"Товар: {offer_title}\n"
                + (f"Категория: {offer_category}\n" if offer_category else "")
                + f"Кол-во единиц лота: {funpay_amount}\n"
                f"Цена: {price} {currency or ''}\n\n"
                f"Жду от покупателя ссылку командой <code>!ссылка</code>.\n"
                f"{auto_note}"
            )
            try:
                await self.bot.send_message(self.forum_chat_id, info_text, message_thread_id=topic_id, parse_mode="HTML")
            except Exception as e:
                print(f"⚠️ Не удалось отправить сообщение в топик форума: {e}")

        # Приветственное сообщение покупателю в чат FunPay
        if chat_id:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self.account.send_message(
                        chat_id,
                        "Спасибо за заказ! 🙌\n"
                        "Пришлите, пожалуйста, ссылку в формате:\n"
                        "!ссылка https://t.me/ваш_канал_или_пост\n\n"
                        "Если нужна помощь — напишите !поддержка"
                    )
                )
            except Exception as e:
                print(f"⚠️ Не удалось отправить приветствие покупателю FunPay: {e}")

    async def _handle_new_message(self, event):
        message = event.message
        chat_id = getattr(message, "chat_id", None)
        author_id = getattr(message, "author_id", None)
        text = getattr(message, "text", None) or ""

        if not chat_id or not text:
            return
        if self.account and author_id == self.account.id:
            return  # это наше собственное сообщение, не дублируем само в себя

        sale = await self._get_sale_by_chat(chat_id)
        if not sale:
            return  # это сообщение не относится к отслеживаемому заказу FunPay

        # Дублируем сообщение покупателя в топик форума
        if sale.topic_id:
            try:
                await self.bot.send_message(
                    self.forum_chat_id,
                    text,
                    message_thread_id=sale.topic_id
                )
            except Exception as e:
                print(f"⚠️ Не удалось продублировать сообщение FunPay в форум: {e}")

        # Команда !ссылка <url>
        link_match = LINK_COMMAND_RE.search(text)
        if link_match:
            link = link_match.group(1).strip()
            await self._update_sale_link(sale.funpay_order_id, link)
            if sale.topic_id:
                try:
                    await self.bot.send_message(
                        self.forum_chat_id,
                        f"🔗 Покупатель прислал ссылку:\n{link}",
                        message_thread_id=sale.topic_id
                    )
                except Exception:
                    pass
            await self._try_auto_order(sale.funpay_order_id, link)

        # Команда !поддержка
        if SUPPORT_COMMAND in text.lower():
            usernames = self.config.get("support_usernames", [])
            if usernames and sale.topic_id:
                mentions = " ".join(f"@{u.lstrip('@')}" for u in usernames)
                try:
                    await self.bot.send_message(
                        self.forum_chat_id,
                        f"🆘 Покупатель зовёт поддержку по заказу <code>{sale.funpay_order_id}</code>! {mentions}",
                        message_thread_id=sale.topic_id,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    async def _try_auto_order(self, funpay_order_id: str, link: str):
        """После получения ссылки от покупателя пытается автоматически
        оформить заказ в smmway.ru (если для товара указан smmway_service_id
        в конфиге и smmway_api доступен). Если что-то не так — просто
        просит в топике оформить заказ вручную."""
        sale = await self._get_sale(funpay_order_id)
        if not sale:
            return

        if sale.smmway_order_id:
            return  # уже оформлен, повторно не отправляем

        if not sale.smmway_service_id:
            if sale.topic_id:
                await self.bot.send_message(
                    self.forum_chat_id,
                    "⚠️ Для этого товара не указан smmway_service_id в funpay_config.json — оформи заказ в smmway вручную.",
                    message_thread_id=sale.topic_id
                )
            await self._update_sale_status(funpay_order_id, "manual_required")
            return

        if not self.smmway_api:
            if sale.topic_id:
                await self.bot.send_message(
                    self.forum_chat_id,
                    "⚠️ API smmway.ru сейчас недоступен — оформи заказ вручную.",
                    message_thread_id=sale.topic_id
                )
            await self._update_sale_status(funpay_order_id, "manual_required")
            return

        quantity = sale.smmway_quantity or 0
        if quantity <= 0:
            if sale.topic_id:
                await self.bot.send_message(
                    self.forum_chat_id,
                    "⚠️ Для этого товара не указан smmway_quantity в funpay_config.json — оформи заказ вручную.",
                    message_thread_id=sale.topic_id
                )
            await self._update_sale_status(funpay_order_id, "manual_required")
            return

        try:
            result = await self.smmway_api.create_order(
                service_id=sale.smmway_service_id,
                link=link,
                quantity=quantity
            )
        except Exception as e:
            result = {"error": str(e)}

        if result.get("success") or result.get("order") or result.get("order_id"):
            api_order_id = result.get("order_id") or result.get("order")
            await self._update_sale_order(funpay_order_id, smmway_order_id=api_order_id, status="ordered")
            if sale.topic_id:
                await self.bot.send_message(
                    self.forum_chat_id,
                    f"✅ Заказ автоматически отправлен в smmway.ru\n"
                    f"ID заказа smmway: <code>{api_order_id}</code>\n"
                    f"Кол-во: {quantity}",
                    message_thread_id=sale.topic_id,
                    parse_mode="HTML"
                )
        else:
            await self._update_sale_status(funpay_order_id, "order_failed")
            error_text = result.get("error", "неизвестная ошибка")
            if sale.topic_id:
                await self.bot.send_message(
                    self.forum_chat_id,
                    f"❌ Не удалось автоматически оформить заказ в smmway: {error_text}\nОформи вручную.",
                    message_thread_id=sale.topic_id
                )

    # ==================== ответ из форума покупателю ====================

    async def handle_forum_reply(self, forum_chat_id: int, topic_id: int, text: str) -> bool:
        """
        Вызывается из main.py при новом сообщении в топике форума.
        Если топик принадлежит сделке FunPay — пересылает текст покупателю
        и возвращает True. Если топик не наш — возвращает False (main.py
        передаст обработку дальше, например в account_manager).
        """
        if not self.forum_chat_id or forum_chat_id != self.forum_chat_id:
            return False
        if not self.account:
            return False

        sale = await self._get_sale_by_topic(topic_id)
        if not sale or not sale.chat_id:
            return False

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self.account.send_message(sale.chat_id, text))
        except Exception as e:
            print(f"⚠️ Не удалось отправить ответ покупателю FunPay: {e}")
        return True

    # ==================== работа с БД ====================

    async def _create_sale(self, **kwargs):
        async with Session() as session:
            sale = FunPaySale(**kwargs)
            session.add(sale)
            await session.commit()
            return sale

    async def _get_sale(self, funpay_order_id: str):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.funpay_order_id == funpay_order_id))
            return result.scalar_one_or_none()

    async def _get_sale_by_chat(self, chat_id: int):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.chat_id == chat_id))
            return result.scalar_one_or_none()

    async def _get_sale_by_topic(self, topic_id: int):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.topic_id == topic_id))
            return result.scalar_one_or_none()

    async def _update_sale_link(self, funpay_order_id: str, link: str):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.funpay_order_id == funpay_order_id))
            sale = result.scalar_one_or_none()
            if sale:
                sale.link = link
                sale.status = "link_received"
                await session.commit()
            return sale

    async def _update_sale_status(self, funpay_order_id: str, status: str):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.funpay_order_id == funpay_order_id))
            sale = result.scalar_one_or_none()
            if sale:
                sale.status = status
                await session.commit()
            return sale

    async def _update_sale_order(self, funpay_order_id: str, smmway_order_id, status: str):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).where(FunPaySale.funpay_order_id == funpay_order_id))
            sale = result.scalar_one_or_none()
            if sale:
                sale.smmway_order_id = smmway_order_id
                sale.status = status
                await session.commit()
            return sale

    async def get_all_sales(self):
        async with Session() as session:
            result = await session.execute(select(FunPaySale).order_by(FunPaySale.created_at.desc()))
            return result.scalars().all()
