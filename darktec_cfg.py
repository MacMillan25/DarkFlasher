"""DarkTec: роли, прошивки из папки firmware и запись настроек по COM.

Без Tkinter: модуль проверяется из консоли и переиспользуется в CLI-режиме.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "darkflasher.json"
FIRMWARE_DIR_NAME = "firmware"
SERIAL_BAUD = 115200

# VID, по которым узнаём nRF52: Adafruit (bootloader и core) и Nordic.
NRF_VIDS = {0x239A, 0x1915}

# Роль -> env PlatformIO.
ROLES: list[tuple[str, str, str]] = [
    ("companion_radio_ble", "Компаньон (BLE)", "Darktec_companion_radio_ble"),
    ("companion_radio_usb", "Компаньон (USB)", "Darktec_companion_radio_usb"),
    ("repeater", "Репитер", "Darktec_repeater"),
    ("repeater_bridge_rs232_serial1", "Репитер + мост RS232",
     "Darktec_repeater_bridge_rs232_serial1"),
    ("room_server", "Комнатный сервер", "Darktec_room_server"),
    ("sensor", "Сенсор + GPS", "Darktec_sensor"),
    ("terminal_chat", "Чат с терминала", "Darktec_terminal_chat"),
    ("kiss_modem", "KISS-модем", "Darktec_kiss_modem"),
]

ROLE_TITLES = {slug: title for slug, title, _env in ROLES}
ROLE_ENVS = {slug: env for slug, _title, env in ROLES}
ENV_TO_ROLE = {env: slug for slug, _title, env in ROLES}

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
    "last_env": "",
}


# ---------------------------------------------------------------- конфигурация

def app_dir() -> str:
    """Папка рядом с exe (или с исходником при запуске из Python)."""
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
    try:
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path())
    except OSError:
        pass


# ------------------------------------------------------------------- прошивки

def firmware_dirs() -> list[str]:
    """Папка firmware рядом с exe, рядом с исходником и рядом с .bat."""
    dirs = [os.path.join(app_dir(), FIRMWARE_DIR_NAME)]
    if APP_DIR not in dirs:
        dirs.append(os.path.join(APP_DIR, FIRMWARE_DIR_NAME))
    out = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _looks_like_uf2(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"UF2\n"
    except OSError:
        return False


def role_env_from_filename(name: str) -> str | None:
    """Определяет env по имени файла — самый длинный совпавший.

    Понимает и `Darktec_companion_radio_ble.uf2`, и
    `Darktec_companion_radio_ble-v1.18.0-2026-10-17.uf2`.
    """
    low = name.lower()
    best = None
    for _slug, _title, env in ROLES:
        e = env.lower()
        if e in low and (best is None or len(e) > len(best)):
            best = env
        # имя без префикса Darktec_: companion_radio_ble.uf2
        short = e[len("darktec_"):]
        if short in low and (best is None or len(short) > len(best)):
            best = env
    return best


def scan_firmware() -> dict[str, str]:
    """env -> путь к .uf2 из папки firmware (свежий файл при дублях)."""
    found: dict[str, str] = {}
    for d in firmware_dirs():
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".uf2"):
                continue
            path = os.path.join(d, name)
            if not os.path.isfile(path) or not _looks_like_uf2(path):
                continue
            env = role_env_from_filename(name)
            if not env:
                continue
            prev = found.get(env)
            if prev is None or os.path.getmtime(path) > os.path.getmtime(prev):
                found[env] = path
    return found


def role_title(slug: str) -> str:
    return ROLE_TITLES.get(slug, slug)


def role_for_env(env: str) -> str | None:
    return ENV_TO_ROLE.get(env)


# ----------------------------------------------------------------------- COM

def _serial():
    import serial  # pyserial
    return serial


def nrf_ports() -> list[str]:
    """Только порты nRF52 (по VID). Чужие COM не трогаем никогда."""
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return []
    out = []
    for p in lp.comports():
        vid = getattr(p, "vid", None)
        if vid is not None and vid in NRF_VIDS:
            out.append(p.device)
        elif vid is None:
            # Без VID (например, виртуальный) — только если сам себя назвал.
            text = f"{p.description or ''} {p.hwid or ''}".lower()
            if "nrf" in text or "adafruit" in text or "nordic" in text:
                out.append(p.device)
    return out


def open_serial(port: str):
    """Открывает порт, не дёргая DTR/RTS.

    Поднятая DTR часто означает reset/boot у Arduino-совместимых плат;
    на этой шине висят чужие устройства, поэтому линии держим опущенными.
    """
    serial = _serial()
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = SERIAL_BAUD
    ser.timeout = 0.2
    ser.write_timeout = 3
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


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
    """Отправляет CLI-команды по очереди. `reboot` — последней, без ответа."""
    ok = True
    try:
        ser = open_serial(port)
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    try:
        time.sleep(0.4)
        ser.reset_input_buffer()
        for cmd in commands:
            ser.write((cmd + "\r\n").encode("ascii", "replace"))
            ser.flush()
            log(f"> {cmd}")
            if cmd.strip() == "reboot":
                time.sleep(0.4)
                break
            reply = _read_reply(ser)
            if reply:
                log(f"< {reply}")
            low = reply.lower()
            if "error" in low or "unknown" in low:
                ok = False
                log(f"команда не принята: {cmd}")
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return ok


# ------------------------------------------------------------- сборка команд

def build_commands(s: dict) -> list[str]:
    cmds: list[str] = []
    name = str(s.get("name", "")).strip()
    if name:
        cmds.append(f"set name {name}")
    cmds.append(f"set freq {float(s.get('freq', 869.9)):.3f}")
    bw = float(s.get("bw", 62.5))
    cmds.append(f"set bw {bw:g}")
    cmds.append(f"set sf {int(s.get('sf', 8))}")
    cmds.append(f"set cr {int(s.get('cr', 8))}")
    cmds.append(f"set tx {int(s.get('tx', 22))}")
    cmds.append(f"set adc.multiplier {float(s.get('adc', 1.75)):.3f}")
    cmds.append(f"set radio.module {int(s.get('module', 0))}")
    cmds.append(f"set darktec.capacity {int(s.get('capacity', 3000))}")
    return cmds


# ------------------------------------------------------------ онлайн-прошивки

# Манифест лежит в репозитории DarkFlasher: рядом с exe или в main на GitHub.
# Формат: {"version": "1.1", "files": {"<env>": "<url>"}}
MANIFEST_URL = ("https://raw.githubusercontent.com/MacMillan25/DarkFlasher/"
                "main/firmware/manifest.json")


def fetch_manifest(url: str = MANIFEST_URL, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.load(r)
    return data if isinstance(data, dict) else {}


def download_firmware(env: str, url: str, log=print) -> str | None:
    """Скачивает .uf2 в папку firmware. Возвращает путь или None."""
    os.makedirs(firmware_dirs()[0], exist_ok=True)
    dest = os.path.join(firmware_dirs()[0], env + ".uf2")
    tmp = dest + ".part"
    log(f"скачиваю {env}…")
    try:
        with urllib.request.urlopen(url, timeout=90) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        log(f"не скачалось: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    if not _looks_like_uf2(tmp):
        log("скачанный файл не похож на UF2")
        os.remove(tmp)
        return None
    os.replace(tmp, dest)
    return dest


def send_commands_batch(port: str, commands: list[str], log=print,
                        progress=None) -> bool:
    """Отправляет все команды одной посылкой, затем reboot.

    Быстрее, чем по одной: нода разбирает входной буфер построчно, ответы
    приходят пачкой. `progress(0..1)` — для единого индикатора записи.
    """
    ok = True
    try:
        ser = open_serial(port)
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    try:
        time.sleep(0.4)
        ser.reset_input_buffer()
        payload = "".join(c + "\r\n" for c in commands)
        ser.write(payload.encode("ascii", "replace"))
        ser.flush()
        log(f"> {len(commands)} команд одной посылкой")
        if progress:
            progress(0.6)
        time.sleep(1.2)
        reply = _read_reply(ser, 2.0)
        if progress:
            progress(1.0)
        for line in reply.splitlines():
            log(f"< {line}")
        low = reply.lower()
        if "error" in low or "unknown" in low:
            ok = False
            log("часть команд отклонена — смотри ответы выше")
        # Перезагрузка: применяет freq/bw/sf/cr/tx.
        ser.write(b"reboot\r\n")
        ser.flush()
        time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return ok
