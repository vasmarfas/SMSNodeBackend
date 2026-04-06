import asyncio
import time
import httpx
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("load_test")

# Конфигурация
BASE_URL = "http://localhost:8007"
USERNAME = "admin_test"  
PASSWORD = "admin_password"  
TOTAL_REQUESTS = 100
PHONE_NUMBER = "+79990000000"
TEXT_MESSAGE = "Load test message"

async def setup_environment(client):
    """Логинит админа, создает шлюз, симку и назначает ее админу."""
    # 2. Логин
    response = await client.post(f"{BASE_URL}/auth/token", data={
        "username": USERNAME,
        "password": PASSWORD
    })
    if response.status_code != 200:
        logger.error(f"Failed to login: {response.text}")
        return None, None
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Получение профиля (чтобы узнать свой ID)
    me_resp = await client.get(f"{BASE_URL}/auth/me", headers=headers)
    user_id = me_resp.json()["id"]

    # 3. Создаем шлюз
    gw_resp = await client.post(f"{BASE_URL}/api/v1/admin/gateways", json={
        "name": "Mock Gateway",
        "type": "goip_http",
        "host": "127.0.0.1",
        "port": 9991,
        "username": "admin",
        "password": "pwd"
    }, headers=headers)
    if gw_resp.status_code not in (200, 201, 409):
        logger.error(f"Failed to create gateway: {gw_resp.text}")
    
    gw_id = 1
    if gw_resp.status_code == 201:
        gw_id = gw_resp.json()["id"]
    else:
        # Если шлюз уже есть, получаем его ID
        gws = await client.get(f"{BASE_URL}/api/v1/admin/gateways", headers=headers)
        if gws.json():
            gw_id = gws.json()[0]["id"]
            
    # 4. Создаем SIM-карту
    sim_resp = await client.post(f"{BASE_URL}/api/v1/admin/gateways/{gw_id}/sims", json={
        "port_number": 1,
        "phone_number": "+70000000000"
    }, headers=headers)
    
    sim_id = 1
    if sim_resp.status_code == 201:
        sim_id = sim_resp.json()["id"]
    else:
        sims = await client.get(f"{BASE_URL}/api/v1/admin/gateways/{gw_id}/sims", headers=headers)
        if sims.json():
            sim_id = sims.json()[0]["id"]
            
    # 5. Назначаем SIM-карту пользователю
    await client.post(f"{BASE_URL}/api/v1/admin/users/{user_id}/sims/{sim_id}", headers=headers)
    
    return token, sim_id

async def send_sms(client, token, sim_id, index):
    """Отправка одного SMS."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "phone": PHONE_NUMBER,
        "text": f"{TEXT_MESSAGE} #{index}",
        "sim_card_id": sim_id
    }
    
    start_time = time.monotonic()
    try:
        response = await client.post(
            f"{BASE_URL}/api/v1/user/messages/send", 
            json=payload, 
            headers=headers
        )
        duration = time.monotonic() - start_time
        
        if response.status_code == 202:
            return True, duration
        else:
            logger.warning(f"Request {index} failed: {response.status_code} - {response.text}")
            return False, duration
    except Exception as e:
        logger.error(f"Request {index} error: {e}")
        return False, 0

async def main():
    async with httpx.AsyncClient() as client:
        logger.info("Setting up environment...")
        token, sim_id = await setup_environment(client)
        if not token:
            return

        logger.info(f"Starting load test: {TOTAL_REQUESTS} requests...")
        
        # Массовая отправка (burst)
        start_time = time.monotonic()
        tasks = [send_sms(client, token, sim_id, i) for i in range(TOTAL_REQUESTS)]
        results = await asyncio.gather(*tasks)
        end_time = time.monotonic()
        
        total_time = end_time - start_time
        success_count = sum(1 for r in results if r[0])
        avg_req_time = sum(r[1] for r in results) / len(results) if results else 0
        
        logger.info("-" * 40)
        logger.info(f"API Load Test Results:")
        logger.info(f"Total Requests: {TOTAL_REQUESTS}")
        logger.info(f"Successful: {success_count}")
        logger.info(f"Failed: {TOTAL_REQUESTS - success_count}")
        logger.info(f"Total API Time (burst): {total_time:.4f}s")
        logger.info(f"Avg Request Time: {avg_req_time:.4f}s")
        logger.info(f"Requests per Second: {TOTAL_REQUESTS / total_time:.2f}")
        logger.info("-" * 40)
        logger.info("Now check backend logs for queue processing speed.")

if __name__ == "__main__":
    asyncio.run(main())
