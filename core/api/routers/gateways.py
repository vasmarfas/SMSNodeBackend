"""
Роутер управления шлюзами — /api/v1/admin/gateways
Доступно только администраторам.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.api.dependencies import require_admin, get_gateway_manager
from core.db.database import get_db
from core.db.models import Gateway, GatewayTypeEnum, SimCard, User, GoIPEvent, GoIPEventTypeEnum
from core.gateways.manager import GatewayManager

router = APIRouter(prefix="/api/v1/admin/gateways", tags=["Gateways"])


class GatewayResponse(BaseModel):
    id: int
    name: str
    type: str
    host: str
    port: int
    username: str
    is_active: bool
    last_seen: Optional[datetime]
    last_status: Optional[str]

    class Config:
        from_attributes = True


class GatewayCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: GatewayTypeEnum
    host: str = Field(..., min_length=7)
    port: int = Field(default=9991, ge=1, le=65535)
    username: str = Field(default="admin", max_length=50)
    password: str = Field(..., min_length=1)


class GatewayUpdateRequest(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None


class GatewayTestResult(BaseModel):
    gateway_id: int
    online: bool
    detail: str
    latency_ms: Optional[float]


class SimCardResponse(BaseModel):
    id: int
    port_number: int
    phone_number: Optional[str]
    label: Optional[str]
    status: str
    assigned_user_id: Optional[int]

    class Config:
        from_attributes = True


class SimCardCreateRequest(BaseModel):
    port_number: int = Field(..., ge=1, le=256)
    phone_number: Optional[str] = None


class DiscoveredChannel(BaseModel):
    """Канал из keepalive, по которому ещё нет записи SimCard (или можно обновить)."""
    host: str
    goip_id: str
    port_number: int
    phone_number: Optional[str] = None
    signal: Optional[str] = None
    gsm_status: Optional[str] = None
    gateway_id: Optional[int] = None
    can_add: bool = True


class DiscoveredSimAddRequest(BaseModel):
    """Добавить SIM по обнаруженному каналу (с подтверждения админа)."""
    gateway_id: int = Field(..., ge=1)
    port_number: int = Field(..., ge=1, le=8)
    phone_number: Optional[str] = None


@router.get(
    "/discovered-sims",
    response_model=List[DiscoveredChannel],
    summary="Каналы из keepalive без записи SIM (для добавления с подтверждения)",
)
async def list_discovered_sims(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
    minutes: int = 10,
):
    """Список каналов из последних keepalive, по которым ещё нет SimCard."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    r = await db.execute(
        select(GoIPEvent)
        .where(
            and_(
                GoIPEvent.event_type == GoIPEventTypeEnum.KEEPALIVE,
                GoIPEvent.received_at >= since,
            )
        )
        .order_by(GoIPEvent.received_at.desc())
    )
    events = r.scalars().all()
    # Группируем по (host, goip_id), берём последний
    by_key: dict[tuple[str, str], GoIPEvent] = {}
    for ev in events:
        try:
            port_num = int(ev.goip_id)
        except ValueError:
            continue
        if 1001 <= port_num <= 1008:
            key = (ev.host, ev.goip_id)
            if key not in by_key:
                by_key[key] = ev

    out: List[DiscoveredChannel] = []
    for (host, goip_id), ev in by_key.items():
        port_number = int(goip_id) - 1000
        payload = ev.payload_json or {}
        phone_number = payload.get("num") or None
        signal = payload.get("signal")
        gsm_status = payload.get("gsm_status")

        gw_r = await db.execute(select(Gateway.id).where(Gateway.host == host).limit(1))
        gateway_id = gw_r.scalars().first()
        can_add = True
        if gateway_id is not None:
            sim_r = await db.execute(
                select(SimCard.id).where(
                    and_(
                        SimCard.gateway_id == gateway_id,
                        SimCard.port_number == port_number,
                    )
                ).limit(1)
            )
            if sim_r.scalars().first() is not None:
                can_add = False
        else:
            gateway_id = None

        out.append(
            DiscoveredChannel(
                host=host,
                goip_id=goip_id,
                port_number=port_number,
                phone_number=phone_number,
                signal=signal,
                gsm_status=gsm_status,
                gateway_id=gateway_id,
                can_add=can_add,
            )
        )
    return out


