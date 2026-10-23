#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dark Flasher — запись .uf2 на nRF52 (nice!nano / Adafruit nRF52 UF2 bootloader).

Почему не Explorer:
  * загрузчик уходит в ребут сразу после последнего блока, том исчезает
    посреди закрытия файла — Windows отдаёт 0x80070022 (ERROR_WRONG_DISK);
  * старая буква диска/окно Explorer'а указывают на уже несуществующий том —
    отсюда ошибка «сразу»;
  * антивирус/кэш добавляют задержку, загрузчик успевает уйти в прошивку.

Что делает Dark Flasher:
  * находит том по Volume GUID и проверяет INFO_UF2.TXT (буква не важна);
  * пишет файл напрямую, с write-through, и стартует в момент появления тома;
  * успехом считает только полностью записанный файл (частичный последний
    блок — ошибка, даже если том уже исчез);
  * показывает прогресс, лог и внятную причину, если что-то не так.

Режимы:
  DarkFlasher.py --check f.uf2   — проверить файл
  DarkFlasher.py --list          — показать UF2-тома
  DarkFlasher.py --flash f.uf2   — прошить из консоли (ждёт плату)
  без аргументов                 — запустить Qt-интерфейс (darkflasher_qt.py)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import struct
import sys
import threading
import time
import zipfile
import zlib
from ctypes import wintypes
from dataclasses import dataclass


APP_NAME = "Dark Flasher"
APP_VERSION = "1.4.1"

DEST_NAME = "FW.UF2"          # короткое 8.3-имя: никаких сюрпризов с LFN
CHUNK_SIZE = 16 * 1024
REBOOT_WAIT = 7.0             # сколько ждать исчезновения тома после записи

UF2_MAGIC0 = 0x0A324655
UF2_MAGIC1 = 0x9E5D5157
# Стандартный UF2 и вариант nice!nano-загрузчика 0.10.0 (в нём 0x0AB16F30).
# Оба встречаются в природе, поэтому принимаем любой из них.
UF2_MAGIC_END_STD = 0x0FE2E000
UF2_MAGIC_END_NICE = 0x0AB16F30
UF2_MAGIC_ENDS = (UF2_MAGIC_END_STD, UF2_MAGIC_END_NICE)
UF2_FAMILY_NRF52840 = 0xADA52840
UF2_BLOCK = 512
UF2_PAYLOAD = 256
MAX_IMAGE_BYTES = 8 * 1024 * 1024
APP_FLASH_BASE = 0x26000

# OTA: сборка Nordic DFU пакета (zip) из готового .uf2
# --------------------------------------------------------------------------

DFU_DEVICE_TYPE = 0x0052       # nRF52840
DFU_DEVICE_REV = 0xFFFF
DFU_SD_ID = 0x00B6             # SoftDevice S140 6.1.1 — как в прошивках DarkTec



# --- заводские настройки внутри образа -------------------------------------
# Метка блока в прошивке (variants/darktec/DarktecFactoryCfg.h). Прошивальщик
# находит её в образе и вписывает настройки — структура UF2 при этом не
# меняется: правим байты внутри уже существующих блоков.
FACTORY_MARKER = b"DARKTEC-FACTORY\x01"
FACTORY_HEADER = 24            # метка(16) + версия + длина + резерв + crc32
FACTORY_PAYLOAD = 128
FACTORY_VERSION = 1

FLAG_NAME = 1 << 0
FLAG_RADIO = 1 << 1
FLAG_CAPACITY = 1 << 2
FLAG_MODULE = 1 << 3


def _utf8_prefix(text: str, max_bytes: int) -> bytes:
    """Обрезает UTF-8 по границе символа, чтобы не получить битый текст."""
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    raw = raw[:max_bytes]
    while raw:
        try:
            raw.decode("utf-8")
            return raw
        except UnicodeDecodeError:
            raw = raw[:-1]
    return b""


