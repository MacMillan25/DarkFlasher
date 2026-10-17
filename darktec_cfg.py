"""DarkTec: роли, прошивки из папки firmware и запись настроек по COM.

Без Tkinter: модуль проверяется из консоли и переиспользуется в CLI-режиме.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "darkflasher.json"
FIRMWARE_DIR_NAME = "firmware"
SERIAL_BAUD = 115200

# Бинарный протокол компаньона (MyMesh.cpp): кадр `<` + len16 + payload.
CMD_RUN_CLI = 66       # CMD_RUN_CLI_COMMAND, v14+
RESP_CLI_REPLY = 29    # RESP_CODE_CLI_REPLY

# VID, по которым узнаём nRF52: Adafruit (bootloader и core) и Nordic.
NRF_VIDS = {0x239A, 0x1915}

# Роль -> env PlatformIO.
ROLES: list[tuple[str, str, str]] = [
    ("companion_radio_ble", "Companion (BLE)", "Darktec_companion_radio_ble"),
    ("companion_radio_usb", "Companion (USB)", "Darktec_companion_radio_usb"),
    ("repeater", "Repeater", "Darktec_repeater"),
    ("repeater_bridge_rs232_serial1", "Repeater + RS232",
     "Darktec_repeater_bridge_rs232_serial1"),
    ("room_server", "Room server", "Darktec_room_server"),
    ("sensor", "Sensor + GPS", "Darktec_sensor"),
    ("terminal_chat", "Terminal chat", "Darktec_terminal_chat"),
    ("kiss_modem", "KISS modem", "Darktec_kiss_modem"),
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


def open_serial(port: str, dtr: bool = True):
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
    # DTR поднимаем (как обычный терминал), RTS не трогаем: на nRF CDC это
    # не сброс, а чужой порт мы всё равно не открываем.
    ser.dtr = dtr
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
    # Канал сети одной командой, как принято на форуме: частота, полоса, SF, CR.
    cmds.append(f"set radio {float(s.get('freq', 869.9)):.3f},"
                f"{float(s.get('bw', 62.5)):g},"
                f"{int(s.get('sf', 8))},{int(s.get('cr', 8))}")
    cmds.append(f"set tx {int(s.get('tx', 22))}")
    cmds.append(f"set adc.multiplier {float(s.get('adc', 1.75)):.3f}")
    cmds.append(f"set radio.module {int(s.get('module', 0))}")
    cmds.append(f"set darktec.capacity {int(s.get('capacity', 3000))}")
    return cmds


# ------------------------------------------------------------ онлайн-прошивки

# Манифест лежит в репозитории DarkFlasher: рядом с exe или в main на GitHub.
MANIFEST_URL = ("https://raw.githubusercontent.com/MacMillan25/DarkFlasher/"
                "main/firmware/manifest.json")

# Релизы прошивки: теги вида darktec-*, ассеты — <env>.uf2.
RELEASES_API = ("https://api.github.com/repos/MacMillan25/MeshCore/"
                "releases?per_page=30")


def list_firmware_releases(timeout: float = 15.0) -> list[dict]:
    """Список релизов прошивки: tag, name, date, files{env: {url, sha256}}."""
    req = urllib.request.Request(RELEASES_API, headers={
        "User-Agent": "DarkFlasher",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    out: list[dict] = []
    for rel in data if isinstance(data, list) else []:
        tag = rel.get("tag_name", "")
        if not tag.startswith("darktec-"):
            continue
        files = {}
        for a in rel.get("assets", []):
            name = a.get("name", "")
            if not name.lower().endswith(".uf2"):
                continue
            digest = a.get("digest") or ""
            files[name[:-4]] = {
                "url": a.get("browser_download_url", ""),
                "sha256": digest[7:] if digest.startswith("sha256:") else "",
                "size": a.get("size", 0),
            }
        out.append({
            "tag": tag,
            "name": rel.get("name") or tag,
            "date": (rel.get("published_at") or "")[:10],
            "files": files,
        })
    return out


def fetch_manifest(url: str = MANIFEST_URL, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.load(r)
    return data if isinstance(data, dict) else {}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_firmware(env: str, url: str, sha256: str = "",
                      progress=None, log=print) -> str | None:
    """Скачивает .uf2 в firmware, считает скорость и сверяет sha256.

    progress(done, total, bytes_per_sec) — для статуса «скачивание, N МБ/с».
    """
    os.makedirs(firmware_dirs()[0], exist_ok=True)
    dest = os.path.join(firmware_dirs()[0], env + ".uf2")
    tmp = dest + ".part"
    log(f"скачиваю {env}…")
    h = hashlib.sha256()
    done = 0
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    speed = done / max(0.001, time.time() - started)
                    progress(done, total, speed)
    except Exception as e:  # noqa: BLE001
        log(f"не скачалось: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    got = h.hexdigest()
    if sha256 and got.lower() != sha256.lower():
        log(f"хэш не совпал: ждали {sha256[:12]}…, получили {got[:12]}…")
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
    log(f"sha256 ок: {got[:16]}…")
    return dest


# ------------------------------------------------------------- CLI по USB

def find_meshcore_port(protocol: str = "frames",
                       timeout: float = 15.0) -> str | None:
    """Ищет ноду среди портов nRF: та, что ответила как MeshCore.

    Порт нельзя брать первым попавшимся (чужая nRF52 тоже видна по VID),
    а после прошивки нода ещё грузится — поэтому опрашиваем все порты
    по кругу до таймаута, а не один раз.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in nrf_ports():
            ver = read_version(port, protocol=protocol, dtr=True,
                               timeout=1.5)
            if ver and ("v1." in ver or "repeater" in ver.lower()
                        or "id:" in ver.lower() or "ok" in ver.lower()):
                return port
        time.sleep(0.5)
    return None


