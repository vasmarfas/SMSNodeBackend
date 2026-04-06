import asyncio
import logging
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mock_goip")

# Конфигурация
PORT = 9991  # Порт, на который бэкенд будет слать запросы
HOST = "0.0.0.0"

async def handle_send_sms(request):
    """Обработчик запроса на отправку SMS (эмуляция GoIP HTTP)."""
    try:
        # GoIP обычно принимает GET/POST параметры
        # Пример: /default/en_US/send.html?u=admin&p=admin&l=1&n=phone&m=text
        params = request.query
        phone = params.get("n", "unknown")
        text = params.get("m", "unknown")
        line = params.get("l", "1")
        
        logger.info(f"Received SMS request: Line={line}, To={phone}, Text={text[:20]}...")
        
        # Имитация задержки обработки шлюзом (опционально)
        await asyncio.sleep(0.1)
        
        # Ответ GoIP (обычно просто текст или HTML)
        return web.Response(text="Sending,L1:ID:12345 OK")
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return web.Response(status=500, text="Error")

async def init_app():
    app = web.Application()
    # Роут, куда бэкенд шлет запросы (подстроить под реализацию GoIPGateway)
    # В goip_http.py используется url: f"http://{self.host}:{self.port}/default/en_US/send.html"
    app.router.add_get("/default/en_US/send.html", handle_send_sms)
    app.router.add_post("/default/en_US/send.html", handle_send_sms)
    return app

if __name__ == "__main__":
    logger.info(f"Starting Mock GoIP Gateway on {HOST}:{PORT}")
    web.run_app(init_app(), host=HOST, port=PORT)