def pack_factory_payload(settings: dict) -> tuple[bytes, int]:
    """Собирает 128-байтовую полезную часть и набор флагов «что применять».

    Флаги `use_*` из окна: выключенная настройка не попадает ни в блок, ни в
    набор флагов — прошивка её просто не трогает.
    """
    buf = bytearray(FACTORY_PAYLOAD)
    flags = 0

    if settings.get("use_name", True):
        name = (settings.get("name") or "").strip()
        if name:
            raw = _utf8_prefix(name, 31)
            buf[0:len(raw)] = raw
            flags |= FLAG_NAME

    # В прошивке мощность лежит в том же флаге, что и радио. Поэтому в
    # заводской блок они вшиваются только вместе; раздельно — командами по USB.
    if settings.get("use_radio", True) and settings.get("use_tx", True):
        try:
            freq = float(settings.get("freq") or 0)
            bw = float(settings.get("bw") or 0)
            sf = int(settings.get("sf") or 0)
            cr = int(settings.get("cr") or 0)
            tx = int(settings.get("tx") or 0)
        except (TypeError, ValueError) as e:
            raise Uf2Error("радио: в настройках не числа") from e
        if not (100.0 <= freq <= 6000.0):
            raise Uf2Error("частота вне диапазона 100..6000 МГц")
        if not (7.8 <= bw <= 500.0):
            raise Uf2Error("полоса вне диапазона 7.8..500 кГц")
        if not (5 <= sf <= 12):
            raise Uf2Error("SF вне диапазона 5..12")
        if not (5 <= cr <= 8):
            raise Uf2Error("CR вне диапазона 5..8")
        if not (-9 <= tx <= 22):
            raise Uf2Error("мощность вне диапазона -9..22 дБм")
        struct.pack_into("<ff", buf, 32, freq, bw)
        buf[40] = sf & 0xFF
        buf[41] = cr & 0xFF
        struct.pack_into("<b", buf, 42, tx)
        flags |= FLAG_RADIO

    if settings.get("use_capacity", True):
        try:
            cap = int(settings.get("capacity") or 0)
        except (TypeError, ValueError) as e:
            raise Uf2Error("ёмкость батареи: не число") from e
        if not (1000 <= cap <= 10000):
            raise Uf2Error("ёмкость батареи вне диапазона 1000..10000 мАч")
        buf[43] = 0                      # CAP_USER
        struct.pack_into("<H", buf, 44, cap)
        flags |= FLAG_CAPACITY

    if settings.get("use_module", True):
        try:
            module = int(settings.get("module") or 0)
        except (TypeError, ValueError) as e:
            raise Uf2Error("радиомодуль: не число") from e
        if not (0 <= module <= 3):
            raise Uf2Error("радиомодуль вне диапазона 0..3")
        buf[46] = module & 0xFF
        flags |= FLAG_MODULE

    buf[51] = flags
    return bytes(buf), flags


def find_factory_blob(image: bytes) -> int:
    """Ищет настоящий блок заводских настроек: метка + правдоподобный заголовок.

    Метка может встретиться в образе не один раз (например, если прошивка
    держит её копию в .rodata). Поэтому кандидатов проверяем по заголовку:
    версия раскладки, длина и CRC полезной части.
    """
    found: list[int] = []
    start = 0
    while True:
        i = image.find(FACTORY_MARKER, start)
        if i < 0:
            break
        start = i + 1
        if i + FACTORY_HEADER + FACTORY_PAYLOAD > len(image):
            continue
        if image[i + 16] != FACTORY_VERSION:
            continue
        length = image[i + 17]
        if length == 0:
            found.append(i)          # пустая заготовка — то, что нам нужно
        elif length == FACTORY_PAYLOAD:
            crc = int.from_bytes(image[i + 20:i + 24], "little")
            body = bytes(image[i + FACTORY_HEADER:i + FACTORY_HEADER + FACTORY_PAYLOAD])
            if (zlib.crc32(body) & 0xFFFFFFFF) == crc:
                found.append(i)

    if not found:
        if FACTORY_MARKER in image:
            raise Uf2Error("блок заводских настроек повреждён — прошивка не "
                           "поймёт настройки. Нужна другая версия прошивки")
        raise Uf2Error("в этой прошивке нет блока заводских настроек — "
                       "она не умеет вшивать параметры. Нужна версия с "
                       "поддержкой (см. релизы прошивки)")
    if len(found) > 1:
        raise Uf2Error("в образе несколько блоков заводских настроек — "
                       "патчить нельзя, нужна другая версия прошивки")
    return found[0]


