# SMSNodeBackend

**Русский:** [`README.md`](README.md)

## Purpose

Asynchronous backend for SMS routing and GSM gateway integration: REST API (FastAPI), Telegram bot, in-process SMS queue, GOIP/Skyline UDP and HTTP adapters, PostgreSQL persistence.

## Data on deploy

**No demo or seed data is created automatically** when you run the container or `main.py`. Only the database schema is ensured via `init_db` / `create_tables`. Gateways, SIM cards, users, and messages must be added via API or bot.

### Demo Mode (Read-Only)

If `IS_DEMO=True` is set in `.env` (or environment), the backend will upon startup:
1. Automatically create English/Russian demo data.
2. Create test accounts: `demo` (password: `demo`, user role) and `admin` (password: `admin`, admin role).
3. Create mock gateways (UDP and HTTP) and generate messages.
4. **Enable soft Read-Only mode**: all mutation operations via API and Telegram bot will be processed normally and return valid successful responses to the client, but **without actually saving changes to the database** (transactions are rolled back at the end of the request). This allows full UI testing without accumulating junk data in the demo database.

The **`seed_fake_data.py`** script is invoked under demo mode to seed data, and can also be run manually:

```bash
python seed_fake_data.py
```

## Components

| Area | Location |
|------|----------|
| Entrypoint | `main.py` |
| HTTP API | `core/api/` |
| ORM models | `core/db/models.py` |
| Gateways | `core/gateways/`, `gateway_service.py` |
| UDP / protocol | `goip_sms_receiver.py`, `goip_udp_client.py`, `goip_runtime_registry.py` |
| Queue | `sms_queue.py` |
| Bot handlers | `message_handlers/` |
| Config | `config_reader.py`, `.env` |

## Quick Start

### 1. Environment Setup
```bash
cp .env.example .env
```
Set at least `BOT_TOKEN`, `ADMIN_ID`, `API_SECRET_KEY`, and PostgreSQL variables in the `.env` file.

### 2. Run via Docker (Recommended)
You can build the image locally or use a pre-built image from the GitHub Container Registry (GHCR).

**Option A: Build from source (default)**
```bash
docker compose up -d --build
```

**Option B: Use pre-built GHCR image**
If you want to skip the local build process, uncomment the `DOCKER_IMAGE` line in `.env` and point it to your registry:
```env
DOCKER_IMAGE=ghcr.io/vasmarfas/smsnodebackend:latest
```
Then run:
```bash
docker compose up -d
```

### 3. Local Run (without Docker)
Ensure you have a running PostgreSQL server (configured via `POSTGRES_*` in `.env`).

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

API defaults to `http://localhost:8000` (or `API_PORT` from `.env`). Health check: `GET /health`.

## Documentation

| Topic | File |
|-------|------|
| Environment template | `.env.example` |
| GOIP UDP protocol map | `GOIP_UDP_COMMAND_MAP.md` |

## Production notes

- Restrict CORS origins instead of `*` when exposing the API.
- Use a strong `API_SECRET_KEY`.
- Bcrypt truncates passwords longer than 72 bytes; align client policies.

## Tests

```bash
set RUN_API_TESTS=1
pytest tests/
```

Some tests are skipped unless `RUN_API_TESTS` is set.
