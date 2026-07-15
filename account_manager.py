"""
Модуль для управления аккаунтами через Telethon
Синхронизация чатов с форумом Telegram
"""

import asyncio
import os
import random
import tempfile
from datetime import datetime
from typing import Optional, Dict
from telethon import TelegramClient, events
from telethon.tl.types import User, Channel, Chat
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from aiogram import Bot
from functions.db.db import Database, Base, Column, Integer, String, ForeignKey, DateTime, select, delete, Session, engine


# Таблицы для хранения данных
# Аккаунты хранятся как .session файлы в папке data/telethon_sessions/
# Используем имя файла (без расширения) как идентификатор аккаунта

class ForumChatMapping(Base):
    __tablename__ = "forum_chat_mappings"
    
    id = Column(Integer, primary_key=True)
    account_session = Column(String, nullable=False)  # Имя файла сессии (без .session)
    forum_chat_id = Column(Integer, nullable=False)  # ID форума
    topic_id = Column(Integer, nullable=False)  # ID топика в форуме
    user_tg_id = Column(Integer, nullable=False)  # Telegram ID пользователя
    user_name = Column(String, nullable=True)  # Имя пользователя
    created_at = Column(DateTime, default=datetime.utcnow)


class BannedUser(Base):
    __tablename__ = "banned_users"
    
    id = Column(Integer, primary_key=True)
    account_session = Column(String, nullable=False)  # Имя файла сессии (без .session)
    user_tg_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class AutoReply(Base):
    __tablename__ = "auto_replies"
    
    id = Column(Integer, primary_key=True)
    account_session = Column(String, nullable=False)  # Имя файла сессии (без .session)
    reply_text = Column(String, nullable=True)  # Текст автоответа, None если отключен
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AccountManager:
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.clients: Dict[str, TelegramClient] = {}  # session_name -> client
        self.account_forums: Dict[str, int] = {}  # session_name -> forum_chat_id
        self.sync_interval = 30  # Интервал синхронизации в секундах
        self.sync_tasks: Dict[str, asyncio.Task] = {}  # session_name -> task
        self.sessions_dir = "data/telethon_sessions"
        
    async def init(self):
        """Инициализация - создание таблиц и загрузка активных аккаунтов"""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        # Загружаем forum_id для сессий из БД (чтобы работало после перезапуска без /sync)
        await self._load_account_forums_from_db()
        
        # Загружаем активные аккаунты
        await self._load_active_accounts()
    
    async def _load_account_forums_from_db(self):
        """Восстанавливаем account_forums из forum_chat_mappings (после перезапуска)"""
        try:
            async with Session() as session:
                result = await session.execute(select(ForumChatMapping))
                for m in result.scalars().all():
                    self.account_forums[m.account_session] = m.forum_chat_id
                if self.account_forums:
                    print(f"✅ Восстановлены форумы для сессий: {list(self.account_forums.keys())}")
        except Exception as e:
            print(f"Ошибка загрузки форумов из БД: {e}")
    
    async def _load_active_accounts(self):
        """Загрузка аккаунтов из папки с .session файлами"""
        os.makedirs(self.sessions_dir, exist_ok=True)
        
        # Сканируем папку на наличие .session файлов
        for filename in os.listdir(self.sessions_dir):
            if filename.endswith('.session'):
                session_name = filename[:-8]  # Убираем .session
                session_file = os.path.join(self.sessions_dir, filename)
                
                try:
                    await self._connect_account(session_name, session_file)
                    print(f"✅ Загружен аккаунт: {session_name}")
                except Exception as e:
                    print(f"❌ Ошибка загрузки аккаунта {session_name}: {e}")
    
    async def _connect_account(self, session_name: str, session_file: str):
        """Подключение к аккаунту по файлу сессии"""
        if not os.path.exists(session_file):
            print(f"Файл сессии {session_file} не найден")
            return
        
        api_id = 2040
        api_hash = "b18441a1ff607e10a989891a5462e627"
        
        client = TelegramClient(session_file, api_id, api_hash)
        await client.connect()
        
        if await client.is_user_authorized():
            self.clients[session_name] = client
            await self._setup_event_handlers(session_name, client)
        else:
            print(f"Аккаунт {session_name} не авторизован. Удалите файл сессии и создайте новый.")
    
    async def _setup_event_handlers(self, session_name: str, client: TelegramClient):
        """Настройка обработчиков событий для аккаунта"""
        
        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            await self._handle_new_message(session_name, event)
        
        # Запускаем клиент
        await client.start()
    
    async def _handle_new_message(self, session_name: str, event):
        """Обработка нового сообщения"""
        try:
            # Небольшая задержка — имитация человека, снижает риск бана
            await asyncio.sleep(random.uniform(0.3, 0.8))
            chat = await event.get_chat()
            
            # Проверяем, это форум или приватный чат
            if isinstance(chat, Channel) and chat.forum:
                # Это форум - сообщение от менеджера в топике
                # Получаем topic_id из reply_to или из самого сообщения
                topic_id = None
                if event.message.reply_to:
                    if hasattr(event.message.reply_to, 'reply_to_top_id') and event.message.reply_to.reply_to_top_id:
                        topic_id = event.message.reply_to.reply_to_top_id
                    elif hasattr(event.message.reply_to, 'reply_to_msg_id'):
                        # Пытаемся получить topic_id из исходного сообщения
                        try:
                            replied_msg = await event.get_reply_message()
                            if replied_msg and hasattr(replied_msg, 'reply_to'):
                                if hasattr(replied_msg.reply_to, 'reply_to_top_id'):
                                    topic_id = replied_msg.reply_to.reply_to_top_id
                        except:
                            pass
                
                # Если не нашли через reply_to, пытаемся получить из самого сообщения
                if not topic_id and hasattr(event.message, 'reply_to'):
                    if hasattr(event.message.reply_to, 'reply_to_top_id'):
                        topic_id = event.message.reply_to.reply_to_top_id
                
                if topic_id:
                    await self._handle_forum_message(session_name, event, topic_id)
            elif isinstance(chat, User):
                # Это приватный чат от пользователя
                user_id = event.sender_id
                
                # Проверяем, не забанен ли пользователь
                if await self._is_user_banned(session_name, user_id):
                    return
                
                # Получаем форум для этого аккаунта
                forum_id = await self.get_account_forum(session_name)
                if not forum_id:
                    print(f"⚠️ Для аккаунта {session_name} не задан форум. Выполните /sync в нужном форуме.")
                    return
                
                await self._handle_user_message(session_name, event, forum_id)
        except Exception as e:
            import traceback
            print(f"Ошибка обработки сообщения: {e}")
            traceback.print_exc()
    
    async def _handle_user_message(self, session_name: str, event, forum_id: int):
        """Обработка сообщения от пользователя"""
        user_id = event.sender_id
        client = self.clients[session_name]
        
        # Получаем имя пользователя
        sender = await event.get_sender()
        user_name = self._get_user_name(sender)
        
        # Проверяем, есть ли уже топик для этого пользователя
        mapping = await self._get_mapping_by_user(session_name, user_id)
        chat_just_created = False
        
        if not mapping:
            chat_just_created = True
            # Создаем новый топик в форуме
            # В Telethon топик создается через отправку сообщения в форум
            try:
                # Получаем сущность канала перед отправкой
                # Используем сохраненную сущность форума если есть
                if session_name in self.account_forums:
                    # Пробуем получить сущность форума
                    try:
                        forum_entity = await client.get_entity(forum_id)
                    except (ValueError, TypeError):
                        # Если не получилось, используем ID напрямую (Telethon попробует сам)
                        forum_entity = forum_id
                else:
                    forum_entity = forum_id
                
                # Используем основного бота (aiogram) для создания топика в форуме
                # Это проще и надежнее, чем через Telethon
                try:
                    # Создаем топик через основного бота
                    topic = await self.bot.create_forum_topic(
                        chat_id=forum_id,
                        name=user_name
                    )
                    topic_id = topic.message_thread_id
                except Exception as e:
                    print(f"Ошибка создания топика через бота: {e}")
                    raise Exception(f"Не удалось создать топик через бота: {e}")
                
                # Сохраняем маппинг
                async with Session() as session:
                    mapping = ForumChatMapping(
                        account_session=session_name,
                        forum_chat_id=forum_id,
                        topic_id=topic_id,
                        user_tg_id=user_id,
                        user_name=user_name
                    )
                    session.add(mapping)
                    await session.commit()
            except Exception as e:
                print(f"Ошибка создания топика: {e}")
                return
        else:
            topic_id = mapping.topic_id
        
        # Отправляем сообщение в топик форума через ОСНОВНОГО БОТА
        message_text = event.message.text or "[Медиа]"
        try:
            from aiogram.types import FSInputFile
            if event.message.photo:
                photo_path = await self._download_photo(session_name, event.message)
                if photo_path:
                    await self.bot.send_photo(
                        chat_id=forum_id,
                        photo=FSInputFile(photo_path),
                        caption=message_text,
                        message_thread_id=topic_id
                    )
                    try:
                        os.remove(photo_path)
                    except:
                        pass
            else:
                await self.bot.send_message(
                    chat_id=forum_id,
                    text=message_text,
                    message_thread_id=topic_id
                )
            if "пополнение" in message_text.lower():
                await self._send_topup_reply(session_name, user_id, client)
            elif chat_just_created:
                await self._send_autoreply(session_name, user_id, client)
            await client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
        except Exception as e:
            if mapping and self._is_thread_not_found_error(e):
                # Топик удалён в Telegram — удаляем маппинг и создаём заново
                await self._delete_mapping_by_user(session_name, user_id)
                await self._handle_user_message(session_name, event, forum_id)
            else:
                print(f"Ошибка отправки сообщения в форум: {e}")
    
    async def _handle_forum_message(self, session_name: str, event, topic_id: int):
        """Обработка сообщения в форуме (ответ от менеджера)"""
        # Находим маппинг по topic_id
        mapping = await self._get_mapping_by_topic(session_name, topic_id)
        if not mapping:
            return
        
        user_id = mapping.user_tg_id
        client = self.clients[session_name]
        
        # Небольшая задержка перед отправкой (антибан)
        await asyncio.sleep(random.uniform(0.2, 0.6))
        message_text = event.message.text or ""
        if event.message.photo:
            photo_path = await self._download_photo(session_name, event.message)
            if photo_path:
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
            # Отправляем сообщение без клавиатуры
            await client.send_message(
                user_id, 
                message_text,
                buttons=None  # Убираем клавиатуру
            )
        
            # Помечаем прочитанным только последнее (меньше нагрузка = меньше риска бана)
            try:
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await client.send_read_acknowledge(user_id, max_id=event.message.id)
            except:
                pass
    
    async def _download_photo(self, session_name: str, message) -> Optional[str]:
        """Скачивание фото из сообщения"""
        try:
            if not message.photo:
                return None
            
            client = self.clients[session_name]
            temp_dir = tempfile.gettempdir()
            photo_path = os.path.join(temp_dir, f"photo_{message.id}.jpg")
            
            await client.download_media(message, photo_path)
            return photo_path
        except Exception as e:
            print(f"Ошибка скачивания фото: {e}")
            return None
    
    def _get_user_name(self, sender) -> str:
        """Получение имени пользователя"""
        if isinstance(sender, User):
            if sender.first_name:
                name = sender.first_name
                if sender.last_name:
                    name += f" {sender.last_name}"
                if sender.username:
                    name += f" (@{sender.username})"
                return name
            return f"User {sender.id}"
        return "Unknown"
    
    async def _delete_mapping_by_user(self, session_name: str, user_id: int) -> None:
        """Удаление маппинга по user (например, топик удалён в форуме)"""
        async with Session() as session:
            await session.execute(
                delete(ForumChatMapping).where(
                    ForumChatMapping.account_session == session_name,
                    ForumChatMapping.user_tg_id == user_id
                )
            )
            await session.commit()
    
    def _is_thread_not_found_error(self, e: Exception) -> bool:
        """Проверка: ошибка «топик не найден» (удалён в Telegram)"""
        msg = str(e).lower()
        return "thread" in msg and "not found" in msg or "message_thread_not_found" in msg
    
    async def _get_mapping_by_user(self, session_name: str, user_id: int) -> Optional[ForumChatMapping]:
        """Получение маппинга по user_id"""
        async with Session() as session:
            result = await session.execute(
                select(ForumChatMapping).where(
                    ForumChatMapping.account_session == session_name,
                    ForumChatMapping.user_tg_id == user_id
                )
            )
            return result.scalar_one_or_none()
    
    async def _get_mapping_by_topic(self, session_name: str, topic_id: int) -> Optional[ForumChatMapping]:
        """Получение маппинга по topic_id"""
        async with Session() as session:
            result = await session.execute(
                select(ForumChatMapping).where(
                    ForumChatMapping.account_session == session_name,
                    ForumChatMapping.topic_id == topic_id
                )
            )
            return result.scalar_one_or_none()
    
    async def get_mapping_by_topic(self, session_name: str, topic_id: int) -> Optional[ForumChatMapping]:
        """Публичный метод для получения маппинга по topic_id"""
        return await self._get_mapping_by_topic(session_name, topic_id)
    
    async def get_mapping_by_forum_and_topic(self, forum_chat_id: int, topic_id: int) -> Optional[ForumChatMapping]:
        """Получение маппинга по forum_chat_id и topic_id"""
        async with Session() as session:
            result = await session.execute(
                select(ForumChatMapping).where(
                    ForumChatMapping.forum_chat_id == forum_chat_id,
                    ForumChatMapping.topic_id == topic_id
                )
            )
            return result.scalar_one_or_none()
    
    async def _is_user_banned(self, session_name: str, user_id: int) -> bool:
        """Проверка, забанен ли пользователь"""
        async with Session() as session:
            result = await session.execute(
                select(BannedUser).where(
                    BannedUser.account_session == session_name,
                    BannedUser.user_tg_id == user_id
                )
            )
            return result.scalar_one_or_none() is not None
    
    async def sync_chats(self, session_name: str, forum_chat_id: int) -> Dict:
        """
        Синхронизация чатов с форумом
        Создает топики для всех приватных чатов
        """
        if session_name not in self.clients:
            return {
                "success": False,
                "error": "Аккаунт не подключен"
            }
        
        try:
            client = self.clients[session_name]
            
            # Получаем сущность форума перед началом синхронизации
            try:
                # Пробуем получить сущность по ID
                # Для каналов в Telethon нужно использовать отрицательный ID или правильный формат
                forum_entity = await client.get_entity(forum_chat_id)
            except (ValueError, TypeError) as e:
                # Если не получилось, пробуем через PeerChannel
                from telethon.tl.types import PeerChannel
                try:
                    # Для каналов ID может быть отрицательным
                    channel_id = abs(forum_chat_id)
                    forum_entity = await client.get_entity(PeerChannel(channel_id=channel_id))
                except:
                    return {
                        "success": False,
                        "error": f"Не удалось получить доступ к форуму. Убедитесь, что аккаунт добавлен в форум и имеет права администратора. Ошибка: {str(e)}"
                    }
            
            self.account_forums[session_name] = forum_chat_id
            
            # Получаем все приватные диалоги
            created_count = 0
            async for dialog in client.iter_dialogs():
                if dialog.is_user:  # Только приватные чаты с пользователями
                    # В Telethon dialog.entity уже содержит сущность
                    user = dialog.entity
                    user_id = user.id
                    user_name = self._get_user_name(user)
                    
                    # Проверяем, есть ли уже маппинг
                    mapping = await self._get_mapping_by_user(session_name, user_id)
                    
                    if not mapping:
                        # Создаем топик
                        try:
                            # Используем основного бота (aiogram) для создания топика
                            try:
                                topic = await self.bot.create_forum_topic(
                                    chat_id=forum_chat_id,
                                    name=user_name
                                )
                                topic_id = topic.message_thread_id
                            except Exception as e:
                                print(f"Ошибка создания топика через бота: {e}")
                                raise Exception(f"Не удалось создать топик через бота: {e}")
                            
                            # Сохраняем маппинг
                            async with Session() as session:
                                mapping = ForumChatMapping(
                                    account_session=session_name,
                                    forum_chat_id=forum_chat_id,
                                    topic_id=topic_id,
                                    user_tg_id=user_id,
                                    user_name=user_name
                                )
                                session.add(mapping)
                                await session.commit()
                            
                            created_count += 1
                        except Exception as e:
                            print(f"Ошибка создания топика для {user_name}: {e}")
            
            return {
                "success": True,
                "created_topics": created_count,
                "message": f"Создано топиков: {created_count}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def ban_user(self, session_name: str, user_id: int) -> Dict:
        """Бан пользователя"""
        try:
            async with Session() as session:
                # Проверяем, не забанен ли уже
                result = await session.execute(
                    select(BannedUser).where(
                        BannedUser.account_session == session_name,
                        BannedUser.user_tg_id == user_id
                    )
                )
                if result.scalar_one_or_none():
                    return {
                        "success": False,
                        "error": "Пользователь уже забанен"
                    }
                
                banned = BannedUser(
                    account_session=session_name,
                    user_tg_id=user_id
                )
                session.add(banned)
                await session.commit()
            
            return {
                "success": True,
                "message": "Пользователь забанен"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def unban_user(self, session_name: str, user_id: int) -> Dict:
        """Разбан пользователя"""
        try:
            async with Session() as session:
                result = await session.execute(
                    select(BannedUser).where(
                        BannedUser.account_session == session_name,
                        BannedUser.user_tg_id == user_id
                    )
                )
                banned = result.scalar_one_or_none()
                if not banned:
                    return {
                        "success": False,
                        "error": "Пользователь не забанен"
                    }
                
                await session.delete(banned)
                await session.commit()
            
            return {
                "success": True,
                "message": "Пользователь разбанен"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    async def set_autoreply(self, session_name: str, reply_text: Optional[str]) -> Dict:
        """Установка автоответа"""
        try:
            async with Session() as session:
                result = await session.execute(
                    select(AutoReply).where(AutoReply.account_session == session_name)
                )
                autoreply = result.scalar_one_or_none()
                
                if autoreply:
                    autoreply.reply_text = reply_text
                    autoreply.updated_at = datetime.utcnow()
                else:
                    autoreply = AutoReply(
                        account_session=session_name,
                        reply_text=reply_text
                    )
                    session.add(autoreply)
                
                await session.commit()
            
            return {
                "success": True,
                "message": "Автоответ установлен" if reply_text else "Автоответ отключен"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    TOPUP_REPLY_TEXT = (
        "Для пополнения отправьте нужную сумму на номер карты <code>2204120110617061</code>, "
        "затем пришлите сюда чек и номер заказа, в формате #XXXXXXXX"
    )
    
    async def _send_topup_reply(self, session_name: str, user_id: int, client: TelegramClient):
        """Ответ на слово «пополнение» — инструкция по оплате картой"""
        try:
            await client.send_message(
                user_id,
                self.TOPUP_REPLY_TEXT,
                parse_mode='html',
                buttons=None
            )
        except Exception as e:
            print(f"Ошибка отправки ответа на пополнение: {e}")
    
    async def _send_autoreply(self, session_name: str, user_id: int, client: TelegramClient):
        """Отправка автоответа (при создании чата)"""
        try:
            async with Session() as session:
                result = await session.execute(
                    select(AutoReply).where(AutoReply.account_session == session_name)
                )
                autoreply = result.scalar_one_or_none()
                
                if autoreply and autoreply.reply_text:
                    await client.send_message(user_id, autoreply.reply_text, buttons=None)
        except Exception as e:
            print(f"Ошибка отправки автоответа: {e}")
    
    async def start_sync_task(self, session_name: str):
        """Запуск задачи синхронизации по таймеру (если события не работают)"""
        if session_name in self.sync_tasks:
            return
        
        async def sync_loop():
            while session_name in self.clients:
                try:
                    if session_name in self.account_forums:
                        forum_id = self.account_forums[session_name]
                        # Можно добавить дополнительную синхронизацию здесь
                        pass
                except Exception as e:
                    print(f"Ошибка синхронизации: {e}")
                
                await asyncio.sleep(self.sync_interval)
        
        self.sync_tasks[session_name] = asyncio.create_task(sync_loop())
    
    async def get_account_forum(self, session_name: str) -> Optional[int]:
        """Получение ID форума для аккаунта"""
        return self.account_forums.get(session_name)
    
    async def get_active_accounts(self) -> list:
        """Получение списка активных аккаунтов (имена сессий)"""
        return list(self.clients.keys())