def patch_factory_cfg(image: bytearray, payload: bytes) -> dict:
    """Вписывает полезную часть в образ на месте метки. Возвращает статистику."""
    pos = find_factory_blob(bytes(image))

    version = image[pos + 16]
    if version != FACTORY_VERSION:
        raise Uf2Error(f"раскладка блока v{version}, программа знает "
                       f"только v{FACTORY_VERSION}")

    crc = zlib.crc32(payload) & 0xFFFFFFFF
    image[pos + 17] = FACTORY_PAYLOAD & 0xFF
    image[pos + 18] = 0
    image[pos + 19] = 0
    struct.pack_into("<I", image, pos + 20, crc)
    image[pos + FACTORY_HEADER:pos + FACTORY_HEADER + FACTORY_PAYLOAD] = payload
    return {"offset": pos, "crc": crc, "payload_len": len(payload)}


def apply_factory_cfg_to_uf2(src_path: str, dst_path: str,
                             settings: dict) -> dict:
    """Копия UF2 с вписанными настройками.

    Патчим НЕ файл, а собранный из него образ: в UF2 данные разложены по 256
    байт внутри 512-байтовых блоков, и запись «подряд» по смещению в файле
    разнесла бы блоки. Блоки пересобираются с теми же адресами, размерами и
    номерами — структура файла не меняется, только байты данных.
    """
    payload, flags = pack_factory_payload(settings)
    image, base, blocks = uf2_to_image(src_path)
    img = bytearray(image)
    stats = patch_factory_cfg(img, payload)

    with open(src_path, "rb") as src:
        data = bytearray(src.read())
    total = len(data) // UF2_BLOCK
    for i in range(total):
        _m0, _m1, _flags, addr, size = struct.unpack_from("<5I", data, i * UF2_BLOCK)
        if size != UF2_PAYLOAD:
            raise Uf2Error(f"блок {i}: неожиданный размер данных")
        off = addr - base
        if off < 0 or off + size > len(img):
            raise Uf2Error(f"блок {i}: адрес вне образа")
        chunk = bytes(img[off:off + size])
        data[i * UF2_BLOCK + 32:i * UF2_BLOCK + 32 + len(chunk)] = chunk

    tmp = dst_path + ".part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    stats.update({"flags": flags, "size": os.path.getsize(dst_path),
                  "blocks": total, "base": base, "image": len(img)})
    return stats


def uf2_to_image(path: str) -> tuple[bytes, int, int]:
    """Склеивает блоки UF2 в непрерывный образ: (данные, базовый адрес, блоков).

    Валидацию делает Uf2Info.load — здесь только сборка, чтобы не тащить
    второй набор проверок и не выделять память по адресам из битого файла.
    """
    info = Uf2Info.load(path)
    image = bytearray(b"\xff" * (info.num_blocks * UF2_PAYLOAD))
    with open(path, "rb") as f:
        for idx in range(info.num_blocks):
            block = f.read(UF2_BLOCK)
            if len(block) != UF2_BLOCK:
                raise Uf2Error("файл обрезан")
            _m0, _m1, _flags, addr, payload = struct.unpack_from("<5I", block, 0)
            off = addr - info.base_addr
            if payload != UF2_PAYLOAD or off < 0 or off + payload > len(image):
                raise Uf2Error(f"блок {idx}: адрес вне образа")
            image[off:off + payload] = block[32:32 + payload]
    return bytes(image), info.base_addr, info.num_blocks


