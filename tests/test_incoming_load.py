import asyncio
import random
import socket
import string
import time

from config_reader import config


def _rand_text(n: int) -> str:
    alphabet = string.ascii_letters + string.digits + " "
    return "".join(random.choice(alphabet) for _ in range(n)).strip() or "test"


async def _send_and_wait_ack(sock: socket.socket, addr: tuple[str, int], payload: str, timeout_s: float = 2.0) -> str:
    sock.sendto(payload.encode("utf-8"), addr)
    sock.settimeout(timeout_s)
    data, _ = sock.recvfrom(2048)
    return data.decode("utf-8", errors="replace").strip()


async def main(
    host: str = "127.0.0.1",
    port: int | None = None,
    messages_per_minute: int = 50,
    duration_sec: int = 60,
    goip_id: str = "TEST_GOIP",
    goip_password: str = "admin",
):
    """
    Нагрузочная проверка входящих SMS по GoIP UDP:
    - Генерирует RECEIVE-пакеты с частотой 40–50 в минуту на канал.
    - Проверяет, что сервер отвечает ACK на каждый пакет.

    Требования:
    - Backend запущен и слушает UDP (GOIP_LISTEN_HOST/GOIP_LISTEN_PORT).
    """
    if port is None:
        port = int(getattr(config, "GOIP_LISTEN_PORT", 9999))

    addr = (host, port)
    interval = duration_sec / max(1, int(messages_per_minute))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))

    ok = 0
    failed = 0
    started = time.monotonic()

    for i in range(int(messages_per_minute)):
        recvid = str(10_000_000 + i)
        srcnum = f"+79{random.randint(0, 999999999):09d}"
        msg = _rand_text(random.randint(10, 80))
        payload = f"RECEIVE:{recvid};id:{goip_id};pass:{goip_password};srcnum:{srcnum};msg:{msg}"
        try:
            ack = await _send_and_wait_ack(sock, addr, payload, timeout_s=2.5)
            if ack.startswith(f"RECEIVE {recvid} OK"):
                ok += 1
            else:
                failed += 1
        except Exception:
            failed += 1

        await asyncio.sleep(interval)

    elapsed = time.monotonic() - started
    sock.close()

    print(
        "Incoming UDP load test finished\n"
        f"Target: {addr[0]}:{addr[1]}\n"
        f"Sent: {messages_per_minute} in ~{duration_sec}s (interval ~{interval:.2f}s)\n"
        f"ACK OK: {ok}\n"
        f"ACK failed: {failed}\n"
        f"Elapsed: {elapsed:.2f}s\n"
    )

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

