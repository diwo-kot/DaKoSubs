import asyncio
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    ForeignKey,
    DateTime,
    select,
    delete,
    or_
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession
)
from sqlalchemy.orm import (
    declarative_base,
    sessionmaker
)

engine = create_async_engine(
    "sqlite+aiosqlite:///data/bot.db"
)

Base = declarative_base()

Session = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    balance = Column(Integer, default=0)
    role = Column(String, default="user")
    username = Column(String, nullable=True)  # username без @
    first_name = Column(String, nullable=True)
    is_banned = Column(Integer, default=0)  # 0/1 флаг бана в боте
    privacy_accepted = Column(Integer, default=0)  # 0/1 флаг согласия с политикой
    referral_code = Column(String, unique=True, nullable=True)  # Реферальный код пользователя
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # ID пригласившего
    total_earned = Column(Integer, default=0)  # Заработано с рефералов
    discount_percent = Column(Float, default=0.0)  # Скидка пользователя в процентах
    used_promocodes = Column(String, nullable=True) # Использованные промокоды (JSON-строка или CSV)

class Logs(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    action = Column(String, nullable=False)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    amount = Column(Float, nullable=True)  # Сумма для пополнений

class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tg_id = Column(Integer, nullable=False)
    amount = Column(String, nullable=False)
    status = Column(String, default="expired")
    method = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    order = Column(String, nullable=False)
    invoice_id = Column(Integer, nullable=True)
    payment_type = Column(String, default='crypto') # 'crypto' or 'manual'
    fee = Column(Float, default=0.0)



class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_code = Column(String, unique=True, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    payment_id = Column(Integer, ForeignKey("payments.id"))
    service = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    status = Column(String, default="new")
    created_at = Column(DateTime, default=datetime.utcnow)
    api_order_id = Column(Integer, nullable=True)  # ID заказа в API smmway.ru
    service_api_id = Column(Integer, nullable=True) # ID услуги в API для повторных попыток
    link = Column(String, nullable=True)  # Ссылка для выполнения заказа
    price_buy = Column(Float, nullable=True)  # Сколько мы потратили
    price_sell = Column(Float, nullable=True) # Сколько заплатил пользователь

class App(Base):
    __tablename__ = 'apps'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

class Service(Base):
    __tablename__ = 'service'

    id = Column(Integer, primary_key=True)
    app_id = Column(Integer, ForeignKey('apps.id'), nullable=False)
    name = Column(String, nullable=False)
    service_api_id = Column(Integer, nullable=True)  # ID услуги в API smmway.ru

class Price(Base):
    __tablename__ = 'price'

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey('service.id'), nullable=False)
    name = Column(String, nullable=False)
    priceSell = Column(Float, nullable=False)
    priceBuy = Column(Float, nullable=False)
    markup_percent = Column(Float, default=250.0)  # Наценка в процентах (по умолчанию 250%)
    min_quantity = Column(Integer, nullable=True)
    max_quantity = Column(Integer, nullable=True)

class SupportAccount(Base):
    __tablename__ = "support_accounts"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False, unique=True)  # @username без @
    created_at = Column(DateTime, default=datetime.utcnow)


class PromoCode(Base):
    __tablename__ = "promocodes"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    discount = Column(Float, nullable=False) # Скидка в процентах
    total_activations = Column(Integer, nullable=False)
    used_activations = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)



