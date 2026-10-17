"""DarkTec: роли, прошивки и запись настроек по COM.

Модуль намеренно без Tkinter: его можно проверить из консоли и переиспользовать
в CLI-режиме DarkFlasher.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "darkflasher.json"
CACHE_DIR_NAME = "fw"
SERIAL_BAUD = 115200

# Роль -> env PlatformIO. Порядок совпадает с тем, что удобно человеку.
ROLES: list[tuple[str, str, str]] = [
    ("companion_radio_ble", "Компаньон (BLE)", "Darktec_companion_radio_ble"),
    ("companion_radio_usb", "Компаньон (USB)", "Darktec_companion_radio_usb"),
    ("repeater", "Репитер", "Darktec_repeater"),
    ("repeater_bridge_rs232_serial1", "Репитер + мост RS232", "Darktec_repeater_bridge_rs232_serial1"),
    ("room_server", "Комнатный сервер", "Darktec_room_server"),
    ("sensor", "Сенсор + GPS", "Darktec_sensor"),
    ("terminal_chat", "Чат с терминала", "Darktec_terminal_chat"),
    ("kiss_modem", "KISS-модем", "Darktec_kiss_modem"),
]

ROLE_TITLES = {slug: title for slug, title, _env in ROLES}
ROLE_ENVS = {slug: env for slug, _title, env in ROLES}

# Порядок обязан совпадать с DarktecPrefsFormat::ModuleCode в прошивке.
MODULES = [
    (0, "E22-900M33S"),
    (1, "E22-900M30S"),
    (2, "HT-RA62"),
    (3, "E22P-868M30S"),
]

DEFAULT_SETTINGS = {
    "role": "companion_radio_ble",
    "name": "",
    "freq": 869.900,
    "bw": 62.5,
    "sf": 8,
    "cr": 8,
    "tx": 22,
    "capacity": 3000,
    "module": 0,
    "adc": 1.750,
    "send_after_flash": True,
}


# ---------------------------------------------------------------- конфигурация

def app_dir() -> str:
    """Папка рядом с exe (или с исходником при запуске из Python)."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return APP_DIR


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_NAME)


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in data})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data: dict) -> None:
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path())


# ------------------------------------------------------------------- прошивки

def fw_dir() -> str:
    return os.path.join(app_dir(), CACHE_DIR_NAME)


def manifest_path() -> str:
    return os.path.join(fw_dir(), "manifest.json")


