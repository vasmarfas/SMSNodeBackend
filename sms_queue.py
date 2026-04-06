"""
Асинхронная очередь отправки SMS (asyncio.Queue, внутри процесса).

Сериализует исходящие сообщения к шлюзу, задаёт троттлинг по каналам
и снижает риск перегрузки оборудования при параллельных запросах.
"""

import asyncio
import logging
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from core.db.database import AsyncSessionLocal
from core.db.models import Message, MessageDirectionEnum, MessageStatusEnum, SimCard
from config_reader import config

logger = logging.getLogger(__name__)

# Структура задачи в очереди

@dataclass
class SmsJob:
    """Одна задача на отправку SMS."""
    job_id: str
    gateway_id: int
    port_num: int
    phone: str
    text: str
    sim_card_id: Optional[int] = None
    retry_count: int = 0
    max_retries: Optional[int] = None


# Синглтон-очередь

_queue: asyncio.Queue[SmsJob] = asyncio.Queue(maxsize=500)
_worker_task: Optional[asyncio.Task] = None

# Троттлинг (ограничение частоты) по каналам
# (gateway_id, port_num) -> timestamp последней успешной отправки
_last_sent: dict[tuple[int, int], float] = {}

# Индивидуальные переопределения интервалов для каналов (опционально)
# (gateway_id, port_num) -> интервал в секундах
_per_channel_interval: dict[tuple[int, int], float] = {}

# Простая статистика
_stats = {"sent_ok": 0, "failed": 0, "retried": 0}


def get_stats() -> dict:
    """Вернуть копию статистики очереди."""
    return _stats.copy()


async def enqueue_sms(
    gateway_id: int,
    port_num: int,
    phone: str,
    text: str,
    sim_card_id: Optional[int] = None,
) -> str:
    """
    Добавить задачу отправки SMS в очередь.

    Returns:
        job_id: уникальный идентификатор задачи для отслеживания.
    """
    job_id = str(uuid.uuid4())[:8]
    job = SmsJob(
        job_id=job_id,
        gateway_id=gateway_id,
        port_num=port_num,
        phone=phone,
        text=text,
        sim_card_id=sim_card_id,
    )
    await _queue.put(job)
    logger.info(f"SMS enqueued [{job_id}]: gw={gateway_id} port={port_num} to={phone}")
    return job_id


async def _process_job(job: SmsJob) -> None:
    """
    Обработать одну задачу: отправить SMS через GatewayService,
    сохранить результат в PostgreSQL messages.
    """
    from core.gateways.manager import gateway_manager  # Ленивый импорт (избегаем circular)

    # 1. Троттлинг (ограничение частоты) на уровне SIM
    channel_key = (job.gateway_id, job.port_num)
    
    # Определяем интервал: сначала индивидуальный, затем глобальный из конфига
    interval = _per_channel_interval.get(channel_key, config.MIN_INTERVAL_PER_CHANNEL_SEC)
    
    last_ts = _last_sent.get(channel_key, 0.0)
    now = time.monotonic()
    time_passed = now - last_ts
    
    if time_passed < interval:
        wait_time = interval - time_passed
        logger.info(
            f"Throttle SIM (gw={job.gateway_id}, port={job.port_num}): "
            f"wait {wait_time:.2f}s before processing [{job.job_id}]"
        )
        await asyncio.sleep(wait_time)

    logger.info(f"SMS worker: processing [{job.job_id}] to {job.phone}")

    result = await gateway_manager.send_sms(
        gateway_id=job.gateway_id,
        port_num=job.port_num,
        phone=job.phone,
        text=job.text,
    )

    should_retry = job.max_retries is None or job.retry_count < job.max_retries

    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(Message).where(Message.gateway_task_id == job.job_id)
        )
        msg = existing.scalar_one_or_none()

        # Создаём/обновляем запись в БД только когда SMS действительно
        # либо успешно отправлена, либо все попытки исчерпаны (финальный FAIL).
        if result.success or not should_retry:
            if msg is None:
                msg = Message(
                    sim_card_id=job.sim_card_id,
                    external_phone=job.phone,
                    direction=MessageDirectionEnum.OUTGOING,
                    text=job.text,
                    status=MessageStatusEnum.SENT_OK
                    if result.success
                    else MessageStatusEnum.FAILED,
                    error_text=None if result.success else result.message,
                    gateway_task_id=job.job_id,
                )
                session.add(msg)
            else:
                msg.status = (
                    MessageStatusEnum.SENT_OK
                    if result.success
                    else MessageStatusEnum.FAILED
                )
                msg.error_text = None if result.success else result.message

            await session.commit()

    if result.success:
        # Обновляем таймстемп последней успешной отправки для канала
        _last_sent[channel_key] = time.monotonic()
        _stats["sent_ok"] += 1
        logger.info(f"SMS sent [{job.job_id}]: {result.message}")
    else:
        _stats["failed"] += 1
        logger.warning(f"SMS failed [{job.job_id}]: {result.message}")
        if should_retry:
            _stats["retried"] += 1
            job.retry_count += 1
            logger.info(
                f"Retrying [{job.job_id}] attempt {job.retry_count}/{job.max_retries or '∞'}"
            )
            # Экспоненциальная задержка перед повтором (не путать с троттлингом канала)
            await asyncio.sleep(5 * job.retry_count)
            await _queue.put(job)


async def _worker() -> None:
    """
    Фоновый воркер: бесконечно берёт задачи из очереди и обрабатывает.
    Запускается один раз при старте приложения.
    """
    logger.info("SMS queue worker started.")
    while True:
        try:
            job = await _queue.get()
            await _process_job(job)
            _queue.task_done()
            # Минимальная пауза между отправками (настраиваемая через env)
            await asyncio.sleep(max(0.0, float(getattr(config, "SMS_WORKER_MIN_SLEEP_SEC", 0.5))))
        except asyncio.CancelledError:
            logger.info("SMS queue worker stopped.")
            break
        except Exception as e:
            logger.error(f"SMS worker unexpected error: {e}", exc_info=True)
            await asyncio.sleep(1)


def start_sms_worker() -> asyncio.Task:
    """
    Запустить фоновый воркер очереди SMS.
    Вызывается из main.py один раз при старте.
    """
    global _worker_task
    _worker_task = asyncio.create_task(_worker())
    return _worker_task


def stop_sms_worker() -> None:
    """Остановить воркер при завершении приложения."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        _worker_task = None


def queue_size() -> int:
    """Текущий размер очереди (для мониторинга)."""
    return _queue.qsize()