@router.post(
    "/discovered-sims",
    response_model=SimCardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить SIM по обнаруженному каналу (подтверждение админа)",
)
async def add_discovered_sim(
    body: DiscoveredSimAddRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Создать запись SimCard для канала, обнаруженного по keepalive."""
    gw = await db.get(Gateway, body.gateway_id)
    if not gw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")
    existing = await db.execute(
        select(SimCard).where(
            and_(
                SimCard.gateway_id == body.gateway_id,
                SimCard.port_number == body.port_number,
            )
        )
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"SIM для порта {body.port_number} уже есть у шлюза {body.gateway_id}",
        )
    sim = SimCard(
        gateway_id=body.gateway_id,
        port_number=body.port_number,
        phone_number=body.phone_number,
    )
    db.add(sim)
    await db.commit()
    await db.refresh(sim)
    return sim


@router.get("", response_model=List[GatewayResponse], summary="Список всех шлюзов")
async def list_gateways(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(Gateway).order_by(Gateway.id))
    return result.scalars().all()


@router.post(
    "",
    response_model=GatewayResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить шлюз",
)
async def create_gateway(
    body: GatewayCreateRequest,
    db: AsyncSession = Depends(get_db),
    manager: GatewayManager = Depends(get_gateway_manager),
    _: User = Depends(require_admin),
):
    gw = Gateway(
        name=body.name, type=body.type, host=body.host, port=body.port,
        username=body.username, password=body.password, is_active=True,
    )
    db.add(gw)
    await db.commit()
    await db.refresh(gw)

    manager.add(gw)
    return gw


@router.get("/{gateway_id}", response_model=GatewayResponse, summary="Получить шлюз по ID")
async def get_gateway(
    gateway_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    gw = await db.get(Gateway, gateway_id)
    if not gw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")
    return gw


@router.patch("/{gateway_id}", response_model=GatewayResponse, summary="Обновить шлюз")
async def update_gateway(
    gateway_id: int,
    body: GatewayUpdateRequest,
    db: AsyncSession = Depends(get_db),
    manager: GatewayManager = Depends(get_gateway_manager),
    _: User = Depends(require_admin),
):
    gw = await db.get(Gateway, gateway_id)
    if not gw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(gw, field, value)

    await db.commit()
    await db.refresh(gw)

    # Обновить экземпляр в памяти
    if gw.is_active:
        manager.add(gw)
    else:
        manager.remove(gateway_id)

    return gw


@router.delete(
    "/{gateway_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить шлюз",
)
async def delete_gateway(
    gateway_id: int,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    manager: GatewayManager = Depends(get_gateway_manager),
    _: User = Depends(require_admin),
):
    gw = await db.get(Gateway, gateway_id)
    if not gw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")

    sims_query = await db.execute(select(SimCard).where(SimCard.gateway_id == gateway_id))
    sims = sims_query.scalars().all()
    sims_count = len(sims)
    
    if sims_count > 0:
        if not force:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"В шлюзе есть привязанные SIM-карты ({sims_count} шт.). Удалите их сначала или используйте форсированное удаление."
            )
        else:
            for sim in sims:
                await db.delete(sim)

    await db.delete(gw)
    await db.commit()
    manager.remove(gateway_id)


@router.post(
    "/{gateway_id}/test",
    response_model=GatewayTestResult,
    summary="Тест соединения со шлюзом",
)
async def test_gateway(
    gateway_id: int,
    db: AsyncSession = Depends(get_db),
    manager: GatewayManager = Depends(get_gateway_manager),
    _: User = Depends(require_admin),
):
    gw_db = await db.get(Gateway, gateway_id)
    if not gw_db:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")

    # Если нет в памяти — добавляем на время теста
    gw_instance = manager.get(gateway_id)
    if not gw_instance:
        manager.add(gw_db)
        gw_instance = manager.get(gateway_id)

    response = await gw_instance.get_status()

    # Обновляем статус в БД
    status_str = "online" if response.success else f"offline: {response.message}"
    gw_db.last_status = (status_str[:50]) if status_str else None
    if response.success:
        gw_db.last_seen = datetime.now(timezone.utc)
    await db.commit()

    return GatewayTestResult(
        gateway_id=gateway_id,
        online=response.success,
        detail=response.message,
        latency_ms=None,
    )


@router.get(
    "/{gateway_id}/sims",
    response_model=List[SimCardResponse],
    summary="Список SIM-карт шлюза",
)
async def list_sims(
    gateway_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(SimCard).where(SimCard.gateway_id == gateway_id).order_by(SimCard.port_number)
    )
    return result.scalars().all()


@router.post(
    "/{gateway_id}/sims",
    response_model=SimCardResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить SIM-карту (порт) в шлюз",
)
async def add_sim(
    gateway_id: int,
    body: SimCardCreateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    gw = await db.get(Gateway, gateway_id)
    if not gw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шлюз не найден")

    sim = SimCard(
        gateway_id=gateway_id,
        port_number=body.port_number,
        phone_number=body.phone_number,
    )
    db.add(sim)
    await db.commit()
    await db.refresh(sim)
    return sim
