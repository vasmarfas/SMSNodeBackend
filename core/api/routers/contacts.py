"""
Роутер контактов пользователя — /api/v1/user/contacts.
CRUD подписей для внешних номеров (текущий пользователь по JWT).
"""

from typing import List, Optional

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.api.dependencies import get_current_user
from core.db.database import get_db
from core.db.models import Contact, ContactGroup, User

router = APIRouter(prefix="/api/v1", tags=["Contacts"])

PHONE_PATTERN = r"^\+?[1-9]\d{10,14}$"


class ContactResponse(BaseModel):
    id: int
    name: str
    phone_number: str

    class Config:
        from_attributes = True


class ContactCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    phone_number: str = Field(..., min_length=10, max_length=20, pattern=PHONE_PATTERN)


class ContactUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    phone_number: Optional[str] = Field(None, min_length=10, max_length=20, pattern=PHONE_PATTERN)

class ContactImportResponse(BaseModel):
    created: int
    updated: int
    skipped: int


class ContactGroupResponse(BaseModel):
    id: int
    name: str
    contact_ids: List[int] = []
    contacts_count: int = 0

    class Config:
        from_attributes = True


class ContactGroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ContactGroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)


class ContactGroupMembersRequest(BaseModel):
    contact_ids: List[int] = Field(default_factory=list)


@router.get(
    "/user/contacts",
    response_model=List[ContactResponse],
    summary="[User] Список контактов",
)
async def list_contacts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(Contact).where(Contact.user_id == current_user.id).order_by(Contact.name)
    )
    return r.scalars().all()


@router.post(
    "/user/contacts",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[User] Создать контакт",
)
async def create_contact(
    body: ContactCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = await db.execute(
        select(Contact).where(
            Contact.user_id == current_user.id,
            Contact.phone_number == body.phone_number,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Контакт с таким номером уже существует",
        )
    contact = Contact(
        user_id=current_user.id,
        name=body.name,
        phone_number=body.phone_number,
    )
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return contact


@router.get(
    "/user/contacts/{contact_id}",
    response_model=ContactResponse,
    summary="[User] Получить контакт по ID",
)
async def get_contact(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контакт не найден")
    return contact


@router.patch(
    "/user/contacts/{contact_id}",
    response_model=ContactResponse,
    summary="[User] Обновить контакт",
)
async def update_contact(
    contact_id: int,
    body: ContactUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контакт не найден")
    if body.name is not None:
        contact.name = body.name
    if body.phone_number is not None:
        contact.phone_number = body.phone_number
    await db.commit()
    await db.refresh(contact)
    return contact


@router.delete(
    "/user/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[User] Удалить контакт",
)
async def delete_contact(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контакт не найден")
    await db.delete(contact)
    await db.commit()


@router.get(
    "/user/contacts/export",
    summary="[User] Экспорт контактов (CSV)",
)
async def export_contacts_csv(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(Contact).where(Contact.user_id == current_user.id).order_by(Contact.name)
    )
    contacts = r.scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "phone_number"])
    for c in contacts:
        w.writerow([c.name, c.phone_number])

    data = out.getvalue().encode("utf-8-sig")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=contacts.csv"},
    )


@router.post(
    "/user/contacts/import",
    response_model=ContactImportResponse,
    summary="[User] Импорт контактов (CSV)",
)
async def import_contacts_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ожидается CSV-файл")

    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")

    reader = csv.DictReader(io.StringIO(text))
    created = updated = skipped = 0

    for row in reader:
        name = (row.get("name") or "").strip()
        phone = (row.get("phone_number") or row.get("phone") or row.get("number") or "").strip()
        if not name or not phone:
            skipped += 1
            continue

        existing = await db.execute(
            select(Contact).where(
                Contact.user_id == current_user.id,
                Contact.phone_number == phone,
            )
        )
        contact = existing.scalar_one_or_none()
        if contact:
            if contact.name != name:
                contact.name = name
                updated += 1
            else:
                skipped += 1
        else:
            db.add(Contact(user_id=current_user.id, name=name, phone_number=phone))
            created += 1

    await db.commit()
    return ContactImportResponse(created=created, updated=updated, skipped=skipped)


@router.get(
    "/user/contact-groups",
    response_model=List[ContactGroupResponse],
    summary="[User] Список групп контактов",
)
async def list_contact_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = await db.execute(
        select(ContactGroup)
        .where(ContactGroup.user_id == current_user.id)
        .options(selectinload(ContactGroup.contacts))
        .order_by(ContactGroup.name)
    )
    groups = r.scalars().all()
    return [
        ContactGroupResponse(
            id=g.id,
            name=g.name,
            contact_ids=[c.id for c in (g.contacts or [])],
            contacts_count=len(g.contacts or []),
        )
        for g in groups
    ]


@router.post(
    "/user/contact-groups",
    response_model=ContactGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="[User] Создать группу контактов",
)
async def create_contact_group(
    body: ContactGroupCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = await db.execute(
        select(ContactGroup).where(
            ContactGroup.user_id == current_user.id,
            func.lower(ContactGroup.name) == body.name.lower(),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Группа с таким именем уже существует")

    g = ContactGroup(user_id=current_user.id, name=body.name)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return ContactGroupResponse(id=g.id, name=g.name, contact_ids=[], contacts_count=0)


@router.patch(
    "/user/contact-groups/{group_id}",
    response_model=ContactGroupResponse,
    summary="[User] Обновить группу контактов",
)
async def update_contact_group(
    group_id: int,
    body: ContactGroupUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    g = await db.get(ContactGroup, group_id, options=[selectinload(ContactGroup.contacts)])
    if not g or g.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Группа не найдена")

    if body.name is not None:
        g.name = body.name

    await db.commit()
    await db.refresh(g)
    return ContactGroupResponse(
        id=g.id,
        name=g.name,
        contact_ids=[c.id for c in (g.contacts or [])],
        contacts_count=len(g.contacts or []),
    )


@router.put(
    "/user/contact-groups/{group_id}/members",
    response_model=ContactGroupResponse,
    summary="[User] Задать состав группы (полная замена)",
)
async def set_contact_group_members(
    group_id: int,
    body: ContactGroupMembersRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    g = await db.get(ContactGroup, group_id, options=[selectinload(ContactGroup.contacts)])
    if not g or g.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Группа не найдена")

    if body.contact_ids:
        r = await db.execute(
            select(Contact).where(
                Contact.user_id == current_user.id,
                Contact.id.in_(body.contact_ids),
            )
        )
        contacts = r.scalars().all()
    else:
        contacts = []

    g.contacts = contacts
    await db.commit()
    await db.refresh(g)
    return ContactGroupResponse(
        id=g.id,
        name=g.name,
        contact_ids=[c.id for c in (g.contacts or [])],
        contacts_count=len(g.contacts or []),
    )


@router.delete(
    "/user/contact-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[User] Удалить группу контактов",
)
async def delete_contact_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    g = await db.get(ContactGroup, group_id)
    if not g or g.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Группа не найдена")
    await db.delete(g)
    await db.commit()
