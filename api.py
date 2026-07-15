"""
API модуль для автоматического выполнения заказов
Этот модуль можно использовать для интеграции с внешними системами
"""

from functions.db.db import Database
from typing import Optional, Dict, List
from datetime import datetime


class OrderAPI:
    """API класс для работы с заказами"""
    
    def __init__(self):
        self.db = Database()
    
    async def init(self):
        """Инициализация базы данных"""
        await self.db.init()
    
    async def create_order(
        self,
        tg_id: int,
        service: str,
        quantity: int = 1,
        payment_id: Optional[int] = None
    ) -> Dict:
        """
        Создание нового заказа
        
        Args:
            tg_id: Telegram ID пользователя
            service: Название услуги
            quantity: Количество
            payment_id: ID платежа (опционально)
        
        Returns:
            Dict с результатом создания заказа
        """
        try:
            user = await self.db.get_user(tg_id=tg_id)
            if not user:
                return {
                    "success": False,
                    "error": "User not found",
                    "error_code": "USER_NOT_FOUND"
                }
            
            order = await self.db.create_order(
                tg_id=tg_id,
                service=service,
                quantity=quantity,
                payment_id=payment_id
            )
            
            return {
                "success": True,
                "order": {
                    "id": order.id,
                    "user_id": order.user_id,
                    "service": order.service,
                    "quantity": order.quantity,
                    "status": order.status,
                    "created_at": order.created_at.isoformat() if order.created_at else None
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def update_order_status(
        self,
        order_id: int,
        new_status: str
    ) -> Dict:
        """
        Обновление статуса заказа
        
        Args:
            order_id: ID заказа
            new_status: Новый статус (new, processing, completed, cancelled, failed)
        
        Returns:
            Dict с результатом обновления
        """
        try:
            valid_statuses = ['new', 'processing', 'completed', 'cancelled', 'failed']
            if new_status not in valid_statuses:
                return {
                    "success": False,
                    "error": f"Invalid status. Allowed: {', '.join(valid_statuses)}",
                    "error_code": "INVALID_STATUS"
                }
            
            order = await self.db.update_order_status(
                order_id=order_id,
                new_status=new_status
            )
            
            if not order:
                return {
                    "success": False,
                    "error": "Order not found",
                    "error_code": "ORDER_NOT_FOUND"
                }
            
            return {
                "success": True,
                "order": {
                    "id": order.id,
                    "status": order.status,
                    "updated_at": datetime.utcnow().isoformat()
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def get_order(self, order_id: int) -> Dict:
        """
        Получение информации о заказе
        
        Args:
            order_id: ID заказа
        
        Returns:
            Dict с информацией о заказе
        """
        try:
            order = await self.db.get_order_by_id(order_id=order_id)
            
            if not order:
                return {
                    "success": False,
                    "error": "Order not found",
                    "error_code": "ORDER_NOT_FOUND"
                }
            
            return {
                "success": True,
                "order": {
                    "id": order.id,
                    "user_id": order.user_id,
                    "service": order.service,
                    "quantity": order.quantity,
                    "status": order.status,
                    "created_at": order.created_at.isoformat() if order.created_at else None
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def get_user_orders(
        self,
        tg_id: int,
        status: Optional[str] = None
    ) -> Dict:
        """
        Получение всех заказов пользователя
        
        Args:
            tg_id: Telegram ID пользователя
            status: Фильтр по статусу (опционально)
        
        Returns:
            Dict со списком заказов
        """
        try:
            orders = await self.db.get_orders(tg_id=tg_id)
            
            orders_list = []
            for order in orders:
                if status and order.status != status:
                    continue
                
                orders_list.append({
                    "id": order.id,
                    "service": order.service,
                    "quantity": order.quantity,
                    "status": order.status,
                    "created_at": order.created_at.isoformat() if order.created_at else None
                })
            
            return {
                "success": True,
                "orders": orders_list,
                "count": len(orders_list)
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def get_orders_by_status(self, status: str) -> Dict:
        """
        Получение всех заказов с определенным статусом
        
        Args:
            status: Статус заказов
        
        Returns:
            Dict со списком заказов
        """
        try:
            all_orders = await self.db.get_all_orders()
            
            orders_list = []
            for order in all_orders:
                if order.status == status:
                    orders_list.append({
                        "id": order.id,
                        "user_id": order.user_id,
                        "service": order.service,
                        "quantity": order.quantity,
                        "status": order.status,
                        "created_at": order.created_at.isoformat() if order.created_at else None
                    })
            
            return {
                "success": True,
                "orders": orders_list,
                "count": len(orders_list)
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def process_order(self, order_id: int) -> Dict:
        """
        Автоматическая обработка заказа (перевод в статус processing)
        
        Args:
            order_id: ID заказа
        
        Returns:
            Dict с результатом обработки
        """
        try:
            order = await self.db.get_order_by_id(order_id=order_id)
            
            if not order:
                return {
                    "success": False,
                    "error": "Order not found",
                    "error_code": "ORDER_NOT_FOUND"
                }
            
            if order.status != 'new':
                return {
                    "success": False,
                    "error": f"Order already processed. Current status: {order.status}",
                    "error_code": "ORDER_ALREADY_PROCESSED"
                }
            
            # Обновляем статус на processing
            updated_order = await self.db.update_order_status(
                order_id=order_id,
                new_status='processing'
            )
            
            return {
                "success": True,
                "order": {
                    "id": updated_order.id,
                    "status": updated_order.status,
                    "message": "Order is being processed"
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }
    
    async def complete_order(self, order_id: int) -> Dict:
        """
        Завершение заказа (перевод в статус completed)
        
        Args:
            order_id: ID заказа
        
        Returns:
            Dict с результатом завершения
        """
        try:
            order = await self.db.get_order_by_id(order_id=order_id)
            
            if not order:
                return {
                    "success": False,
                    "error": "Order not found",
                    "error_code": "ORDER_NOT_FOUND"
                }
            
            if order.status == 'completed':
                return {
                    "success": False,
                    "error": "Order already completed",
                    "error_code": "ORDER_ALREADY_COMPLETED"
                }
            
            updated_order = await self.db.update_order_status(
                order_id=order_id,
                new_status='completed'
            )
            
            return {
                "success": True,
                "order": {
                    "id": updated_order.id,
                    "status": updated_order.status,
                    "message": "Order completed successfully"
                }
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "INTERNAL_ERROR"
            }


# Пример использования:
"""
async def example():
    api = OrderAPI()
    await api.init()
    
    # Создание заказа
    result = await api.create_order(
        tg_id=123456789,
        service="YouTube Premium",
        quantity=1
    )
    print(result)
    
    # Обработка заказа
    if result["success"]:
        order_id = result["order"]["id"]
        process_result = await api.process_order(order_id)
        print(process_result)
        
        # Завершение заказа
        complete_result = await api.complete_order(order_id)
        print(complete_result)
    
    # Получение заказов пользователя
    user_orders = await api.get_user_orders(tg_id=123456789)
    print(user_orders)
"""

