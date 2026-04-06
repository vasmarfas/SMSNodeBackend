"""
Базовые тесты REST API: диалоги, сообщения, контакты, авторизация.
Запуск: из корня SMSNodeBackend: pytest tests/test_messages_api.py -v
С опциональной переменной RUN_API_TESTS=1 (если не задана, тесты пропускаются при отсутствии БД).
"""

import os
import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Импортируем app после возможной установки env (для тестовой БД при необходимости)
from core.api.app import app
from core.api.dependencies import get_current_user
from core.db.models import User, RoleEnum


# Пропускать тесты, если нет БД или не установлен флаг (опционально)
RUN_TESTS = os.getenv("RUN_API_TESTS", "0") == "1"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Пользователь для подмены get_current_user."""
    u = User(
        id=1,
        username="testuser",
        hashed_password="",
        role=RoleEnum.USER,
        telegram_id=None,
        is_active=True,
    )
    return u


class TestHealthEndpoints:
    """Эндпоинты / и /health."""

    def test_root_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
        assert "version" in data
        assert "gateways_loaded" in data

    def test_health_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"


class TestProtectedEndpointsUnauthorized:
    """Без токена защищённые эндпоинты возвращают 401."""

    @pytest.mark.parametrize("method,url", [
        ("GET", "/api/v1/user/dialogs"),
        ("GET", "/api/v1/user/contacts"),
        ("GET", "/api/v1/user/messages/recent"),
        ("POST", "/api/v1/user/messages/send"),
        ("GET", "/api/v1/admin/messages"),
    ])
    def test_no_token_returns_401(self, client, method, url):
        if method == "GET":
            response = client.get(url)
        else:
            response = client.post(url, json={"phone": "+79001234567", "text": "test"})
        assert response.status_code == 401

    def test_invalid_token_returns_401(self, client):
        response = client.get(
            "/api/v1/user/contacts",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


@pytest.mark.skipif(not RUN_TESTS, reason="RUN_API_TESTS=1 and DB required")
class TestUserDialogsAndContactsWithAuth:
    """С валидным пользователем (override): пустые списки и создание контакта."""

    def test_user_dialogs_empty(self, client, mock_user):
        app.dependency_overrides[get_current_user] = lambda: mock_user
        try:
            response = client.get("/api/v1/user/dialogs")
            assert response.status_code == 200
            assert response.json() == []
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_user_contacts_empty_then_create(self, client, mock_user):
        app.dependency_overrides[get_current_user] = lambda: mock_user
        try:
            response = client.get("/api/v1/user/contacts")
            assert response.status_code == 200
            assert response.json() == []

            create = client.post(
                "/api/v1/user/contacts",
                json={"name": "Test Contact", "phone_number": "+79001234567"},
            )
            assert create.status_code in (201, 409)
            if create.status_code == 201:
                data = create.json()
                assert data["name"] == "Test Contact"
                assert data["phone_number"] == "+79001234567"
                list_resp = client.get("/api/v1/user/contacts")
                assert list_resp.status_code == 200
                assert len(list_resp.json()) >= 1
        finally:
            app.dependency_overrides.pop(get_current_user, None)


class TestSendMessageSmoke:
    """Smoke-тест отправки SMS с замоканной очередью."""

    @patch("sms_queue.enqueue_sms", new_callable=AsyncMock)
    def test_send_message_returns_202_when_sim_assigned(self, mock_enqueue, client, mock_user):
        mock_enqueue.return_value = "fake-job-id"
        app.dependency_overrides[get_current_user] = lambda: mock_user
        try:
            response = client.post(
                "/api/v1/user/messages/send",
                json={"phone": "+79001234567", "text": "Hello"},
            )
            # 202 если SIM есть у пользователя, 403 если нет
            assert response.status_code in (202, 403)
            if response.status_code == 202:
                data = response.json()
                assert data.get("status") == "queued"
                assert data.get("job_id") == "fake-job-id"
        finally:
            app.dependency_overrides.pop(get_current_user, None)