def dfu_crc16(data: bytes) -> int:
    """CRC-16/CCITT, начальное значение 0xFFFF — так считает adafruit-nrfutil."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_dfu_zip_from_image(image: bytes, out_path: str,
                             app_version: int = 0xFFFFFFFF,
                             base: int = 0, blocks: int = 0,
                             progress=None) -> dict:
    """Собирает Nordic DFU пакет из готового образа (bytes)."""
    if base != APP_FLASH_BASE:
        raise Uf2Error(
            f"образ начинается с 0x{base:X}, а приложение nRF52840 — "
            f"с 0x{APP_FLASH_BASE:X}; такой образ нельзя лить через DFU")
    if progress:
        progress(0.45)
    image = image.rstrip(b"\xff")          # хвост стирания в пакет не кладём
    if not image:
        raise Uf2Error("пустой образ — нечего паковать")
    if len(image) % 4:
        image += b"\xff" * (4 - len(image) % 4)
    crc = dfu_crc16(image)
    if progress:
        progress(0.75)

    init = struct.pack("<HHIH", DFU_DEVICE_TYPE, DFU_DEVICE_REV, app_version, 1)
    init += struct.pack("<H", DFU_SD_ID)
    init += struct.pack("<H", crc)

    manifest = {"manifest": {"application": {
        "bin_file": "firmware.bin",
        "dat_file": "firmware.dat",
        "init_packet_data": {
            "application_version": app_version,
            "device_revision": DFU_DEVICE_REV,
            "device_type": DFU_DEVICE_TYPE,
            "firmware_crc16": crc,
            "softdevice_req": [DFU_SD_ID],
        },
    }, "dfu_version": 0.5}}

    tmp = out_path + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=4))
            z.writestr("firmware.dat", init)
            z.writestr("firmware.bin", image)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if progress:
        progress(1.0)
    return {"size": os.path.getsize(out_path), "image": len(image),
            "base": base, "blocks": blocks, "crc": crc}


# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000



def _init_win32() -> None:
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    kernel32.FindFirstVolumeW.restype = wintypes.HANDLE
    kernel32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    kernel32.FindNextVolumeW.restype = wintypes.BOOL
    kernel32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.FindVolumeClose.restype = wintypes.BOOL
    kernel32.FindVolumeClose.argtypes = [wintypes.HANDLE]

    kernel32.GetVolumePathNamesForVolumeNameW.restype = wintypes.BOOL
    kernel32.GetVolumePathNamesForVolumeNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]


_init_win32()

WINERROR_RU = {
    2: "том или файл не найден (плата отключилась?)",
    3: "путь не найден",
    5: "доступ запрещён — том занят антивирусом или другой программой",
    19: "том защищён от записи",
    21: "устройство не готово",
    23: "ошибка данных (плохой кабель/питание?)",
    31: "общая ошибка устройства",
    32: "файл занят другим процессом",
    33: "том занят другим процессом",
    34: "Windows потерял том — плата перезагрузилась",
    39: "диск полон",
    87: "неверный параметр",
    112: "на томе нет места",
    121: "таймаут операции (устройство не отвечает)",
    1167: "устройство отключено",
    1231: "сеть недоступна",
}


def winerror_text(code: int) -> str:
    if code in WINERROR_RU:
        return WINERROR_RU[code]
    try:
        return ctypes.FormatError(code).strip() or f"ошибка {code}"
    except Exception:
        return f"ошибка {code}"


# --------------------------------------------------------------------------
# UF2
# --------------------------------------------------------------------------

class Uf2Error(Exception):
    pass


def family_name(family: int) -> str:
    if family == UF2_FAMILY_NRF52840:
        return "nRF52840"
    return f"family 0x{family:08X}"


@dataclass
class Uf2Info:
    path: str
    size: int
    num_blocks: int
    family: int
    base_addr: int
    magic_end: int

    @classmethod
    def load(cls, path: str) -> "Uf2Info":
        try:
            size = os.path.getsize(path)
        except OSError as e:
            raise Uf2Error(f"не могу открыть файл: {e.strerror or e}") from e
        if size < UF2_BLOCK or size % UF2_BLOCK:
            raise Uf2Error("размер не кратен 512 байт — это не UF2")

        num = size // UF2_BLOCK
        if num * UF2_PAYLOAD > MAX_IMAGE_BYTES:
            raise Uf2Error("UF2 слишком большой для nRF52840")
        seen = bytearray(num)
        family = None
        base = None
        magic_end = None
        with open(path, "rb") as f:
            for idx in range(num):
                block = f.read(UF2_BLOCK)
                if len(block) != UF2_BLOCK:
                    raise Uf2Error("файл обрезан")
                magic0, magic1, _flags, addr, payload, block_no, total, fam = \
                    struct.unpack_from("<8I", block, 0)
                block_magic_end, = struct.unpack_from("<I", block, 508)
                if magic0 != UF2_MAGIC0 or magic1 != UF2_MAGIC1:
                    raise Uf2Error(f"блок {idx}: неверная сигнатура UF2 — файл битый")
                if block_magic_end not in UF2_MAGIC_ENDS:
                    raise Uf2Error(
                        f"блок {idx}: незнакомый UF2_MAGIC_END 0x{block_magic_end:08X}"
                    )
                if magic_end is None:
                    magic_end = block_magic_end
                elif block_magic_end != magic_end:
                    raise Uf2Error("в файле смешаны разные варианты UF2")
                if payload != UF2_PAYLOAD:
                    raise Uf2Error(f"блок {idx}: неожиданный размер данных")
                if total != num:
                    raise Uf2Error(
                        f"файл неполный: внутри заявлено {total} блоков, а лежит {num}"
                    )
                if block_no >= num:
                    raise Uf2Error(f"блок {idx}: номер {block_no} вне файла")
                if seen[block_no]:
                    raise Uf2Error(f"блок {block_no} встречается дважды")
                seen[block_no] = 1
                if family is None:
                    family = fam
                elif fam != family:
                    raise Uf2Error("в файле смешаны разные family ID")
                # База не зависит от порядка блоков в файле: UF2 не требует,
                # чтобы block 0 лежал первым.
                block_base = addr - block_no * UF2_PAYLOAD
                if base is None:
                    base = block_base
                elif block_base != base:
                    raise Uf2Error("блоки не образуют непрерывный образ")

        if family is None or family == 0:
            raise Uf2Error("в UF2 нет family ID — не понимаю, для какого это чипа")
        if base is None:
            raise Uf2Error("в UF2 нет данных")

        return cls(path=path, size=size, num_blocks=num, family=family,
                   base_addr=base, magic_end=magic_end or UF2_MAGIC_END_STD)


# --------------------------------------------------------------------------
# Тома
# --------------------------------------------------------------------------

def list_volume_guids() -> list[str]:
    out: list[str] = []
    buf = ctypes.create_unicode_buffer(1024)
    handle = kernel32.FindFirstVolumeW(buf, 1024)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return out
    try:
        while True:
            out.append(buf.value)
            if not kernel32.FindNextVolumeW(handle, buf, 1024):
                break
    finally:
        kernel32.FindVolumeClose(handle)
    return out


def volume_letter(guid: str) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    needed = wintypes.DWORD(0)
    if kernel32.GetVolumePathNamesForVolumeNameW(guid, buf, 1024, ctypes.byref(needed)):
        first = buf.value
        if first:
            return first.rstrip("\\")
    return ""


def volume_label(guid: str) -> str:
    buf = ctypes.create_unicode_buffer(256)
    fs = ctypes.create_unicode_buffer(64)
    serial = wintypes.DWORD(0)
    maxlen = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    if kernel32.GetVolumeInformationW(
        guid, buf, 256, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fs, 64,
    ):
        return buf.value
    return ""


def read_info_uf2(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(8192)
    except OSError:
        return None


@dataclass(frozen=True)
class Uf2Volume:
    path: str
    letter: str
    label: str
    model: str
    board_id: str
    bootloader: str

    @property
    def title(self) -> str:
        return self.model or self.label or "UF2"

    @property
    def where(self) -> str:
        return self.letter or "?"


def probe_uf2_volume(guid: str) -> Uf2Volume | None:
    text = read_info_uf2(guid + "INFO_UF2.TXT")
    if not text or "UF2" not in text.upper():
        return None
    # Не путаем nRF52 с RP2040/другими UF2-загрузчиками: у них тоже есть
    # INFO_UF2.TXT, но прошивка не та.
    if "nrf" not in text.lower() and "adafruit" not in text.lower():
        return None
    model = ""
    board_id = ""
    bootloader = ""
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("uf2 bootloader"):
            bootloader = line[len("UF2 Bootloader"):].strip() or line
        elif low.startswith("model:"):
            model = line.split(":", 1)[1].strip()
        elif low.startswith("board-id:"):
            board_id = line.split(":", 1)[1].strip()
    return Uf2Volume(
        path=guid,
        letter=volume_letter(guid),
        label=volume_label(guid),
        model=model,
        board_id=board_id,
        bootloader=bootloader,
    )


def scan_uf2_volumes() -> list[Uf2Volume]:
    found: list[Uf2Volume] = []
    for guid in list_volume_guids():
        vol = probe_uf2_volume(guid)
        if vol is not None:
            found.append(vol)
    return found


def volume_present(guid: str) -> bool:
    return guid in list_volume_guids()


def wait_volume_gone(guid: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not volume_present(guid):
            return True
        time.sleep(0.12)
    return False


# --------------------------------------------------------------------------
# Запись
# --------------------------------------------------------------------------

@dataclass
class FlashResult:
    ok: bool
    written: int
    total: int
    winerror: int
    message: str
    elapsed: float
    last_block_ok: bool = True
    rebooted: bool = False
    cancelled: bool = False
    no_volume: bool = False

    @property
    def percent(self) -> int:
        return int(self.written * 100 / self.total) if self.total else 0


def _write_all(handle, data: bytes, progress, base: int, total: int) -> tuple[int, int]:
    buf = ctypes.create_string_buffer(data, len(data))
    offset = 0
    size = len(data)
    while offset < size:
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(
            handle, ctypes.byref(buf, offset), size - offset,
            ctypes.byref(written), None,
        )
        if not ok:
            return offset + int(written.value), ctypes.get_last_error()
        if written.value == 0:
            return offset, 31
        offset += int(written.value)
        if progress is not None:
            progress(base + offset, total)
    return offset, 0


def write_uf2(vol_path: str, src_path: str, total: int,
              progress=None, cancelled=None) -> FlashResult:
    """
    Пишет файл на том. Последний 512-байтный блок — отдельным WriteFile:
    если связь оборвалась ровно на нём, это значит, что загрузчик принял
    последний блок и ушёл в ребут (а не что запись провалилась).
    """
    started = time.monotonic()
    try:
        src_size = os.path.getsize(src_path)
    except OSError as e:
        return FlashResult(False, 0, total, 0,
                           f"не могу открыть файл: {e.strerror or e}",
                           time.monotonic() - started)
    if src_size != total:
        return FlashResult(False, 0, total, 0,
                           "файл изменился после проверки — выбери его заново",
                           time.monotonic() - started)
    dest = vol_path + DEST_NAME
    handle = 0
    code = 0
    for attempt in range(4):
        handle = kernel32.CreateFileW(
            dest, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH | FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if handle and handle != INVALID_HANDLE_VALUE:
            break
        code = ctypes.get_last_error()
        # Explorer/антивирус может на мгновение держать файл — даём шанс.
        if code in (5, 32, 33) and attempt < 3:
            time.sleep(0.3)
            continue
        return FlashResult(False, 0, total, code, winerror_text(code),
                           time.monotonic() - started)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return FlashResult(False, 0, total, code, winerror_text(code),
                           time.monotonic() - started)

    written = 0
    err = 0
    last_ok = True
    body = max(0, total - UF2_BLOCK)
    try:
        with open(src_path, "rb") as f:
            remaining = body
            while remaining > 0:
                if cancelled is not None and cancelled():
                    return FlashResult(False, written, total, 0, "отменено",
                                       time.monotonic() - started, cancelled=True)
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    err = 31
                    break
                got, code = _write_all(handle, chunk, progress, written, total)
                written += got
                if code:
                    err = code
                    break
                remaining -= got
            if err == 0:
                tail = f.read(UF2_BLOCK)
                if len(tail) == UF2_BLOCK:
                    got, code = _write_all(handle, tail, progress, written, total)
                    written += got
                    if code:
                        err = code
                        last_ok = False
                else:
                    err = 31
    finally:
        # После ребута CloseHandle может вернуть ошибку — это нормально.
        kernel32.CloseHandle(handle)

    elapsed = time.monotonic() - started
    ok = err == 0 and written == total
    if ok:
        message = "готово"
    elif err:
        message = winerror_text(err)
    else:
        message = "записано не всё"
    return FlashResult(ok, written, total, err, message, elapsed, last_block_ok=last_ok)


# --------------------------------------------------------------------------
# Фоновые потоки
# --------------------------------------------------------------------------

class VolumeWatcher(threading.Thread):
    """Каждые 250 мс смотрит, есть ли UF2-том. UI обновляется через очередь."""

    def __init__(self, q: queue.Queue, interval: float = 0.25):
        super().__init__(name="volume-watcher", daemon=True)
        self.q = q
        self.interval = interval
        self._stop = threading.Event()
        self._pause = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def run(self) -> None:
        last: tuple | None = None
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(0.1)
                continue
            try:
                vols = scan_uf2_volumes()
            except Exception:
                # Один сбой перечисления томов не должен выглядеть как
                # «плата отключилась»: пропускаем цикл, оставляем прошлое.
                self._stop.wait(self.interval)
                continue
            signature = tuple((v.path, v.model, v.board_id) for v in vols)
            if signature != last:
                last = signature
                self.q.put(("volumes", vols))
            self._stop.wait(self.interval)


class FlashJob(threading.Thread):
    def __init__(self, q: queue.Queue, info: Uf2Info, volume: Uf2Volume):
        super().__init__(name="flash-job", daemon=True)
        self.q = q
        self.info = info
        self.volume = volume

    def _resolve_volume(self) -> Uf2Volume | None:
        vols = scan_uf2_volumes()
        if not vols:
            return None
        for v in vols:
            if v.path == self.volume.path:
                return v
        same = [v for v in vols if v.board_id and v.board_id == self.volume.board_id]
        if len(same) == 1:
            return same[0]
        # Единственный «другой» том — это уже другая плата; не шьём в неё.
        return None

    def run(self) -> None:
        started = time.monotonic()
        try:
            vol = self._resolve_volume()
            if vol is None:
                self.q.put(("flash", FlashResult(
                    False, 0, self.info.size, 0,
                    "плата не в режиме загрузчика", time.monotonic() - started,
                    no_volume=True)))
                return
            if vol.path != self.volume.path:
                self.q.put(("log", f"том переподключился: {vol.where}", "warn"))
            res = write_uf2(
                vol.path, self.info.path, self.info.size,
                progress=lambda done, total: self.q.put(("progress", done, total)),
            )
            res.elapsed = time.monotonic() - started
            size = self.info.size

            if res.written == size and res.ok:
                res.ok = True
                res.rebooted = wait_volume_gone(vol.path, REBOOT_WAIT)
                res.message = ("готово" if res.rebooted
                               else "записано, но плата не перезагрузилась")
            else:
                # Частично записанный последний блок — не успех, даже если
                # том исчез: отличить ребут от выдёргивания кабеля нельзя.
                res.ok = False
                gone = not volume_present(vol.path)
                if res.written == 0 and gone:
                    res.no_volume = True
                    res.message = "плата исчезла до начала записи"
                elif not res.last_block_ok:
                    res.message = ("последний блок не подтверждён — "
                                   "прошей ещё раз")
                elif gone:
                    res.message = f"обрыв на {res.percent}% — тома уже нет"
                elif not res.message:
                    res.message = "запись прервалась"
            self.q.put(("flash", res))
        except Exception as e:  # noqa: BLE001 — поток не должен падать молча
            self.q.put(("flash", FlashResult(
                False, 0, self.info.size, 0, f"внутренняя ошибка: {e}",
                time.monotonic() - started)))



def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} МБ"
    return f"{n / 1024:.0f} КБ"


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_std_streams() -> None:
    """В windowed-сборке stdout/stderr = None; даём файл и faulthandler."""
    if sys.stderr is not None and sys.stdout is not None:
        return
    try:
        stream = open(os.path.join(app_dir(), "darkflasher-trace.log"),
                      "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stderr is None:
        sys.stderr = stream
    if sys.stdout is None:
        sys.stdout = stream
    try:
        import faulthandler
        faulthandler.enable(stream, all_threads=True)
    except Exception:
        pass


def _cli_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:
        pass


def _wait_for_volume(timeout: float, verbose: bool = True) -> Uf2Volume | None:
    deadline = time.monotonic() + max(0.0, timeout)
    told = False
    while True:
        vols = scan_uf2_volumes()
        if len(vols) == 1:
            return vols[0]
        if len(vols) > 1:
            _cli_print("подключено несколько UF2-томов — оставь один")
            return None
        if time.monotonic() >= deadline:
            return None
        if verbose and not told:
            _cli_print("жду плату в режиме загрузчика (дважды Reset)…")
            told = True
        time.sleep(0.25)


def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="Dark Flasher", description="Запись .uf2 на nRF52 (nice!nano / Adafruit bootloader)")
    parser.add_argument("--list", action="store_true", help="показать UF2-тома")
    parser.add_argument("--check", metavar="FILE", help="проверить .uf2")
    parser.add_argument("--flash", metavar="FILE", help="прошить .uf2")
    parser.add_argument("--wait", type=float, default=60.0,
                        help="сколько секунд ждать плату (по умолчанию 60)")
    parser.add_argument("--version", action="store_true", help="версия")
    args = parser.parse_args(argv)

    if args.version:
        _cli_print(f"{APP_NAME} {APP_VERSION}")
        return 0

    if args.list:
        vols = scan_uf2_volumes()
        if not vols:
            _cli_print("UF2-тома не найдены")
            return 1
        for v in vols:
            _cli_print(f"{v.where:4} {v.title:20} {v.bootloader:28} {v.path}")
        return 0

    if args.check:
        try:
            info = Uf2Info.load(args.check)
        except (OSError, Uf2Error) as e:
            _cli_print(f"плохой файл: {e}")
            return 2
        _cli_print(f"ok: {os.path.basename(args.check)}")
        _cli_print(f"  размер:   {info.size} байт ({fmt_size(info.size)})")
        _cli_print(f"  блоков:   {info.num_blocks}")
        _cli_print(f"  family:   {family_name(info.family)} (0x{info.family:08X})")
        _cli_print(f"  база:     0x{info.base_addr:08X}")
        _cli_print(f"  end:      0x{info.magic_end:08X}"
                   + ("  (nice!nano)" if info.magic_end == UF2_MAGIC_END_NICE else "  (стандарт)"))
        return 0

    if args.flash:
        try:
            info = Uf2Info.load(args.flash)
        except (OSError, Uf2Error) as e:
            _cli_print(f"плохой файл: {e}")
            return 2
        if info.family != UF2_FAMILY_NRF52840:
            _cli_print(f"это UF2 для {family_name(info.family)}, а нужен nRF52840")
            return 2
        _cli_print(f"файл: {os.path.basename(args.flash)} · {fmt_size(info.size)} · "
                   f"{info.num_blocks} блоков")
        vol = _wait_for_volume(args.wait)
        if vol is None:
            _cli_print("плата не найдена")
            return 1
        _cli_print(f"плата: {vol.title} · {vol.where} · {vol.bootloader or 'bootloader ?'}")
        last = -10

        def progress(done: int, total: int) -> None:
            nonlocal last
            pct = int(done * 100 / total) if total else 0
            if pct >= last + 10:
                last = pct
                _cli_print(f"  {pct:3d}%  {fmt_size(done)} / {fmt_size(total)}")

        res = write_uf2(vol.path, args.flash, info.size, progress=progress)
        if res.written == info.size and res.ok:
            res.ok = True
            res.rebooted = wait_volume_gone(vol.path, REBOOT_WAIT)
        elif not res.last_block_ok:
            res.ok = False
            res.message = "последний блок не подтверждён — прошей ещё раз"
        if res.ok:
            _cli_print(f"готово за {res.elapsed:.1f} с"
                       + (" (плата перезагрузилась)" if res.rebooted else ""))
            return 0
        err = f" (WinError {res.winerror})" if res.winerror else ""
        _cli_print(f"ошибка: {res.message}{err}")
        return 3

    parser.print_help()
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Dark Flasher работает только на Windows")
    ensure_std_streams()
    argv = sys.argv[1:]
    if any(a in ("--list", "--check", "--flash", "--version", "--help", "-h")
           or a.startswith(("--check=", "--flash="))
           for a in argv):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        raise SystemExit(cli(argv))
    from darkflasher_qt import run_gui
    run_gui()


if __name__ == "__main__":
    main()
