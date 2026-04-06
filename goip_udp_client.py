#!/usr/bin/env python3

import socket
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SMSResult:
    success: bool
    message: str
    data: Optional[str] = None


@dataclass
class UDPCommandResult:
    success: bool
    command: str
    sendid: int
    response: Optional[str]
    error: Optional[str] = None
    payload: Optional[str] = None


class GoIPUDPClient:
    """
    Полный UDP-клиент GoIP по спецификации goip_sms_Interface_en.

    Поддерживает:
    - SMS flow: MSG/PASSWORD/SEND/DONE с WAIT-ретраями
    - Status/control команды
    - USSD/IMEI/module/cells API
    """

    def __init__(self, goip_ip: str = "172.16.30.1", port: int = 9991, timeout: int = 10):
        self.goip_ip = goip_ip
        self.port = port
        self.timeout = timeout
        self.sendid = 0  # первый _next_sendid() вернёт 1 (по спецификации GoIP — integer, increase by 1)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(timeout)

    def _next_sendid(self) -> int:
        self.sendid += 1
        return self.sendid

    def _send_packet(self, packet: str, target_port: Optional[int] = None) -> None:
        if not packet.endswith("\n"):
            packet += "\n"
        port = target_port or self.port
        print(f"UDP -> {self.goip_ip}:{port}: {packet.strip()}")
        self.socket.sendto(packet.encode("utf-8"), (self.goip_ip, port))

    def _recv_packet(self, target_port: Optional[int] = None) -> Optional[str]:
        try:
            response, addr = self.socket.recvfrom(4096)
            decoded = response.decode("utf-8", errors="replace").strip()
            print(f"UDP <- {addr}: {decoded}")
            return decoded
        except socket.timeout:
            port = target_port or self.port
            print(f"UDP Timeout: {self.goip_ip}:{port} (нет ответа от GoIP)")
            return None
        except ConnectionResetError:
            port = target_port or self.port
            print(
                f"UDP Connection reset by {self.goip_ip}:{port} "
                f"(порт недоступен для SMS API или указан неверно)"
            )
            return None
        except Exception as e:
            print(f"UDP Error: {e}")
            return None

    def _drain_socket(self, max_packets: int = 20) -> None:
        """
        Очистить хвост старых UDP-ответов перед новой сессией отправки.
        Это устраняет проблемы со stale-пакетами (DONE/DELIVER от старого sendid).
        """
        old_timeout = self.socket.gettimeout()
        try:
            self.socket.settimeout(0.01)
            for _ in range(max_packets):
                try:
                    response, addr = self.socket.recvfrom(4096)
                    decoded = response.decode("utf-8", errors="replace").strip()
                    print(f"UDP (drain) <- {addr}: {decoded}")
                except socket.timeout:
                    break
        finally:
            self.socket.settimeout(old_timeout)

    def _send_recv(self, packet: str, target_port: Optional[int] = None) -> Optional[str]:
        self._send_packet(packet, target_port=target_port)
        return self._recv_packet(target_port=target_port)

    @staticmethod
    def _split(resp: Optional[str]) -> List[str]:
        if not resp:
            return []
        return resp.strip().split()

    @staticmethod
    def _is_error(resp: Optional[str]) -> bool:
        return bool(resp and resp.startswith("ERROR"))

    @staticmethod
    def _extract_error(resp: Optional[str]) -> str:
        if not resp:
            return "timeout/no response"
        if resp.startswith("ERROR"):
            parts = resp.split(maxsplit=2)
            return parts[2] if len(parts) > 2 else resp
        return resp

    def _expect_prefix_and_sendid(
        self, resp: Optional[str], prefix: str, sendid: int, sendid_pos: int = 1
    ) -> UDPCommandResult:
        if not resp:
            return UDPCommandResult(False, prefix, sendid, resp, error="timeout/no response")
        if self._is_error(resp):
            return UDPCommandResult(False, prefix, sendid, resp, error=self._extract_error(resp))

        parts = self._split(resp)
        if len(parts) <= sendid_pos:
            return UDPCommandResult(False, prefix, sendid, resp, error="malformed response")
        if parts[0] != prefix:
            return UDPCommandResult(False, prefix, sendid, resp, error=f"unexpected prefix: {parts[0]}")
        if parts[sendid_pos] != str(sendid):
            return UDPCommandResult(
                False, prefix, sendid, resp, error=f"sendid mismatch: got {parts[sendid_pos]}"
            )

        payload = " ".join(parts[sendid_pos + 1 :]) if len(parts) > sendid_pos + 1 else ""
        return UDPCommandResult(True, prefix, sendid, resp, payload=payload)


    def send_sms(
        self,
        password: str,
        phone: str,
        message: str,
        target_port: Optional[int] = None,
        telid: int = 1,
        wait_retries: int = 6,
        wait_delay_sec: float = 2.0,
    ) -> SMSResult:
        """
        Полный flow отправки SMS:
          MSG -> PASSWORD -> SEND (OK/WAIT/ERROR) -> DONE.
        """
        sendid = self._next_sendid()
        msg_len = len(message.encode("utf-8"))
        self._drain_socket()

        self._send_packet(f"MSG {sendid} {msg_len} {message}", target_port=target_port)
        r = self._wait_flow_response(sendid=sendid, target_port=target_port, accepted={"PASSWORD", "ERROR"})
        if not r:
            return SMSResult(False, "MSG timeout", None)
        if r.startswith("ERROR"):
            return SMSResult(False, f"MSG error: {self._extract_error(r)}", r)
        msg_init = self._expect_prefix_and_sendid(r, "PASSWORD", sendid, sendid_pos=1)
        if not msg_init.success:
            return SMSResult(False, f"MSG init invalid: {msg_init.error}", r)

        self._send_packet(f"PASSWORD {sendid} {password}", target_port=target_port)
        r = self._wait_flow_response(sendid=sendid, target_port=target_port, accepted={"SEND", "ERROR"})
        if not r:
            return SMSResult(False, "PASSWORD timeout", None)
        if r.startswith("ERROR"):
            return SMSResult(False, f"PASSWORD error: {self._extract_error(r)}", r)
        auth = self._expect_prefix_and_sendid(r, "SEND", sendid, sendid_pos=1)
        if not auth.success:
            return SMSResult(False, f"PASSWORD auth invalid: {auth.error}", r)

        current = 0
        final_resp = None
        while current <= wait_retries:
            self._send_packet(f"SEND {sendid} {telid} {phone}", target_port=target_port)
            r = self._wait_flow_response(sendid=sendid, target_port=target_port, accepted={"OK", "WAIT", "ERROR"})
            final_resp = r
            if not r:
                return SMSResult(False, "SEND timeout", final_resp)

            parts = self._split(r)
            if len(parts) >= 3 and parts[0] == "OK" and parts[1] == str(sendid) and parts[2] == str(telid):
                break
            if len(parts) >= 3 and parts[0] == "WAIT" and parts[1] == str(sendid) and parts[2] == str(telid):
                current += 1
                if current > wait_retries:
                    return SMSResult(False, "SEND WAIT exhausted", final_resp)
                time.sleep(wait_delay_sec)
                continue
            if parts and parts[0] == "ERROR":
                return SMSResult(False, f"SEND error: {self._extract_error(r)}", final_resp)

            return SMSResult(False, f"SEND invalid response: {r}", final_resp)

        self._send_packet(f"DONE {sendid}", target_port=target_port)
        done = self._wait_flow_response(sendid=sendid, target_port=target_port, accepted={"DONE", "OK", "ERROR"})
        if not done:
            return SMSResult(False, "DONE timeout", None)
        if done.startswith("ERROR"):
            return SMSResult(False, f"DONE error: {self._extract_error(done)}", done)
        done_parts = self._split(done)
        if len(done_parts) >= 2 and done_parts[0] in {"DONE", "OK"} and done_parts[1] == str(sendid):
            return SMSResult(True, "SMS отправлена", done)
        return SMSResult(False, f"DONE invalid: {done}", done)

    def _wait_flow_response(
        self,
        sendid: int,
        target_port: Optional[int],
        accepted: set[str],
        max_reads: int = 20,
    ) -> Optional[str]:
        """
        Ждёт ответ для текущего sendid, игнорируя хвосты старых сессий и async DELIVER.
        """
        for _ in range(max_reads):
            resp = self._recv_packet(target_port=target_port)
            if not resp:
                return None

            if resp.startswith("DELIVER:"):
                continue

            parts = self._split(resp)
            if not parts:
                continue
            if len(parts) < 2:
                continue
            if parts[1] != str(sendid):
                continue
            if parts[0] in accepted:
                return resp
        return None

    def send_sms_bulk(
        self,
        password: str,
        recipients: List[str],
        message: str,
        target_port: Optional[int] = None,
        start_telid: int = 1,
    ) -> Dict[str, List[str]]:
        results = {"success": [], "failed": []}
        telid = start_telid

        for phone in recipients:
            result = self.send_sms(
                password=password,
                phone=phone,
                message=message,
                target_port=target_port,
                telid=telid,
            )
            if result.success:
                results["success"].append(phone)
            else:
                results["failed"].append(phone)
            telid += 1
            time.sleep(0.2)
        return results


    def _simple_get(self, cmd: str, password: str, target_port: Optional[int] = None) -> Optional[str]:
        sendid = self._next_sendid()
        resp = self._send_recv(f"{cmd} {sendid} {password}", target_port=target_port)
        check = self._expect_prefix_and_sendid(resp, cmd, sendid, sendid_pos=1)
        return check.payload if check.success else None

    def _simple_set(
        self, cmd_packet: str, expected_prefix: str, sendid: int, target_port: Optional[int] = None
    ) -> bool:
        resp = self._send_recv(cmd_packet, target_port=target_port)
        check = self._expect_prefix_and_sendid(resp, expected_prefix, sendid, sendid_pos=1)
        return check.success and "ok" in (check.payload or "").lower()


    def get_gsm_number(self, password: str, target_port: Optional[int] = None) -> Optional[str]:
        return self._simple_get("get_gsm_num", password, target_port=target_port)

    def set_gsm_number(self, password: str, number: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"set_gsm_num {sendid} {number} {password}",
            "set_gsm_num",
            sendid,
            target_port=target_port,
        )

    def get_gsm_state(self, password: str, target_port: Optional[int] = None) -> Optional[str]:
        return self._simple_get("get_gsm_state", password, target_port=target_port)

    def get_imei(self, password: str, target_port: Optional[int] = None) -> Optional[str]:
        return self._simple_get("get_imei", password, target_port=target_port)

    def set_imei(self, password: str, imei: str, target_port: Optional[int] = None) -> bool:
        if len(imei) != 15 or not imei.isdigit():
            return False
        sendid = self._next_sendid()
        return self._simple_set(
            f"set_imei {sendid} {imei} {password}",
            "set_imei",
            sendid,
            target_port=target_port,
        )

    def get_remain_time(self, password: str, target_port: Optional[int] = None) -> Optional[int]:
        payload = self._simple_get("get_remain_time", password, target_port=target_port)
        try:
            return int(payload) if payload is not None else None
        except (TypeError, ValueError):
            return None

    def get_exp_time(self, password: str, target_port: Optional[int] = None) -> Optional[int]:
        payload = self._simple_get("get_exp_time", password, target_port=target_port)
        try:
            return int(payload) if payload is not None else None
        except (TypeError, ValueError):
            return None

    def set_exp_time(self, password: str, exp_minutes: int, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"set_exp_time {sendid} {exp_minutes} {password}",
            "set_exp_time",
            sendid,
            target_port=target_port,
        )

    def reset_remain_time(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"reset_remain_time {sendid} {password}",
            "reset_remain_time",
            sendid,
            target_port=target_port,
        )


    def send_ussd(self, password: str, ussd_code: str, target_port: Optional[int] = None) -> Optional[str]:
        sendid = self._next_sendid()
        response = self._send_recv(f"USSD {sendid} {password} {ussd_code}", target_port=target_port)
        if not response:
            return None
        if response.startswith("USSDERROR"):
            return None
        check = self._expect_prefix_and_sendid(response, "USSD", sendid, sendid_pos=1)
        return check.payload if check.success else None

    def exit_ussd(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        response = self._send_recv(f"USSDEXIT {sendid} {password}", target_port=target_port)
        check = self._expect_prefix_and_sendid(response, "USSDEXIT", sendid, sendid_pos=1)
        return check.success


    def drop_call(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"svr_drop_call {sendid} {password}",
            "svr_drop_call",
            sendid,
            target_port=target_port,
        )

    def reboot_module(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"svr_reboot_module {sendid} {password}",
            "svr_reboot_module",
            sendid,
            target_port=target_port,
        )

    def reboot_device(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"svr_reboot_dev {sendid} {password}",
            "svr_reboot_dev",
            sendid,
            target_port=target_port,
        )

    def set_call_forward(
        self,
        password: str,
        reason: int = 0,
        mode: int = 3,
        number: str = "",
        timeout: int = 0,
        target_port: Optional[int] = None,
    ) -> bool:
        sendid = self._next_sendid()
        response = self._send_recv(
            f"CF {sendid} {password} {reason} {mode} {number} {timeout}",
            target_port=target_port,
        )
        if not response:
            return False
        parts = self._split(response)
        return len(parts) >= 2 and parts[0] == "CFOK" and parts[1] == str(sendid)


    def get_out_call_interval(self, password: str, target_port: Optional[int] = None) -> Optional[int]:
        payload = self._simple_get("get_out_call_interval", password, target_port=target_port)
        try:
            return int(payload) if payload is not None else None
        except (TypeError, ValueError):
            return None

    def set_out_call_interval(self, password: str, interval_sec: int, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"set_out_call_interval {sendid} {interval_sec} {password}",
            "set_out_call_interval",
            sendid,
            target_port=target_port,
        )


    def module_ctl_i(self, password: str, value: int, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"module_ctl_i {sendid} {value} {password}",
            "module_ctl_i",
            sendid,
            target_port=target_port,
        )

    def module_ctl(self, password: str, value: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"module_ctl {sendid} {value} {password}",
            "module_ctl",
            sendid,
            target_port=target_port,
        )


    def set_base_cell(self, password: str, cell_id: int, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"set_base_cell {sendid} {cell_id} {password}",
            "set_base_cell",
            sendid,
            target_port=target_port,
        )

    def get_cells_list(self, password: str, target_port: Optional[int] = None) -> bool:
        sendid = self._next_sendid()
        return self._simple_set(
            f"get_cells_list {sendid} {password}",
            "get_cells_list",
            sendid,
            target_port=target_port,
        )

    def get_current_cell(self, password: str, target_port: Optional[int] = None) -> Optional[int]:
        payload = self._simple_get("CURCELL", password, target_port=target_port)
        try:
            return int(payload) if payload is not None else None
        except (TypeError, ValueError):
            return None

    def close(self):
        self.socket.close()
