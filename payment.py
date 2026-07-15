import asyncio
from aiocryptopay import AioCryptoPay, Networks
from functions.orderCode import generate_secure_random_string
from functions.db.db import Database


class CBPayment():
    def __init__(self):
        self.db = Database()
        self.crypto = None

    async def init(self):
        await self.db.init()
        self.crypto = AioCryptoPay(
        token='524099:AAi8XHUvApS0CytXnr3E0ELjXMe54U1Lrrx',
        network=Networks.MAIN_NET
        )

    async def create_invoice(self, data:dict) -> tuple[bool, str]:
        if self.crypto is None:
            await self.init()
        user_id = data['user_id']
        description = data['description']
        amount = float(data['amount'][:-1])
        f = data['amount'][-1]
        order = data['order']

        try:
            invoice = await self.crypto.create_invoice(
                currency_type='fiat',
                fiat="RUB" if f == "R" else "USD",
                expires_in=3600,
                amount=amount,
                description=f'{description} {order}'
            )
            await self.db.create_payment(tg_id=user_id, amount=amount, method="crypto", order=order, fiat='RUB' if f == 'R' else "USD", invoice_id=invoice.invoice_id)
            return True, invoice.mini_app_invoice_url
        except Exception as e:
            await self.db.update_payment_status(code=order, new_status=f'error: {e}')
            return False, f'error: {e}'

        
    async def check_payment(self, data: dict) -> bool:
        if self.crypto is None:
            await self.init()

        order = data['order']
        amount = data['amount'][:-1]
        user_id = data['user_id']
        invoice_id = await self.db.get_invoice_id(order=order)
        invoice_info = await self.crypto.get_invoices(invoice_ids=invoice_id)
        
        if isinstance(invoice_info, list):
            invoice_info = invoice_info[0]
        
        if invoice_info.status == 'paid':
            await self.db.update_payment_status(code=order, new_status='paid')
            await self.db.add_balance(tg_id=user_id, amount=amount)
            print(invoice_info)
            return True
        else:
            return False


async def main():
    """Основная асинхронная функция"""
    order = generate_secure_random_string()
    data = {
        'order': order,
        'amount': "100R",
        'user_id': '123',
        'description': 'test',
    }
    cb = CBPayment()
    print(await cb.create_invoice(data))
    for _ in range(1000):
        await asyncio.sleep(10)
        print(await cb.check_payment(data))
    if cb.crypto:
        await cb.crypto.close()

if __name__ == "__main__":
    pass