def load_manifest() -> dict:
    """Манифест: {"files": {"<env>": "<url или имя файла>", ...}}.

    Его можно положить в fw/ рядом с exe или скачать при релизе. Если файла
    нет — работаем только с локальными .uf2 в fw/.
    """
    try:
        with open(manifest_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _looks_like_uf2(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head == b"UF2\n"
    except OSError:
        return False


def find_local_firmware(env: str) -> str | None:
    """Ищет локальный .uf2 для env: сначала точное имя, потом по префиксу."""
    d = fw_dir()
    if not os.path.isdir(d):
        return None
    exact = os.path.join(d, env + ".uf2")
    if _looks_like_uf2(exact):
        return exact
    found = []
    for name in os.listdir(d):
        if name.lower().endswith(".uf2") and name.startswith(env):
            p = os.path.join(d, name)
            if _looks_like_uf2(p):
                found.append(p)
    if not found:
        return None
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found[0]


def firmware_url(env: str) -> str | None:
    files = load_manifest().get("files")
    if not isinstance(files, dict):
        return None
    v = files.get(env)
    if isinstance(v, str) and v.lower().startswith(("http://", "https://")):
        return v
    return None


def download_firmware(env: str, log=print) -> str | None:
    """Скачивает прошивку по манифесту в fw/. Возвращает путь или None."""
    url = firmware_url(env)
    if not url:
        return None
    os.makedirs(fw_dir(), exist_ok=True)
    dest = os.path.join(fw_dir(), env + ".uf2")
    tmp = dest + ".part"
    log(f"скачиваю {env}: {url}")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)
    if not _looks_like_uf2(dest):
        log("скачанный файл не похож на UF2")
        return None
    return dest


def resolve_firmware(env: str, log=print) -> str | None:
    """Локальный файл, иначе скачивание по манифесту."""
    local = find_local_firmware(env)
    if local:
        return local
    return download_firmware(env, log=log)


# ----------------------------------------------------------------------- COM

def _serial():
    import serial  # pyserial
    return serial


def list_ports() -> list[str]:
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:
        return []


def wait_for_new_port(before: list[str], timeout: float = 15.0, poll: float = 0.4) -> str | None:
    """Ждёт COM-порт, которого не было до прошивки (нода после ребута)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = list_ports()
        new = [p for p in now if p not in before]
        if new:
            return new[0]
        time.sleep(poll)
    return None


def find_node_port(timeout: float = 20.0, log=print) -> str | None:
    """Ищет ноду среди COM-портов, опрашивая её командой `ver`.

    Номер порта после прошивки может остаться прежним, поэтому имя не
    показатель: открываем порт, спрашиваем версию и верим ответу.
    """
    serial = _serial()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in list_ports():
            try:
                with serial.Serial(port, SERIAL_BAUD, timeout=0.2,
                                   write_timeout=1) as ser:
                    time.sleep(0.3)
                    ser.reset_input_buffer()
                    ser.write(b"ver\r\n")
                    reply = _read_reply(ser, 1.0)
            except Exception:
                continue
            if reply and ("dark" in reply.lower() or "v1." in reply.lower()
                          or "ok" in reply.lower()):
                log(f"нода отвечает на {port}: {reply}")
                return port
        time.sleep(1.0)
    return None


def _read_reply(ser, timeout: float = 2.0) -> str:
    ser.timeout = 0.2
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        try:
            chunk = ser.read(256)
        except Exception:
            break
        if chunk:
            buf += chunk
            if b"\n" in buf:
                break
    return buf.decode("utf-8", "replace").strip()


def send_commands(port: str, commands: list[str], log=print) -> bool:
    """Отправляет CLI-команды по очереди, ждёт ответ на каждую.

    `reboot` идёт последней и ответа не ждёт: нода уходит в перезагрузку.
    """
    serial = _serial()
    ok = True
    try:
        with serial.Serial(port, SERIAL_BAUD, timeout=0.2, write_timeout=3) as ser:
            time.sleep(0.5)  # нода поднимает USB CDC не мгновенно
            ser.reset_input_buffer()
            for cmd in commands:
                ser.write((cmd + "\r\n").encode("ascii", "replace"))
                ser.flush()
                log(f"> {cmd}")
                if cmd.strip() == "reboot":
                    time.sleep(0.5)
                    break
                reply = _read_reply(ser)
                if reply:
                    log(f"< {reply}")
                low = reply.lower()
                if "error" in low or "unknown" in low:
                    ok = False
                    log(f"команда не принята: {cmd}")
    except Exception as e:  # noqa: BLE001 - показываем человеку текстом
        log(f"COM {port}: {e}")
        return False
    return ok


# ------------------------------------------------------------- сборка команд

def build_commands(s: dict) -> list[str]:
    """Команды для записи настроек. Пустые поля не отправляем."""
    cmds: list[str] = []

    name = str(s.get("name", "")).strip()
    if name:
        cmds.append(f"set name {name}")

    cmds.append(f"set freq {float(s.get('freq', 869.9)):.3f}")
    cmds.append(f"set bw {float(s.get('bw', 62.5)):.1f}".replace(".0", ""))
    cmds.append(f"set sf {int(s.get('sf', 8))}")
    cmds.append(f"set cr {int(s.get('cr', 8))}")
    cmds.append(f"set tx {int(s.get('tx', 22))}")
    cmds.append(f"set adc.multiplier {float(s.get('adc', 1.75)):.3f}")
    cmds.append(f"set radio.module {int(s.get('module', 0))}")
    cmds.append(f"set darktec.capacity {int(s.get('capacity', 3000))}")
    return cmds


def role_title(slug: str) -> str:
    return ROLE_TITLES.get(slug, slug)
