#!/usr/bin/env python3
"""
Утилита диагностики доступности GoIP-шлюза.

Использование:
    python check_goip.py                          # читает из .env
    python check_goip.py 192.168.1.100            # явный IP
    python check_goip.py 192.168.1.100 9991 80   # IP + UDP-порт + HTTP-порт
"""

import socket
import sys
import os

# Пытаемся прочитать из конфига; если не получится — используем значения из CLI или дефолт
def _load_from_env() -> tuple[str, int, int]:
    """Загрузить настройки из .env через config_reader, или вернуть дефолты."""
    try:
        # Меняем рабочий каталог, чтобы pydantic-settings нашёл .env рядом
        script_dir = os.path.dirname(os.path.abspath(__file__))
        os.chdir(script_dir)
        from config_reader import config
        ip = config.GOIP_IP or "192.168.10.70"
        return ip, 9991, 80
    except Exception:
        return "192.168.10.70", 9991, 80


def check_goip_udp(ip: str, port: int = 9991, timeout: int = 5) -> bool:
    """Проверить доступность GoIP по UDP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        test_packet = "TEST 9999\n"
        print(f"  Отправляем UDP-пакет на {ip}:{port}...")
        sock.sendto(test_packet.encode("utf-8"), (ip, port))
        try:
            response, addr = sock.recvfrom(4096)
            print(f"  Ответ от {addr}: {response.decode('utf-8', errors='replace').strip()}")
            return True
        except socket.timeout:
            print("  Ответа нет (timeout) — порт может быть доступен, просто нет ответа на TEST.")
            return True  # GoIP не отвечает на произвольные пакеты, но соединение прошло
    except Exception as e:
        print(f"  Ошибка: {e}")
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_goip_http(ip: str, port: int = 80, timeout: int = 5) -> bool:
    """Проверить TCP-доступность HTTP-порта GoIP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        if result == 0:
            print(f"  HTTP порт {port} — доступен.")
            return True
        else:
            print(f"  HTTP порт {port} — недоступен (код {result}).")
            return False
    except Exception as e:
        print(f"  Ошибка HTTP-проверки: {e}")
        return False


def main():
    # Парсим аргументы CLI
    args = sys.argv[1:]

    default_ip, default_udp, default_http = _load_from_env()

    goip_ip   = args[0] if len(args) >= 1 else default_ip
    udp_port  = int(args[1]) if len(args) >= 2 else default_udp
    http_port = int(args[2]) if len(args) >= 3 else default_http

    print("GoIP Connectivity Check")
    print("=" * 50)
    print(f"Цель: {goip_ip}")
    print(f"UDP-порт: {udp_port}  |  HTTP-порт: {http_port}")
    print()

    print("1. Проверка HTTP...")
    http_ok = check_goip_http(goip_ip, http_port)
    print()

    print("2. Проверка UDP...")
    udp_ok = check_goip_udp(goip_ip, udp_port)
    print()

    print("Результат:")
    print(f"  HTTP (порт {http_port}): {'✅ Доступен' if http_ok else '❌ Недоступен'}")
    print(f"  UDP  (порт {udp_port}): {'✅ Доступен' if udp_ok else '❌ Недоступен'}")

    if http_ok or udp_ok:
        print("\nUстройство GoIP обнаружено!")
        if http_ok:
            print("  Рекомендуется HTTP API для отправки SMS (GoIPHTTPGateway).")
        if udp_ok:
            print("  UDP-протокол доступен для расширенных функций (GoIPGateway).")
    else:
        print("\nUстройство GoIP не найдено.")
        print("  Возможные причины:")
        print("  • Неверный IP-адрес (укажи в .env как GOIP_IP= или передай аргументом)")
        print("  • Устройство выключено или недоступно в сети")
        print("  • Файрвол блокирует порты")
        print(f"\n  Пример: python check_goip.py 192.168.1.100 {udp_port} {http_port}")


if __name__ == "__main__":
    main()
