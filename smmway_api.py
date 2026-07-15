"""
Модуль для работы с API smmway.ru
Документация: https://smmway.ru/developer
"""

import aiohttp
import asyncio
from typing import Optional, Dict, List
from functions.db.db import Database


class SMMWayAPI:
    """Класс для работы с API smmway.ru"""
    
    def __init__(self, api_key: str, api_url: str = None):
        self.api_key = api_key
        # Пробуем разные варианты базового URL
        self.api_url = api_url or "https://smmway.ru/api/v2"
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None

    async def init(self):
        """Инициализация сессии"""
        await self.db.init()
        self.session = aiohttp.ClientSession()

    async def close(self):
        """Закрытие сессии"""
        if self.session:
            await self.session.close()

    async def _request(self, action: str, data: Optional[Dict] = None) -> Dict:
        """
        Выполнение запроса к API

        Args:
            action: Действие API (balance, services, add, status)
            data: Данные для отправки

        Returns:
            Ответ от API
        """
        if not self.session:
            await self.init()

        url = self.api_url

        request_data = data or {}
        request_data['key'] = self.api_key
        request_data['action'] = action

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Hide key from logs
        log_data = request_data.copy()
        if 'key' in log_data:
            log_data['key'] = f"{log_data['key'][:4]}..."

        print(f"🔗 Запрос к API: {url}, action: {action}")
        print(f"📦 Данные запроса: {log_data}")

        try:
            async with self.session.post(url, headers=headers, json=request_data) as response:
                response_text = await response.text()
                print(f"API Response Status: {response.status}")

                if response.status != 200:
                    return {
                        "error": f"HTTP {response.status}: {response_text[:500]}",
                        "success": False,
                        "status": response.status
                    }

                try:
                    result = await response.json()
                    # API может вернуть ошибку в формате {"error": "..."}
                    if isinstance(result, dict) and "error" in result:
                        print(f"❌ API вернул ошибку: {result['error']}")
                        return {"error": result["error"], "success": False}
                    return result
                except Exception as json_error:
                    return {
                        "error": f"JSON decode error: {json_error}. Response: {response_text[:200]}",
                        "success": False,
                        "raw_response": response_text
                    }
        except aiohttp.ClientError as e:
            return {"error": f"Connection error: {str(e)}", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    async def get_balance(self) -> Dict:
        """Получение баланса аккаунта"""
        return await self._request("balance")

    async def get_services(self) -> List[Dict]:
        """Получение списка всех услуг"""
        return await self._request("services")

    async def create_order(
        self,
        service_id: int,
        link: str,
        quantity: int,
        **kwargs
    ) -> Dict:
        """
        Создание заказа

        Args:
            service_id: ID услуги
            link: Ссылка для выполнения заказа
            quantity: Количество
            **kwargs: Дополнительные параметры

        Returns:
            Результат создания заказа
        """
        data = {
            "service": service_id,
            "link": link,
            "quantity": quantity,
            **kwargs
        }
        return await self._request("add", data)

    async def get_order_status(self, order_id: int) -> Dict:
        """Получение статуса заказа"""
        return await self._request("status", data={"order": order_id})

    async def sync_catalog_to_db(self, global_markup: float):
        """
        Синхронизация каталога из API в базу данных
        Загружает приложения, услуги и цены
        """
        try:
            print("🔄 Начинаю синхронизацию каталога...")
            services_data = await self.get_services()

            # The API returns a list of services on success, or a dict with an error.
            if isinstance(services_data, dict) and "error" in services_data:
                error_msg = services_data.get("error", "Unknown error")
                print(f"❌ Ошибка API при получении услуг: {error_msg}")
                return {"success": False, "error": f"API Error: {error_msg}"}

            if not isinstance(services_data, list):
                print(f"❌ Не удалось найти список услуг в ответе API. Ответ не является списком.")
                print(f"Полный ответ: {services_data}")
                return {"success": False, "error": f"Services list not found in API response. Response was not a list: {services_data}"}

            services = services_data
            print(f"✅ Найдено услуг в API: {len(services)}")

            # Группируем по категориям/приложениям
            apps_dict = {}

            for service in services:
                if not isinstance(service, dict):
                    print(f"⚠️ Пропущен невалидный сервис: {service}")
                    continue

                app_name = service.get("category", "Другое").strip()
                
                if app_name.lower() == "бесплатные услуги": # Игнорируем категорию "бесплатные услуги"
                    continue

                if not app_name:
                    app_name = "Другое"

                if app_name not in apps_dict:
                    apps_dict[app_name] = []

                apps_dict[app_name].append(service)

            # Сохраняем в БД
            for app_name, app_services in apps_dict.items():
                # Создаем или получаем приложение
                app = await self.db.get_all_apps()
                app_obj = next((a for a in app if a.name == app_name), None)

                if not app_obj:
                    app_obj = await self.db.add_app(app_name)

                # Добавляем услуги
                for service_item in app_services:
                    if not isinstance(service_item, dict):
                        continue

                    service_name = service_item.get("name", "Услуга").strip()
                    api_service_id = service_item.get("service")

                    if not api_service_id:
                        continue

                    # Проверяем, существует ли услуга
                    existing_services = await self.db.get_services_by_app(app_obj.id)
                    service_obj = next((s for s in existing_services if s.service_api_id == api_service_id), None)

                    if not service_obj:
                        service_obj = await self.db.add_service(app_obj.id, service_name, service_api_id=api_service_id)
                    else:
                        # Update name if changed
                        if service_obj.name != service_name:
                            service_obj.name = service_name
                            from functions.db.db import Session
                            async with Session() as session:
                                session.add(service_obj)
                                await session.commit()

                    # Добавляем/обновляем пак. В этой API 1 услуга = 1 пак.
                    rate_value = service_item.get("rate")
                    if rate_value is None:
                        continue

                    try:
                        price_buy = float(rate_value)
                    except (ValueError, TypeError):
                        continue

                    if price_buy <= 0:
                        continue

                    min_q = service_item.get('min', '')
                    max_q = service_item.get('max', '')
                    pack_name = "Цена за 1 шт."

                    existing_prices = await self.db.get_prices_by_service(service_obj.id)
                    # Since 1 service = 1 pack, there should be only one.
                    price_obj = existing_prices[0] if existing_prices else None

                    if not price_obj:
                        price_buy_per_1000 = price_buy
                        price_buy_per_item = price_buy_per_1000 / 1000.0
                        markup = global_markup
                        price_sell_per_item = price_buy_per_item * (1 + markup / 100)
                        await self.db.add_price(
                            service_id=service_obj.id,
                            name=pack_name,
                            price_sell=price_sell_per_item,
                            price_buy=price_buy_per_item,
                            markup_percent=markup,
                            min_quantity=min_q,
                            max_quantity=max_q
                        )
                    else:
                        # Update price
                        from functions.db.db import Session, Price, select
                        async with Session() as session:
                            result = await session.execute(select(Price).where(Price.id == price_obj.id))
                            price_to_update = result.scalar_one()
                            
                            price_buy_per_1000 = price_buy
                            price_buy_per_item = price_buy_per_1000 / 1000.0
                            price_to_update.name = pack_name
                            price_to_update.priceBuy = price_buy_per_item
                            price_to_update.markup_percent = global_markup
                            price_to_update.priceSell = price_buy_per_item * (1 + global_markup / 100)
                            price_to_update.min_quantity = min_q
                            price_to_update.max_quantity = max_q
                            await session.commit()

            return {"success": True, "message": "Catalog synced successfully"}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}


# Пример использования:
"""
async def main():
    api = SMMWayAPI(api_key="your_api_key_here")
    await api.init()
    
    # Синхронизация каталога
    result = await api.sync_catalog_to_db()
    print(result)
    
    # Создание заказа
    order = await api.create_order(
        service_id=123,
        link="https://example.com",
        quantity=100
    )
    print(order)
    
    await api.close()

if __name__ == "__main__":
    asyncio.run(main())
"""