class Database:
    async def init(self):
        async with engine.begin() as conn:
            # Создаем все таблицы
            await conn.run_sync(Base.metadata.create_all)
            
            # Проверяем и добавляем новые колонки если их нет (миграция)
            await self._migrate_database(conn)
    
    async def _migrate_database(self, conn):
        """Миграция базы данных - добавление новых колонок"""
        import aiosqlite
        
        # Используем прямое подключение к SQLite для миграций
        db_path = "data/bot.db"
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.cursor()
            
            # Проверяем существующие колонки в таблице users
            await cursor.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in await cursor.fetchall()]
            
            # Добавляем referral_code если его нет
            if 'referral_code' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN referral_code VARCHAR")
                    await db.commit()
                    print("✅ Добавлена колонка referral_code")
                except Exception as e:
                    print(f"Ошибка при добавлении referral_code: {e}")
            
            # Добавляем referrer_id если его нет
            if 'referrer_id' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка referrer_id")
                except Exception as e:
                    print(f"Ошибка при добавлении referrer_id: {e}")
            
            # Добавляем total_earned если его нет
            if 'total_earned' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN total_earned INTEGER DEFAULT 0")
                    await db.commit()
                    print("✅ Добавлена колонка total_earned")
                except Exception as e:
                    print(f"Ошибка при добавлении total_earned: {e}")
            
            if 'discount_percent' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN discount_percent FLOAT DEFAULT 0.0")
                    await db.commit()
                    print("✅ Добавлена колонка discount_percent в users")
                except Exception as e:
                    print(f"Ошибка при добавлении discount_percent: {e}")
            
            if 'used_promocodes' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN used_promocodes TEXT")
                    await db.commit()
                    print("✅ Добавлена колонка used_promocodes в users")
                except Exception as e:
                    print(f"Ошибка при добавлении used_promocodes: {e}")

            # Новые поля для username / имени / бана / политики
            if 'username' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN username VARCHAR")
                    await db.commit()
                    print("✅ Добавлена колонка username в users")
                except Exception as e:
                    print(f"Ошибка при добавлении username: {e}")

            if 'first_name' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN first_name VARCHAR")
                    await db.commit()
                    print("✅ Добавлена колонка first_name в users")
                except Exception as e:
                    print(f"Ошибка при добавлении first_name: {e}")

            if 'is_banned' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
                    await db.commit()
                    print("✅ Добавлена колонка is_banned в users")
                except Exception as e:
                    print(f"Ошибка при добавлении is_banned: {e}")

            if 'privacy_accepted' not in columns:
                try:
                    await cursor.execute("ALTER TABLE users ADD COLUMN privacy_accepted INTEGER DEFAULT 0")
                    await db.commit()
                    print("✅ Добавлена колонка privacy_accepted в users")
                except Exception as e:
                    print(f"Ошибка при добавлении privacy_accepted: {e}")

            # Проверяем таблицу price
            await cursor.execute("PRAGMA table_info(price)")
            price_columns = [row[1] for row in await cursor.fetchall()]
            
            # Добавляем markup_percent если его нет
            if 'markup_percent' not in price_columns:
                try:
                    await cursor.execute("ALTER TABLE price ADD COLUMN markup_percent FLOAT DEFAULT 250.0")
                    await db.commit()
                    print("✅ Добавлена колонка markup_percent")
                except Exception as e:
                    print(f"Ошибка при добавлении markup_percent: {e}")
            
            if 'min_quantity' not in price_columns:
                try:
                    await cursor.execute("ALTER TABLE price ADD COLUMN min_quantity INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка min_quantity в price")
                except Exception as e:
                    print(f"Ошибка при добавлении min_quantity: {e}")
            
            if 'max_quantity' not in price_columns:
                try:
                    await cursor.execute("ALTER TABLE price ADD COLUMN max_quantity INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка max_quantity в price")
                except Exception as e:
                    print(f"Ошибка при добавлении max_quantity: {e}")



            # Проверяем таблицу orders
            await cursor.execute("PRAGMA table_info(orders)")
            order_columns = [row[1] for row in await cursor.fetchall()]
            
            # Добавляем api_order_id если его нет
            if 'api_order_id' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN api_order_id INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка api_order_id")
                except Exception as e:
                    print(f"Ошибка при добавлении api_order_id: {e}")
            
            # Добавляем link если его нет
            if 'link' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN link VARCHAR")
                    await db.commit()
                    print("✅ Добавлена колонка link")
                except Exception as e:
                    print(f"Ошибка при добавлении link: {e}")
            
            if 'price_buy' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN price_buy FLOAT")
                    await db.commit()
                    print("✅ Добавлена колонка price_buy в orders")
                except Exception as e:
                    print(f"Ошибка при добавлении price_buy: {e}")
            
            if 'price_sell' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN price_sell FLOAT")
                    await db.commit()
                    print("✅ Добавлена колонка price_sell в orders")
                except Exception as e:
                    print(f"Ошибка при добавлении price_sell: {e}")

            if 'order_code' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN order_code VARCHAR")
                    await db.commit()
                    print("✅ Добавлена колонка order_code в orders")
                except Exception as e:
                    print(f"Ошибка при добавлении order_code: {e}")

            if 'service_api_id' not in order_columns:
                try:
                    await cursor.execute("ALTER TABLE orders ADD COLUMN service_api_id INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка service_api_id в orders")
                except Exception as e:
                    print(f"Ошибка при добавлении service_api_id: {e}")

            # Проверяем таблицу payments
            await cursor.execute("PRAGMA table_info(payments)")
            payment_cols = [row[1] for row in await cursor.fetchall()]
            if 'payment_type' not in payment_cols:
                await cursor.execute("ALTER TABLE payments ADD COLUMN payment_type VARCHAR DEFAULT 'crypto'")
            if 'fee' not in payment_cols:
                await cursor.execute("ALTER TABLE payments ADD COLUMN fee FLOAT DEFAULT 0.0")
            await db.commit()

            
            # Проверяем таблицу service
            await cursor.execute("PRAGMA table_info(service)")
            service_columns = [row[1] for row in await cursor.fetchall()]
            
            # Добавляем service_api_id если его нет
            if 'service_api_id' not in service_columns:
                try:
                    await cursor.execute("ALTER TABLE service ADD COLUMN service_api_id INTEGER")
                    await db.commit()
                    print("✅ Добавлена колонка service_api_id")
                except Exception as e:
                    print(f"Ошибка при добавлении service_api_id: {e}")
            
            # Проверяем таблицу logs
            await cursor.execute("PRAGMA table_info(logs)")
            logs_columns = [row[1] for row in await cursor.fetchall()]
            
            # Добавляем created_at если его нет
            if 'created_at' not in logs_columns:
                try:
                    await cursor.execute("ALTER TABLE logs ADD COLUMN created_at DATETIME")
                    await db.commit()
                    print("✅ Добавлена колонка created_at в logs")
                except Exception as e:
                    print(f"Ошибка при добавлении created_at: {e}")
            
            # Добавляем amount если его нет
            if 'amount' not in logs_columns:
                try:
                    await cursor.execute("ALTER TABLE logs ADD COLUMN amount FLOAT")
                    await db.commit()
                    print("✅ Добавлена колонка amount в logs")
                except Exception as e:
                    print(f"Ошибка при добавлении amount: {e}")
            
            # Миграция таблиц account_manager
            await self._migrate_account_manager_tables(cursor, db)
            
            # Таблица support_accounts
            await cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='support_accounts'")
            if not await cursor.fetchone():
                await cursor.execute("""
                    CREATE TABLE support_accounts (
                        id INTEGER PRIMARY KEY,
                        username VARCHAR NOT NULL UNIQUE,
                        created_at DATETIME
                    )
                """)
                await db.commit()
                print("✅ Создана таблица support_accounts")
    
    async def _migrate_account_manager_tables(self, cursor, db):
        """Миграция таблиц для account_manager"""
        try:
            # Проверяем существование таблицы forum_chat_mappings
            await cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forum_chat_mappings'")
            table_exists = await cursor.fetchone()
            
            if table_exists:
                # Проверяем структуру таблицы
                await cursor.execute("PRAGMA table_info(forum_chat_mappings)")
                columns = [row[1] for row in await cursor.fetchall()]
                
                # Если есть старая колонка account_id, нужно мигрировать
                if 'account_id' in columns and 'account_session' not in columns:
                    print("🔄 Миграция таблицы forum_chat_mappings...")
                    # Создаем временную таблицу с правильной структурой
                    await cursor.execute("""
                        CREATE TABLE forum_chat_mappings_new (
                            id INTEGER PRIMARY KEY,
                            account_session VARCHAR NOT NULL,
                            forum_chat_id INTEGER NOT NULL,
                            topic_id INTEGER NOT NULL,
                            user_tg_id INTEGER NOT NULL,
                            user_name VARCHAR,
                            created_at DATETIME
                        )
                    """)
                    
                    # Копируем данные (если есть), используя account_id как строку
                    # Но так как мы не знаем реальные имена сессий, просто удаляем старые данные
                    # или можно попробовать получить из account_id, но проще пересоздать
                    await cursor.execute("DROP TABLE forum_chat_mappings")
                    await cursor.execute("ALTER TABLE forum_chat_mappings_new RENAME TO forum_chat_mappings")
                    await db.commit()
                    print("✅ Таблица forum_chat_mappings мигрирована")
            
            # Проверяем таблицу banned_users
            await cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='banned_users'")
            table_exists = await cursor.fetchone()
            
            if table_exists:
                await cursor.execute("PRAGMA table_info(banned_users)")
                columns = [row[1] for row in await cursor.fetchall()]
                
                if 'account_id' in columns and 'account_session' not in columns:
                    print("🔄 Миграция таблицы banned_users...")
                    await cursor.execute("""
                        CREATE TABLE banned_users_new (
                            id INTEGER PRIMARY KEY,
                            account_session VARCHAR NOT NULL,
                            user_tg_id INTEGER NOT NULL,
                            created_at DATETIME
                        )
                    """)
                    await cursor.execute("DROP TABLE banned_users")
                    await cursor.execute("ALTER TABLE banned_users_new RENAME TO banned_users")
                    await db.commit()
                    print("✅ Таблица banned_users мигрирована")
            
            # Проверяем таблицу auto_replies
            await cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_replies'")
            table_exists = await cursor.fetchone()
            
            if table_exists:
                await cursor.execute("PRAGMA table_info(auto_replies)")
                columns = [row[1] for row in await cursor.fetchall()]
                
                if 'account_id' in columns and 'account_session' not in columns:
                    print("🔄 Миграция таблицы auto_replies...")
                    await cursor.execute("""
                        CREATE TABLE auto_replies_new (
                            id INTEGER PRIMARY KEY,
                            account_session VARCHAR NOT NULL,
                            reply_text VARCHAR,
                            created_at DATETIME,
                            updated_at DATETIME
                        )
                    """)
                    await cursor.execute("DROP TABLE auto_replies")
                    await cursor.execute("ALTER TABLE auto_replies_new RENAME TO auto_replies")
                    await db.commit()
                    print("✅ Таблица auto_replies мигрирована")
        except Exception as e:
            print(f"Ошибка при миграции таблиц account_manager: {e}")

    async def get_user(self, tg_id: int):
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            return result.scalar_one_or_none()

    async def create_user(self, tg_id: int, referrer_id: int | None = None, username: str | None = None, first_name: str | None = None):
        async with Session() as session:
            from functions.orderCode import generate_secure_random_string
            # Генерируем уникальный реферальный код
            referral_code = None
            while not referral_code:
                # Генерируем код без префикса
                code = generate_secure_random_string(length=8, with_prefix=False)
                result = await session.execute(select(User).where(User.referral_code == code))
                if not result.scalar_one_or_none():
                    referral_code = code
            
            user = User(
                tg_id=tg_id,
                referral_code=referral_code,
                referrer_id=referrer_id,
                total_earned=0,
                username=username,
                first_name=first_name
            )
            session.add(user)
            await session.commit()
            return user

    async def update_user_names(self, tg_id: int, username: str | None, first_name: str | None):
        """Обновление username / first_name пользователя"""
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                return
            user.username = username
            user.first_name = first_name
            await session.commit()

    async def add_balance(self, tg_id: int, amount: int):
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            user = result.scalar_one()
            user.balance += int(amount)
            await session.commit()
            return user.balance

    async def get_user_by_db_id(self, user_id: int):
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            return result.scalar_one_or_none()

    async def get_order_by_code(self, code: str):
        async with Session() as session:
            # Handle with or without '#'
            if not code.startswith('#'):
                code = '#' + code
            result = await session.execute(
                select(Order).where(Order.order_code == code)
            )
            return result.scalar_one_or_none()

    async def get_payment_by_id(self, payment_id: int):
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.id == payment_id)
            )
            return result.scalar_one_or_none()

    async def get_payment_by_order_code(self, order_code: str):
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.order == order_code)
            )
            return result.scalar_one_or_none()

    async def create_promocode(self, code: str, discount: float, activations: int):
        async with Session() as session:
            promo = PromoCode(
                code=code.upper(),
                discount=discount,
                total_activations=activations
            )
            session.add(promo)
            await session.commit()
            return promo

    async def get_promocode_by_code(self, code: str):
        async with Session() as session:
            result = await session.execute(
                select(PromoCode).where(PromoCode.code == code.upper())
            )
            return result.scalar_one_or_none()

    async def apply_promocode_to_user(self, tg_id: int, promo_code: PromoCode):
        async with Session() as session:
            user_res = await session.execute(select(User).where(User.tg_id == tg_id))
            user = user_res.scalar_one()
            used_codes = user.used_promocodes.split(',') if user.used_promocodes else []
            used_codes.append(promo_code.code)
            user.used_promocodes = ','.join(used_codes)
            user.discount_percent = max(user.discount_percent or 0.0, promo_code.discount)
            promo_code.used_activations += 1
            await session.commit()

    async def reset_user_discount(self, tg_id: int):
        """Сбрасывает скидку пользователя до 0 после использования."""
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            if user:
                user.discount_percent = 0.0
                await session.commit()

    async def get_invoice_id(self, order: str) -> int:
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.order == order)
            )
            payment = result.scalar_one_or_none()
            if payment:
                return payment.invoice_id
            return None
             
    async def log_action(self, admin_id: int, user_id: int, action: str, description: str, amount: float | None = None) -> None:
        async with Session() as session:
            log = Logs(
                admin_id=admin_id,
                user_id=user_id,
                action=action,
                description=description if isinstance(description, str) else '-',
                amount=amount
            )
            session.add(log)
            await session.commit()
            

    async def create_payment(self, tg_id: int, amount: float, method: str, order: str, fiat: str, invoice_id: int):
        if fiat == 'RUB':
            cur = '₽'
        elif fiat == 'USD':
            cur = '$'

        am = f'{amount}{cur}'
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            user = result.scalar_one()

            payment = Payment(
                user_id=user.id,
                tg_id=tg_id,
                amount=am,
                status='expired',
                method=method,
                order=order,
                invoice_id=invoice_id
            )
            session.add(payment)
            await session.commit()
            return payment

    async def create_manual_payment(self, tg_id: int, amount: float, fee: float, order_code: str):
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one()
            
            total_amount = f'{(amount + fee):.2f}₽'

            payment = Payment(
                user_id=user.id,
                tg_id=tg_id,
                amount=total_amount,
                status='pending',
                method='manual',
                order=order_code,
                invoice_id=None,
                payment_type='manual',
                fee=fee
            )
            session.add(payment)
            await session.commit()
            return payment

    async def update_price(self, app_name: str, service_name: str, pack_name: str, new_price: float):
        async with Session() as session:
            result = await session.execute(
                select(App).where(App.name == app_name)
            )
            app = result.scalar_one_or_none()
            if not app:
                return False

            result = await session.execute(
                select(Service).where(
                    Service.app_id == app.id,
                    Service.name == service_name
                )
            )
            service = result.scalar_one_or_none()
            if not service:
                return False
            
            result = await session.execute(
                select(Price).where(
                    Price.service_id == service.id,
                    Price.name == pack_name
                )
            )
            price = result.scalar_one_or_none()
            if not price:
                return False
            price.priceSell = new_price
            await session.commit()
            return True

    async def get_admins(self) -> list[User] | list:
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.role == 'admin')
            )
            admins = result.scalars().all()
            return admins
    
    async def update_payment_status(self, code: str, new_status: str):
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.order == code)
            )
            payment = result.scalar_one_or_none()

            if not payment:
                return None
            
            payment.status = new_status
            await session.commit()
            return payment

    async def create_order(
        self,
        tg_id: int,
        service: str,
        order_code: str,
        quantity: int,
        service_api_id: int | None = None,
        payment_id: int | None = None,
        api_order_id: int | None = None,
        link: str | None = None,
        price_buy: float | None = None,
        price_sell: float | None = None
    ):
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            user = result.scalar_one()

            order = Order(
                user_id=user.id,
                order_code=order_code,
                payment_id=payment_id,
                service=service,
                quantity=quantity,
                status="new",
                api_order_id=api_order_id,
                service_api_id=service_api_id,
                link=link,
                price_buy=price_buy,
                price_sell=price_sell
            )
            session.add(order)
            await session.commit()
            return order

    async def get_orders(self, tg_id: int):
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.tg_id == tg_id)
            )
            user = result.scalar_one()

            result = await session.execute(
                select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc())
            )
            return result.scalars().all()
    
    async def get_payments(self, tg_id: int):
        async with Session() as session:
            result = await session.execute(
                select(Payment).where(Payment.tg_id == tg_id).order_by(Payment.created_at.desc())
            )
            return result.scalars().all()
    
    async def get_all_orders(self):
        async with Session() as session:
            result = await session.execute(
                select(Order).order_by(Order.created_at.desc())
            )
            return result.scalars().all()
    
    async def get_processing_orders(self):
        """Получение всех заказов в статусе 'processing' с api_order_id"""
        async with Session() as session:
            result = await session.execute(
                select(Order).where(
                    Order.status == 'processing',
                    Order.api_order_id.isnot(None)
                )
            )
            return result.scalars().all()

    async def get_all_payments(self):
        async with Session() as session:
            result = await session.execute(
                select(Payment).order_by(Payment.created_at.desc())
            )
            return result.scalars().all()
    
    async def get_order_by_id(self, order_id: int):
        async with Session() as session:
            result = await session.execute(
                select(Order).where(Order.id == order_id)
            )
            return result.scalar_one_or_none()
    
    async def update_order_status(self, order_id: int, new_status: str):
        async with Session() as session:
            result = await session.execute(
                select(Order).where(Order.id == order_id)
            )
            order = result.scalar_one_or_none()
            if not order:
                return None
            order.status = new_status
            await session.commit()
            return order
    
    async def get_statistics(self):
        async with Session() as session:
            users_count = await session.execute(select(User))
            users = users_count.scalars().all()
            
            orders_count = await session.execute(select(Order))
            orders = orders_count.scalars().all()
            
            payments_count = await session.execute(select(Payment).where(Payment.status == 'paid'))
            payments = payments_count.scalars().all()
            
            total_revenue = 0
            for p in payments:
                try:
                    amount_str = p.amount.replace('₽', '').replace('$', '').replace(' ', '')
                    total_revenue += float(amount_str)
                except:
                    pass
            
            # Подсчет активных пользователей
            active_users = len([u for u in users if u.balance > 0 or await self._has_orders(u.id)])
            
            return {
                'users': len(users),
                'active_users': active_users,
                'orders': len(orders),
                'payments': len(payments),
                'revenue': round(total_revenue, 2)
            }
    
    async def _has_orders(self, user_id: int) -> bool:
        """Проверка наличия заказов у пользователя"""
        async with Session() as session:
            result = await session.execute(
                select(Order).where(Order.user_id == user_id).limit(1)
            )
            return result.scalar_one_or_none() is not None
    
    async def get_all_users(self):
        async with Session() as session:
            result = await session.execute(select(User))
            return result.scalars().all()
    
    # ======================== Catalog Methods =========================
    
    async def get_all_apps(self):
        async with Session() as session:
            result = await session.execute(select(App).order_by(App.name))
            return result.scalars().all()
    
    async def get_apps_sorted_telegram_first(self, search_query: str | None = None):
        """Приложения: Telegram первым, опционально поиск по имени"""
        async with Session() as session:
            stmt = select(App)
            if search_query and search_query.strip():
                q = f"%{search_query.strip().lower()}%"
                stmt = stmt.where(App.name.ilike(q))
            result = await session.execute(stmt.order_by(App.name))
            apps = result.scalars().all()
            # Telegram первым
            telegram_apps = [a for a in apps if "telegram" in a.name.lower() or "телеграм" in a.name.lower()]
            other = [a for a in apps if a not in telegram_apps]
            return telegram_apps + other
    
    async def get_app_by_id(self, app_id: int):
        async with Session() as session:
            result = await session.execute(select(App).where(App.id == app_id))
            return result.scalar_one_or_none()
    
    async def get_services_by_app(self, app_id: int):
        async with Session() as session:
            result = await session.execute(
                select(Service).where(Service.app_id == app_id).order_by(Service.name)
            )
            return result.scalars().all()
    
    async def get_service_by_id(self, service_id: int):
        async with Session() as session:
            result = await session.execute(select(Service).where(Service.id == service_id))
            return result.scalar_one_or_none()
    
    async def get_prices_by_service(self, service_id: int):
        async with Session() as session:
            result = await session.execute(
                select(Price).where(Price.service_id == service_id).order_by(Price.priceSell)
            )
            return result.scalars().all()
    
    async def get_price_by_id(self, price_id: int):
        async with Session() as session:
            result = await session.execute(select(Price).where(Price.id == price_id))
            return result.scalar_one_or_none()
    
    async def add_app(self, name: str):
        async with Session() as session:
            app = App(name=name)
            session.add(app)
            await session.commit()
            return app
    
    async def add_service(self, app_id: int, name: str, service_api_id: int | None = None):
        async with Session() as session:
            service = Service(app_id=app_id, name=name, service_api_id=service_api_id)
            session.add(service)
            await session.commit()
            return service
    
    async def add_price(self, service_id: int, name: str, price_sell: float, price_buy: float, markup_percent: float = 250.0, min_quantity: int | None = None, max_quantity: int | None = None):
        async with Session() as session:
            price = Price(
                service_id=service_id, 
                name=name, 
                priceSell=price_sell, 
                priceBuy=price_buy, 
                markup_percent=markup_percent,
                min_quantity=min_quantity,
                max_quantity=max_quantity
            )
            session.add(price)
            await session.commit()
            return price
    
    async def update_markup(self, price_id: int, markup_percent: float):
        """Обновление наценки для пакета"""
        async with Session() as session:
            result = await session.execute(select(Price).where(Price.id == price_id))
            price = result.scalar_one_or_none()
            if not price:
                return False
            price.markup_percent = markup_percent
            # Пересчитываем цену продажи
            price.priceSell = price.priceBuy * (1 + markup_percent / 100)
            await session.commit()
            return True
    
    async def recalculate_all_prices(self, global_markup: float):
        """Пересчет всех цен продажи на основе новой глобальной наценки"""
        async with Session() as session:
            result = await session.execute(select(Price))
            prices = result.scalars().all()
            
            updated_count = 0
            for price in prices:
                price.markup_percent = global_markup
                price.priceSell = price.priceBuy * (1 + global_markup / 100)
                updated_count += 1
            
            await session.commit()
            return updated_count


    async def get_user_by_referral_code(self, code: str):
        """Получение пользователя по реферальному коду"""
        async with Session() as session:
            result = await session.execute(select(User).where(User.referral_code == code))
            return result.scalar_one_or_none()

    async def set_privacy_accepted(self, tg_id: int):
        """Отметить, что пользователь принял политику конфиденциальности"""
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            if not user:
                return False
            user.privacy_accepted = 1
            await session.commit()
            return True

    async def set_user_ban(self, tg_id: int, banned: bool):
        """Бан / разбан пользователя в боте"""
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            if not user:
                return False
            user.is_banned = 1 if banned else 0
            await session.commit()
            return True

    async def is_user_banned(self, tg_id: int) -> bool:
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            return bool(user and getattr(user, 'is_banned', 0))

    async def search_users(self, query: str):
        """Поиск пользователей по tg_id или username (LIKE)"""
        async with Session() as session:
            q = query.strip()
            stmt = None
            if q.isdigit():
                stmt = select(User).where(User.tg_id == int(q))
            else:
                like = f"%{q.lower()}%"
                stmt = select(User).where(
                    or_(User.username.ilike(like), User.first_name.ilike(like))
                )
            result = await session.execute(stmt)
            return result.scalars().all()
    
    async def add_referral_earnings(self, referrer_id: int, amount: float, referral_percent: float):
        """Начисление заработка рефереру"""
        async with Session() as session:
            result = await session.execute(select(User).where(User.id == referrer_id))
            user = result.scalar_one_or_none()
            if not user:
                return False
            earnings = amount * (referral_percent / 100)
            user.balance += int(earnings)
            user.total_earned += int(earnings)
            await session.commit()
            return True
    
    async def set_user_role(self, tg_id: int, role: str):
        """Установка роли пользователя"""
        async with Session() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            if not user:
                return False
            user.role = role
            await session.commit()
            return True
    
    async def get_support_accounts(self) -> list:
        """Список юзернеймов поддержки (без @)"""
        async with Session() as session:
            result = await session.execute(select(SupportAccount).order_by(SupportAccount.id))
            rows = result.scalars().all()
            return [r.username for r in rows]
    
    async def add_support_account(self, username: str) -> bool:
        """Добавить аккаунт поддержки. username без @"""
        username = str(username).strip().lstrip('@').lower()
        if not username:
            return False
        async with Session() as session:
            result = await session.execute(select(SupportAccount).where(SupportAccount.username == username))
            if result.scalar_one_or_none():
                return False
            session.add(SupportAccount(username=username))
            await session.commit()
            return True
    
    async def remove_support_account(self, username: str) -> bool:
        """Удалить аккаунт поддержки"""
        username = str(username).strip().lstrip('@').lower()
        async with Session() as session:
            result = await session.execute(select(SupportAccount).where(SupportAccount.username == username))
            row = result.scalar_one_or_none()
            if not row:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def reset_statistics(self):
        """Сброс статистики (удаление всех заказов, платежей, обнуление балансов)"""
        async with Session() as session:
            # Удаляем все заказы
            orders_result = await session.execute(select(Order))
            for order in orders_result.scalars().all():
                await session.delete(order)
            
            # Удаляем все платежи
            payments_result = await session.execute(select(Payment))
            for payment in payments_result.scalars().all():
                await session.delete(payment)
            
            # Обнуляем балансы (кроме админов)
            users_result = await session.execute(select(User).where(User.role != 'admin'))
            for user in users_result.scalars().all():
                user.balance = 0
                user.total_earned = 0
            
            # Удаляем логи
            logs_result = await session.execute(select(Logs))
            for log in logs_result.scalars().all():
                await session.delete(log)
            
            await session.commit()
            return True
        
async def main():
    db = Database()
    await db.init()

if __name__ == '__main__':
    asyncio.run(main())