def read_version(port: str, protocol: str = "frames",
                 timeout: float = 2.5, dtr: bool = True) -> str:
    """Спрашивает у ноды версию прошивки (CLI `ver`)."""
    if protocol == "text":
        return _text_cmd(port, "ver", timeout, dtr)
    try:
        ser = open_serial(port, dtr)
    except Exception:
        return ""
    try:
        ser.reset_input_buffer()
        payload = bytes([CMD_RUN_CLI]) + b"ver"
        ser.write(b"<" + bytes([len(payload) & 0xFF,
                                (len(payload) >> 8) & 0xFF]) + payload)
        ser.flush()
        return _read_cli_reply(ser, timeout)
    except Exception:
        return ""
    finally:
        try:
            ser.close()
        except Exception:
            pass


def _text_cmd(port: str, cmd: str, timeout: float = 2.5,
              dtr: bool = True) -> str:
    try:
        ser = open_serial(port, dtr)
    except Exception:
        return ""
    try:
        ser.reset_input_buffer()
        ser.write((cmd + "\r\n").encode("ascii", "replace"))
        ser.flush()
        return _read_line(ser, timeout)
    except Exception:
        return ""
    finally:
        try:
            ser.close()
        except Exception:
            pass


def _read_line(ser, timeout: float = 5.0) -> str:
    """Читает ответ текстового CLI: ждёт строку с OK/Error/Unknown.

    Репитер отвечает не сразу и иногда несколькими строками, поэтому первую
    строку брать нельзя — собираем всё окно и возвращаем значимую.
    """
    ser.timeout = 0.2
    deadline = time.time() + timeout
    buf = b""
    meaningful = ""
    while time.time() < deadline:
        try:
            chunk = ser.read(256)
        except Exception:
            break
        if chunk:
            buf += chunk
            text = buf.decode("utf-8", "replace")
            for line in text.splitlines():
                t = line.strip()
                if not t:
                    continue
                low = t.lower()
                if ("ok" in low or "error" in low or "unknown" in low
                        or t.startswith(">")):
                    meaningful = t
            if meaningful:
                # ответ пришёл, но даём добить хвост
                time.sleep(0.15)
                break
    if meaningful:
        return meaningful
    return buf.decode("utf-8", "replace").strip()


def _read_cli_reply(ser, timeout: float = 3.5) -> str:
    """Читает кадр `>` + len + payload и возвращает текст CLI-ответа."""
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        try:
            chunk = ser.read(256)
        except Exception:
            break
        if chunk:
            buf += chunk
            while b">" in buf:
                i = buf.index(b">")
                if len(buf) < i + 3:
                    break
                ln = buf[i + 1] | (buf[i + 2] << 8)
                if len(buf) < i + 3 + ln:
                    break
                payload = buf[i + 3:i + 3 + ln]
                buf = buf[i + 3 + ln:]
                if payload and payload[0] == RESP_CLI_REPLY:
                    return payload[1:].decode("utf-8", "replace").strip()
    return ""


def send_commands_batch(port: str, commands: list[str], protocol: str = "frames",
                        log=print, progress=None) -> bool:
    """Шлёт CLI-команды. Компаньон — кадрами, остальные роли — текстом."""
    if protocol == "text":
        return _send_text(port, commands, log, progress)

    ok = True
    try:
        ser = open_serial(port)
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        n = len(commands)
        for idx, cmd in enumerate(commands):
            payload = bytes([CMD_RUN_CLI]) + cmd.encode("ascii", "replace")
            ser.write(b"<" + bytes([len(payload) & 0xFF,
                                    (len(payload) >> 8) & 0xFF]) + payload)
            ser.flush()
            log(f"> {cmd}")
            if progress:
                progress(0.3 + 0.5 * (idx + 1) / max(1, n))
            reply = _read_cli_reply(ser, timeout=3.5)
            if reply:
                log(f"< {reply}")
                low = reply.lower()
                if "error" in low or "unknown" in low:
                    ok = False
                    log(f"команда не принята: {cmd}")
            else:
                log(f"< (нет ответа на {cmd})")
        if progress:
            progress(1.0)
        payload = bytes([CMD_RUN_CLI]) + b"reboot"
        ser.write(b"<" + bytes([len(payload) & 0xFF,
                                (len(payload) >> 8) & 0xFF]) + payload)
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


def _send_text(port: str, commands: list[str], log=print,
               progress=None) -> bool:
    """Текстовый CLI (репитер, комната, сенсор, мост)."""
    ok = True
    try:
        ser = open_serial(port)
    except Exception as e:  # noqa: BLE001
        log(f"COM {port}: {e}")
        return False
    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        n = len(commands)
        for idx, cmd in enumerate(commands):
            ser.write((cmd + "\r\n").encode("ascii", "replace"))
            ser.flush()
            log(f"> {cmd}")
            if progress:
                progress(0.3 + 0.5 * (idx + 1) / max(1, n))
            reply = _read_line(ser, 5.0)
            if reply:
                log(f"< {reply}")
                low = reply.lower()
                if "error" in low or "unknown" in low:
                    ok = False
                    log(f"команда не принята: {cmd}")
            else:
                log(f"< (нет ответа на {cmd})")
        if progress:
            progress(1.0)
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
