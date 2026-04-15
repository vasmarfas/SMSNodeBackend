from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from config_reader import config

session = None
if config.TG_PROXY:
    session = AiohttpSession(proxy=config.TG_PROXY)

bot = Bot(token=config.bot_token.get_secret_value(), session=session)
dp = Dispatcher()
