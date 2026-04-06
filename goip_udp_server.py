#!/usr/bin/env python3

import socket
import threading
import json
from datetime import datetime, timezone
import logging
import asyncio
from typing import List, Dict, Any

from bot import bot
from config_reader import config


class GoIPSMSServerReceiver:
    """Сервер для приема SMS от GOIP через UDP"""

    def __init__(self, start_port: int = 9991, num_ports: int = 8):
        self.start_port = start_port
        self.num_ports = num_ports
        self.sockets = []
        self.running = False
        self.sms_log: List[Dict[str, Any]] = []
        self.loop = None

    def start(self):
        """Запустить серверы для всех портов GOIP"""
        self.running = True

        for i in range(self.num_ports):
            port = self.start_port + i
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(('0.0.0.0', port))
                self.sockets.append(sock)

                # Запускаем отдельный поток для каждого порта
                thread = threading.Thread(target=self._listen_port, args=(sock, port), daemon=True)
                thread.start()

                print(f"✅ UDP сервер запущен на порту {port} (канал {i+1})")

            except Exception as e:
                print(f"❌ Не удалось запустить сервер на порту {port}: {e}")

        if self.sockets:
            print(f"🎯 Всего запущено {len(self.sockets)} UDP серверов")

    def stop(self):
        """Остановить все серверы"""
        self.running = False
        for sock in self.sockets:
            try:
                sock.close()
            except:
                pass
        self.sockets.clear()
        print(f"❌ Остановлено {len(self.sockets)} UDP серверов")

    def _listen_port(self, sock, port):
        """Обработка пакетов для конкретного порта"""
        channel = port - 9990  # 9991 = channel 1, etc.

        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
                message = data.decode('utf-8').strip()

                print(f"[Порт {port}] Получено: {message[:80]}{'...' if len(message) > 80 else ''}")

                if message.startswith('RECEIVE:'):
                    self._handle_sms(message, addr, channel, sock)
                elif message.startswith('req:'):
                    self._handle_keepalive(message, addr, channel, sock)

            except Exception as e:
                if self.running:
                    print(f"Ошибка UDP сервера на порту {port}: {e}")

    def _handle_sms(self, message: str, addr, channel: int, sock):
        """Обработать входящее SMS"""
        # Парсинг
        parts = {}
        for part in message.replace('RECEIVE:', '', 1).split(';'):
            if ':' in part:
                k, v = part.split(':', 1)
                parts[k.strip()] = v.strip()

        recvid = parts.get('recvid')
        sender = parts.get('srcnum')
        text = parts.get('msg')

        # Логирование
        sms_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'from': sender,
            'message': text
        }
        self.sms_log.append(sms_data)

        print(f"📨 UDP SMS: {sender} -> {text}")

        # Отправить ACK
        ack = f"RECEIVE {recvid} OK\n"
        sock.sendto(ack.encode('utf-8'), addr)

        # Отправить уведомление в Telegram
        self._send_telegram_notification(sender, text)

    def _handle_keepalive(self, message: str, addr, channel: int, sock):
        """Обработать keep-alive с расширенными данными"""
        parts = {}
        for part in message.split(';'):
            if ':' in part:
                k, v = part.split(':', 1)
                parts[k.strip()] = v.strip()

        count = parts.get('count')
        device_id = parts.get('id')
        signal = parts.get('signal')
        gsm_status = parts.get('gsm_status')
        voip_status = parts.get('voip_status')
        sim_num = parts.get('num')
        imei = parts.get('imei')
        imsi = parts.get('imsi')
        iccid = parts.get('iccid')
        cellinfo = parts.get('CELLINFO')

        # Отправить ACK
        ack = f"reg:{count};status:0;\n"
        sock.sendto(ack.encode('utf-8'), addr)

        print(f"📡 Keep-Alive | Канал:{channel} | ID:{device_id} | SIM:{sim_num} | Сигнал:{signal}/31 | GSM:{gsm_status} | VoIP:{voip_status}")

        # Логируем детальную информацию
        if imei:
            print(f"   └─ IMEI:{imei} | IMSI:{imsi} | Cell:{cellinfo or 'N/A'}")

    def _send_telegram_notification(self, sender: str, text: str):
        """Отправить уведомление в Telegram"""
        try:
            message = (f"📨 UDP SMS\n\n"
                      f"{text}\n\n"
                      f"Отправитель: {sender}\n"
                      f"Время: {datetime.now(timezone.utc).strftime('%d-%m-%Y %H:%M:%S')}")

            # Отправить админу
            asyncio.run_coroutine_threadsafe(
                bot.send_message(config.ADMIN_ID, message),
                self.loop
            )

        except Exception as e:
            logging.error(f"Ошибка отправки уведомления: {e}")

    def export_log(self, filename: str = 'udp_sms_log.json'):
        """Экспортировать логи SMS"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.sms_log, f, ensure_ascii=False, indent=2)
        print(f"✅ UDP логи сохранены в {filename}")


# Тестовый запуск
if __name__ == '__main__':
    print("🚀 Запуск UDP серверов для всех каналов GOIP-8")
    server = GoIPSMSServerReceiver(start_port=9991, num_ports=8)
    server.start()

    try:
        print("\n📡 Прослушивание UDP трафика... (нажмите Ctrl+C для выхода)")
        print("Ожидаются keep-alive пакеты от GOIP устройства")
        print("IP GOIP: 172.16.30.1")
        print("Порты: 9991-9998 (каналы 1-8)")
        print()

        while True:
            import time
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 Остановка UDP серверов...")
        server.export_log()
        server.stop()
