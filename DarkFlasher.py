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
  * обрыв связи в самом конце считает успехом (все блоки уже приняты);
  * показывает прогресс, лог и внятную причину, если что-то не так.

Режимы:
  DarkFlasher.py                 — GUI
  DarkFlasher.py --check f.uf2   — проверить файл
  DarkFlasher.py --list          — показать UF2-тома
  DarkFlasher.py --flash f.uf2   — прошить из консоли (ждёт плату)
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import re
import struct
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime

APP_NAME = "Dark Flasher"
APP_VERSION = "1.1"
WINDOW_TITLE = "Dark Flasher — nRF52 UF2"

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

# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
DRIVE_REMOVABLE = 2

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
)


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

    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
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
    kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    kernel32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]

    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
    ]
    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetDpiForSystem.restype = wintypes.UINT
    user32.GetDpiForSystem.argtypes = []

    shell32.DragAcceptFiles.restype = None
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragFinish.restype = None
    shell32.DragFinish.argtypes = [wintypes.HANDLE]

    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
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
    39: "том занят",
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
    data_bytes: int
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
                if base is None:
                    base = addr
                elif addr != base + block_no * UF2_PAYLOAD:
                    raise Uf2Error("блоки идут не подряд — это не обычная прошивка")

        if family is None or family == 0:
            raise Uf2Error("в UF2 нет family ID — не понимаю, для какого это чипа")
        if len(seen) != num:
            raise Uf2Error("файл неполный: не хватает блоков")

        return cls(path=path, size=size, num_blocks=num, family=family,
                   base_addr=base or 0, data_bytes=num * UF2_PAYLOAD,
                   magic_end=magic_end or UF2_MAGIC_END_STD)


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


def volume_free(guid: str) -> int:
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    total_free = ctypes.c_ulonglong(0)
    if kernel32.GetDiskFreeSpaceExW(
        guid, ctypes.byref(free), ctypes.byref(total), ctypes.byref(total_free)
    ):
        return int(free.value)
    return 0


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
    free: int

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
        free=volume_free(guid),
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
    dest = vol_path + DEST_NAME
    handle = kernel32.CreateFileW(
        dest, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH | FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
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
                vols = []
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
        if len(vols) == 1:
            return vols[0]
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

            if res.written >= self.info.size - UF2_BLOCK:
                self.q.put(("log", "файл записан, жду перезагрузку платы…", "info"))
                self.q.put(("status", "файл записан, жду перезагрузку платы…"))
                gone = wait_volume_gone(vol.path, REBOOT_WAIT)
                res.rebooted = gone
                if gone:
                    res.ok = True
                    if res.written < self.info.size:
                        res.message = "последний блок ушёл в момент ребута"
                    else:
                        res.message = "готово"
                else:
                    res.ok = res.written == self.info.size
                    if res.ok:
                        res.message = "записано, но плата не перезагрузилась"
                    else:
                        res.message = "последний блок не записался"
            else:
                res.ok = False
                gone = not volume_present(vol.path)
                if res.written == 0 and gone:
                    res.no_volume = True
                    res.message = "плата исчезла до начала записи"
                elif gone:
                    res.message = f"обрыв на {res.percent}% — тома уже нет"
                elif not res.message:
                    res.message = "запись прервалась"
            self.q.put(("flash", res))
        except Exception as e:  # noqa: BLE001 — поток не должен падать молча
            self.q.put(("flash", FlashResult(
                False, 0, self.info.size, 0, f"внутренняя ошибка: {e}",
                time.monotonic() - started)))


# --------------------------------------------------------------------------
# Оформление
# --------------------------------------------------------------------------

BG = "#0e1116"
PANEL = "#151a22"
PANEL2 = "#1b2230"
BORDER = "#2a3242"
BORDER_HOVER = "#3d4a61"
TEXT = "#e6edf3"
DIM = "#8b98a9"
FAINT = "#5b6779"
ACCENT = "#5ee0a0"
ACCENT_DARK = "#2e6b52"
WARN = "#f2b84b"
WARN_DARK = "#6b5426"
ERR = "#ff6b6b"
ERR_DARK = "#6e2f35"
BLUE = "#6cb6ff"
BLUE_DARK = "#2b4c6b"

# ------------------------------- geometry ---------------------------------
S = 1.0  # DPI scale, ставится в launch_gui()


def X(v: float) -> int:
    return int(round(v * S))


def enable_dpi() -> int:
    dpi = 96
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        dpi = 96
    return dpi if dpi >= 72 else 96


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} МБ"
    return f"{n / 1024:.0f} КБ"


def elide(text: str, font, max_px: int) -> str:
    if max_px <= 0:
        return ""
    if font.measure(text) <= max_px:
        return text
    while text and font.measure(text + "…") > max_px:
        text = text[:-1]
    return text + "…"


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_std_streams() -> None:
    """
    В windowed-сборке (и pythonw) sys.stdout/stderr = None. Исключение внутри
    ctypes-колбэка в таком процессе может закончиться CRT-abort (0xc0000409),
    поэтому даём потокам файл и включаем faulthandler.
    """
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


class App:
    def __init__(self, root, start_path: str | None = None):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import filedialog, messagebox

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.vols: list[Uf2Volume] = []
        self.info: Uf2Info | None = None
        self.file_path: str | None = None
        self.state = "idle"
        self.progress = 0.0
        self.busy = False
        self.status_text = ""
        self.last_error = ""
        self._pulse_on = False
        self._pulse_job = None
        self._zone_sub_id = None
        self._hover = False
        self._last_drop_at = 0.0
        self._closed = False
        self._log_err_shown = False
        self._drop_queue: list[int] = []
        self._drop_seen: set[int] = set()

        root.title(WINDOW_TITLE)
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda _e: self.on_close())
        root.bind("<Control-o>", lambda _e: self.choose_file())

        self.W = X(620)
        self.H = X(560)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = max(0, (sw - self.W) // 2)
        y = max(0, (sh - self.H) // 3)
        root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.f_title = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.f_sub = tkfont.Font(family="Segoe UI", size=9)
        self.f_zone = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self.f_zone2 = tkfont.Font(family="Segoe UI", size=9)
        self.f_name = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.f_meta = tkfont.Font(family="Segoe UI", size=9)
        self.f_status = tkfont.Font(family="Segoe UI", size=9)
        self.f_badge = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.f_log = tkfont.Font(family="Consolas", size=9)

        self._build()
        self._install_drop_target()
        self._set_icon()
        self._log_file_banner()

        self.watcher = VolumeWatcher(self.q)
        self.watcher.start()
        self.root.after(50, self._pump)
        self.root.after(60, self._poll_drops)
        self.root.after(120, self._dark_titlebar)
        self.root.after(250, self._refresh_idle)

        if start_path and os.path.isfile(start_path):
            self.root.after(450, lambda: self.on_drop([start_path]))

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        tk = self.tk
        root = self.root

        # header -----------------------------------------------------------
        self.logo = tk.Canvas(root, width=X(34), height=X(34), bg=BG,
                              highlightthickness=0, bd=0)
        self.logo.place(x=X(24), y=X(20))
        self._draw_logo(self.logo, X(34), X(34))

        tk.Label(root, text=APP_NAME, bg=BG, fg=TEXT, font=self.f_title,
                 anchor="w").place(x=X(70), y=X(17), width=X(260), height=X(24))
        tk.Label(root, text=f"nRF52 · UF2 · v{APP_VERSION}", bg=BG, fg=FAINT,
                 font=self.f_sub, anchor="w").place(x=X(71), y=X(42),
                                                    width=X(300), height=X(18))

        self.badge = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0,
                               height=X(26))
        self.badge.place(x=X(620 - 24 - 150), y=X(24), width=X(150), height=X(26))

        # drop zone --------------------------------------------------------
        self.zone_w = X(620 - 48)
        self.zone_h = X(178)
        self.zone = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0,
                              width=self.zone_w, height=self.zone_h,
                              cursor="hand2")
        self.zone.place(x=X(24), y=X(80), width=self.zone_w, height=self.zone_h)
        self.zone.bind("<Button-1>", lambda _e: self.choose_file())
        self.zone.bind("<Enter>", self._on_zone_enter)
        self.zone.bind("<Leave>", self._on_zone_leave)

        # file row ---------------------------------------------------------
        self.name_lbl = tk.Label(root, text="файл не выбран", bg=BG, fg=FAINT,
                                 font=self.f_meta, anchor="w")
        self.name_lbl.place(x=X(24), y=X(272), width=X(386), height=X(20))
        self.meta_lbl = tk.Label(root, text="", bg=BG, fg=FAINT,
                                 font=self.f_meta, anchor="e")
        self.meta_lbl.place(x=X(416), y=X(273), width=X(180), height=X(18))

        # progress ---------------------------------------------------------
        self.bar = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0,
                             height=X(6))
        self.bar.place(x=X(24), y=X(302), width=self.zone_w, height=X(6))

        self.status_lbl = tk.Label(root, text="", bg=BG, fg=DIM,
                                   font=self.f_status, anchor="w")
        self.status_lbl.place(x=X(24), y=X(316), width=X(500), height=X(18))
        self.pct_lbl = tk.Label(root, text="", bg=BG, fg=DIM,
                                font=self.f_status, anchor="e")
        self.pct_lbl.place(x=X(536), y=X(316), width=X(60), height=X(18))

        # divider + log ----------------------------------------------------
        tk.Frame(root, bg=BORDER, height=X(1), bd=0).place(
            x=X(24), y=X(344), width=self.zone_w, height=X(1))
        tk.Label(root, text="ЛОГ", bg=BG, fg=FAINT,
                 font=self.f_badge).place(x=X(24), y=X(352))

        self.logw = tk.Text(root, bg=PANEL, fg=DIM, bd=0, relief="flat",
                            highlightthickness=0, wrap="word",
                            font=self.f_log, padx=X(10), pady=X(8),
                            insertbackground=TEXT, selectbackground=BLUE_DARK,
                            cursor="arrow", spacing1=X(1), spacing3=X(1))
        self.logw.place(x=X(24), y=X(368), width=self.zone_w, height=X(170))
        self.logw.tag_configure("time", foreground=FAINT)
        self.logw.tag_configure("dim", foreground=FAINT)
        self.logw.tag_configure("info", foreground=DIM)
        self.logw.tag_configure("ok", foreground=ACCENT)
        self.logw.tag_configure("warn", foreground=WARN)
        self.logw.tag_configure("err", foreground=ERR)
        self.logw.configure(state="disabled")

        self._draw_progress()
        self._draw_badge()
        self._redraw_zone()

    def _draw_logo(self, c, w: int, h: int) -> None:
        tk = self.tk
        c.delete("all")
        cx, cy = w / 2, h / 2
        r = X(9)
        # корпус чипа
        self._poly_round(c, cx - r, cy - r, cx + r, cy + r, X(3),
                         fill="", outline=ACCENT, width=max(1, X(1.6)))
        # выводы
        pin = X(3)
        for i in (-1, 1):
            off = i * X(4)
            c.create_line(cx - r - pin, cy + off, cx - r, cy + off,
                          fill=ACCENT, width=max(1, X(1.4)))
            c.create_line(cx + r, cy + off, cx + r + pin, cy + off,
                          fill=ACCENT, width=max(1, X(1.4)))
            c.create_line(cx + off, cy - r - pin, cx + off, cy - r,
                          fill=ACCENT, width=max(1, X(1.4)))
            c.create_line(cx + off, cy + r, cx + off, cy + r + pin,
                          fill=ACCENT, width=max(1, X(1.4)))
        # стрелка внутрь
        c.create_line(cx, cy - X(4), cx, cy + X(3), fill=ACCENT,
                      width=max(1, X(1.5)), capstyle=tk.ROUND)
        c.create_line(cx - X(3), cy, cx, cy + X(4), fill=ACCENT,
                      width=max(1, X(1.5)), capstyle=tk.ROUND)
        c.create_line(cx + X(3), cy, cx, cy + X(4), fill=ACCENT,
                      width=max(1, X(1.5)), capstyle=tk.ROUND)

    @staticmethod
    def _round_points(x1, y1, x2, y2, r, steps=6):
        pts = []
        corners = (
            (x1 + r, y1 + r, 180, 270),
            (x2 - r, y1 + r, 270, 360),
            (x2 - r, y2 - r, 0, 90),
            (x1 + r, y2 - r, 90, 180),
        )
        for cx, cy, a0, a1 in corners:
            for i in range(steps + 1):
                a = math.radians(a0 + (a1 - a0) * i / steps)
                pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    def _poly_round(self, c, x1, y1, x2, y2, r, fill="", outline="",
                    width=1, dash=None):
        pts = self._round_points(x1, y1, x2, y2, max(0, r))
        if fill:
            c.create_polygon(pts, fill=fill, outline="")
        if outline:
            if dash:
                c.create_line(pts + pts[:2], fill=outline, width=width,
                              dash=dash, joinstyle=self.tk.ROUND)
            else:
                c.create_polygon(pts, fill="", outline=outline, width=width,
                                 joinstyle=self.tk.ROUND)

    def _draw_progress(self) -> None:
        c = self.bar
        c.delete("all")
        if self.info is None and self.progress <= 0:
            return
        w = self.zone_w
        h = X(6)
        self._poly_round(c, 0, 0, w, h, h / 2, fill=PANEL2)
        p = max(0.0, min(1.0, self.progress))
        if p > 0:
            if self.state == "error":
                col = ERR
            elif self.state == "warn":
                col = WARN
            else:
                col = ACCENT
            fw = max(h, w * p)
            fw = min(w, fw)
            self._poly_round(c, 0, 0, fw, h, h / 2, fill=col)

    def _draw_badge(self) -> None:
        c = self.badge
        c.delete("all")
        text, dot, fg = self._badge_data()
        tw = self.f_badge.measure(text)
        w = tw + X(30)
        h = X(26)
        # канвас шире — прижимаем пилюлю вправо
        x1 = X(150) - w
        self._poly_round(c, x1, 0, X(150), h, h / 2, fill=PANEL2, outline=BORDER)
        c.create_oval(x1 + X(11), h / 2 - X(3), x1 + X(17), h / 2 + X(3),
                      fill=dot, outline="")
        c.create_text(x1 + X(22), h / 2, text=text, anchor="w",
                      fill=fg, font=self.f_badge)

    def _badge_data(self):
        if self.state == "flash":
            return f"запись {int(self.progress * 100)}%", BLUE, TEXT
        if self.state == "armed":
            return "жду плату", WARN, TEXT
        if self.state == "done":
            return "готово", ACCENT, TEXT
        if self.state == "warn":
            return "проверь", WARN, TEXT
        if self.state == "error":
            return "ошибка", ERR, TEXT
        if len(self.vols) == 1:
            v = self.vols[0]
            return f"{v.title} · {v.where}", ACCENT, TEXT
        if len(self.vols) > 1:
            return f"{len(self.vols)} платы", WARN, TEXT
        return "нет платы", FAINT, DIM

    def _zone_style(self):
        if self.state == "flash":
            return BLUE, BLUE, "Пишу прошивку…"
        if self.state == "armed":
            border = WARN if self._pulse_on else WARN_DARK
            return border, WARN, "Жду плату…"
        if self.state == "done":
            return ACCENT, ACCENT, "Готово"
        if self.state == "warn":
            return WARN, WARN, "Записано"
        if self.state == "error":
            return ERR, ERR, "Не вышло"
        if self._hover:
            return BORDER_HOVER, ACCENT, "Перетащи сюда .uf2"
        return BORDER, ACCENT, "Перетащи сюда .uf2"

    def _zone_sub(self) -> str:
        if self.state == "flash":
            if self.info:
                return f"{int(self.progress * 100)}% · {fmt_size(self.info.size)}"
            return "…"
        if self.state == "armed":
            if len(self.vols) > 1:
                return "подключено несколько плат — оставь одну"
            return "дважды нажми Reset на плате"
        if self.state == "done":
            return "плата перезагружается в прошивку"
        if self.state == "warn":
            return "нажми Reset, если плата не запустилась"
        if self.state == "error":
            text = self.last_error or "попробуй ещё раз"
            return elide(text, self.f_zone2, self.zone_w - X(30))
        if self.info:
            return "нажми или перетащи другой файл"
        return "или нажми, чтобы выбрать файл"

    def _redraw_zone(self) -> None:
        c = self.zone
        tk = self.tk
        c.delete("all")
        w, h = self.zone_w, self.zone_h
        border, icon_col, title = self._zone_style()
        self._poly_round(c, 0, 0, w - 1, h - 1, X(16), fill=PANEL)
        self._poly_round(c, 0, 0, w - 1, h - 1, X(16), outline=border,
                         width=X(1.5 if self.state != "idle" else 1),
                         dash=None if self.state != "idle" else (X(4), X(5)))

        cx = w / 2
        cy = X(60)
        # иконка «скачать в лоток»
        c.create_line(cx, cy - X(20), cx, cy + X(2), fill=icon_col,
                      width=X(2.2), capstyle=tk.ROUND)
        c.create_line(cx - X(8), cy - X(6), cx, cy + X(3), fill=icon_col,
                      width=X(2.2), capstyle=tk.ROUND)
        c.create_line(cx + X(8), cy - X(6), cx, cy + X(3), fill=icon_col,
                      width=X(2.2), capstyle=tk.ROUND)
        c.create_line(cx - X(17), cy + X(8), cx - X(17), cy + X(18),
                      fill=icon_col, width=X(2.2), capstyle=tk.ROUND)
        c.create_line(cx - X(17), cy + X(18), cx + X(17), cy + X(18),
                      fill=icon_col, width=X(2.2), capstyle=tk.ROUND)
        c.create_line(cx + X(17), cy + X(8), cx + X(17), cy + X(18),
                      fill=icon_col, width=X(2.2), capstyle=tk.ROUND)

        c.create_text(cx, X(124), text=title, fill=TEXT, font=self.f_zone,
                      anchor="center")
        self._zone_sub_id = c.create_text(
            cx, X(149), text=self._zone_sub(), fill=DIM,
            font=self.f_zone2, anchor="center", width=w - X(30))

    # ------------------------------------------------------------- state
    def _set_state(self, state: str) -> None:
        self.state = state
        self._redraw_zone()
        self._draw_badge()
        self._draw_progress()

    def _refresh_idle(self) -> None:
        if self.busy or self.state != "idle":
            return
        if len(self.vols) == 1:
            v = self.vols[0]
            self._status(f"плата: {v.title} · {v.where} · {v.bootloader or 'bootloader'}")
        elif len(self.vols) > 1:
            self._status("подключено несколько плат — оставь одну в режиме загрузчика")
        else:
            self._status("плата не найдена — подключи и дважды нажми Reset")
        self._draw_badge()

    def _status(self, text: str) -> None:
        self.status_text = text
        shown = elide(text, self.f_status, X(486))
        self.status_lbl.configure(text=shown)

    def _set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(1.0, value))
        self._draw_progress()
        self.pct_lbl.configure(text=f"{int(self.progress * 100)}%")
        self._draw_badge()
        if self.state == "flash" and self._zone_sub_id is not None:
            self.zone.itemconfigure(self._zone_sub_id, text=self._zone_sub())

    # ------------------------------------------------------------- logging
    def log(self, message: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            self.logw.configure(state="normal")
            self.logw.insert("end", ts + "  ", "time")
            self.logw.insert("end", message + "\n", level)
            lines = int(self.logw.index("end-1c").split(".")[0])
            if lines > 500:
                self.logw.delete("1.0", "101.0")
            self.logw.see("end")
            self.logw.configure(state="disabled")
        except Exception:
            pass
        err = self._log_to_file(message, level)
        if err and not self._log_err_shown:
            self._log_err_shown = True
            try:
                self.logw.configure(state="normal")
                self.logw.insert("end", f"лог-файл: {err}\n", "err")
                self.logw.see("end")
                self.logw.configure(state="disabled")
            except Exception:
                pass

    def _log_to_file(self, message: str, level: str) -> str | None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {message}\n"
        candidates: list[str] = [os.path.join(app_dir(), "darkflasher.log")]
        base = os.environ.get("LOCALAPPDATA")
        if base:
            candidates.append(os.path.join(base, "DarkFlasher", "darkflasher.log"))
        temp = os.environ.get("TEMP")
        if temp:
            candidates.append(os.path.join(temp, "darkflasher.log"))

        last: Exception | None = None
        for path in candidates:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                    try:
                        os.replace(path, path + ".1")
                    except OSError:
                        pass
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
                return None
            except OSError as e:
                last = e
        if last is None:
            return "нет доступного пути для лога"
        return f"{type(last).__name__}: {last}"

    def _log_file_banner(self) -> None:
        self.log(f"{APP_NAME} {APP_VERSION} · nRF52 UF2", "ok")
        self.log("перетащи .uf2 в окно или нажми на него", "dim")

    # ------------------------------------------------------------- actions
    def choose_file(self) -> None:
        if self.busy:
            return
        path = self.filedialog.askopenfilename(
            parent=self.root, title="Выбери прошивку .uf2",
            filetypes=[("UF2 firmware", "*.uf2"), ("Все файлы", "*.*")])
        if path:
            self.on_drop([path])

    def on_drop(self, paths: list[str]) -> None:
        now = time.monotonic()
        if now - self._last_drop_at < 0.35:
            return
        self._last_drop_at = now
        if self.busy:
            self.log("идёт запись — дождись окончания", "warn")
            return
        uf2 = next((p for p in paths if p.lower().endswith(".uf2")
                    and os.path.isfile(p)), None)
        if not uf2:
            self._fail("Нужен файл .uf2")
            return
        try:
            info = Uf2Info.load(uf2)
        except (OSError, Uf2Error) as e:
            self._fail(str(e))
            return
        if info.family != UF2_FAMILY_NRF52840:
            self._fail(f"это UF2 для {family_name(info.family)}, а нужен nRF52840")
            return

        self.info = info
        self.file_path = uf2
        self.progress = 0.0
        self.pct_lbl.configure(text="0%")
        name = os.path.basename(uf2)
        self.name_lbl.configure(text=elide(name, self.f_name, X(378)),
                                fg=TEXT, font=self.f_name)
        self.meta_lbl.configure(
            text=f"{fmt_size(info.size)} · {info.num_blocks} бл. · {family_name(info.family)}",
            fg=FAINT)
        self.log(f"файл: {name}", "info")
        self.log(f"{fmt_size(info.size)} · {info.num_blocks} блоков · {family_name(info.family)} · "
                 f"база 0x{info.base_addr:08X} · end 0x{info.magic_end:08X}", "info")
        self._draw_progress()

        if len(self.vols) == 1:
            self.start_flash(self.vols[0])
        else:
            self.arm()

    def _fail(self, message: str) -> None:
        self._stop_pulse()
        self.info = None
        self.file_path = None
        self.last_error = message
        self.progress = 0.0
        self.name_lbl.configure(text="файл не выбран", fg=FAINT,
                                font=self.f_meta)
        self.meta_lbl.configure(text="")
        self.pct_lbl.configure(text="")
        self._set_state("error")
        self._status(message)
        self.log(message, "err")

    def arm(self) -> None:
        self.busy = False
        self.pct_lbl.configure(text="")
        self._set_state("armed")
        if len(self.vols) > 1:
            self._status("подключено несколько плат — оставь одну в режиме загрузчика")
            self.log("подключено несколько плат — оставь одну", "warn")
        else:
            self._status("жду плату в режиме загрузчика — дважды нажми Reset")
            self.log("жду плату в режиме загрузчика…", "warn")
        self._start_pulse()

    def start_flash(self, vol: Uf2Volume) -> None:
        if self.busy or self.info is None:
            return
        self.busy = True
        self._stop_pulse()
        self.watcher.pause()
        self.progress = 0.0
        self.pct_lbl.configure(text="0%")
        self._set_state("flash")
        self._status(f"пишу {fmt_size(self.info.size)} на {vol.title} · {vol.where}")
        self.log(f"плата: {vol.title} · {vol.where} · {vol.bootloader or 'bootloader ?'}", "ok")
        self.log(f"пишу {fmt_size(self.info.size)}…", "info")
        FlashJob(self.q, self.info, vol).start()

    def _start_pulse(self) -> None:
        self._stop_pulse()
        self._pulse_on = False

        def tick():
            if self.state != "armed":
                return
            self._pulse_on = not self._pulse_on
            self._redraw_zone()
            self._pulse_job = self.root.after(650, tick)

        self._pulse_job = self.root.after(650, tick)

    def _stop_pulse(self) -> None:
        if self._pulse_job is not None:
            try:
                self.root.after_cancel(self._pulse_job)
            except Exception:
                pass
            self._pulse_job = None

    # ------------------------------------------------------------- events
    def _pump(self) -> None:
        if self._closed:
            return
        try:
            while True:
                event = self.q.get_nowait()
                kind = event[0]
                if kind == "volumes":
                    self.on_volumes(event[1])
                elif kind == "progress":
                    self.on_progress(event[1], event[2])
                elif kind == "flash":
                    self.on_flash(event[1])
                elif kind == "status":
                    self._status(event[1])
                elif kind == "log":
                    self.log(event[1], event[2])
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def on_volumes(self, vols: list[Uf2Volume]) -> None:
        self.vols = vols
        if self.busy:
            return
        if self.state == "armed":
            if len(vols) == 1:
                self.start_flash(vols[0])
            elif len(vols) > 1:
                self._status("подключено несколько плат — оставь одну")
                self._redraw_zone()
            else:
                self._status("жду плату в режиме загрузчика — дважды нажми Reset")
                self._redraw_zone()
            return
        if self.state == "idle":
            self._refresh_idle()
            self._redraw_zone()

    def on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._set_progress(done / total)
        self._status(f"пишу {fmt_size(done)} из {fmt_size(total)}")

    def on_flash(self, res: FlashResult) -> None:
        self.busy = False
        self._stop_pulse()
        self.watcher.resume()
        if res.no_volume and self.info is not None:
            self.log("не успел поймать диск — жду ещё раз, нажми Reset", "warn")
            self.arm()
            return
        if res.ok:
            self.progress = 1.0
            if res.rebooted:
                self._set_state("done")
                self._status(f"записано за {res.elapsed:.1f} с · {fmt_size(res.written)}")
                self.log(f"готово за {res.elapsed:.1f} с — плата перезагрузилась", "ok")
            else:
                self._set_state("warn")
                self._status("файл записан, но плата не перезагрузилась — нажми Reset")
                self.log("файл записан, но том не исчез — плата не ушла в ребут", "warn")
        else:
            self.progress = max(0.0, res.written / res.total) if res.total else 0.0
            short = res.message
            if res.winerror:
                short = f"{short} (WinError {res.winerror})"
            self.last_error = short
            self._set_state("error")
            self._status(f"{short} · записано {res.percent}%")
            self.log(f"ошибка: {short}", "err")
            self.log(f"записано {fmt_size(res.written)} из {fmt_size(res.total)} "
                     f"({res.percent}%)", "dim")
            if res.winerror in (34, 1167, 21, 121):
                self.log("похоже, плата отключилась. Проверь кабель/порт, "
                         "дважды нажми Reset и брось файл ещё раз", "dim")
        self.pct_lbl.configure(text=f"{int(self.progress * 100)}%")
        self._draw_progress()

    # ------------------------------------------------------------- drag&drop
    def _install_drop_target(self) -> None:
        """Один target на клиентское окно Tk. В WndProc — только очередь."""
        self.root.update_idletasks()
        hwnd = self.root.winfo_id()
        self._drop_hwnd = hwnd
        self._drop_olds = {}
        self._drop_procs = []
        proc = WNDPROC(self._wndproc)
        old = user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, ctypes.cast(proc, ctypes.c_void_p))
        self._drop_olds[hwnd] = old
        self._drop_procs.append(proc)
        shell32.DragAcceptFiles(hwnd, True)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_DROPFILES:
                # Не трогаем Tcl и shell внутри оконной процедуры:
                # забираем HDROP позже, в обычном таймере Tk.
                self._drop_queue.append(int(wparam))
                return 0
            old = self._drop_olds.get(hwnd)
            if old:
                return user32.CallWindowProcW(old, hwnd, msg, wparam, lparam)
        except Exception:
            try:
                sys.stderr.write("wndproc error\n")
            except Exception:
                pass
        return 0

    def _poll_drops(self) -> None:
        if self._closed:
            return
        while self._drop_queue:
            hdrop = self._drop_queue.pop(0)
            if hdrop in self._drop_seen:
                continue
            self._drop_seen.add(hdrop)
            self.log(f"drop: WM_DROPFILES 0x{hdrop:X}", "dim")
            try:
                files = self._read_hdrop(hdrop)
            finally:
                shell32.DragFinish(hdrop)
            self.log(f"drop: путей {len(files)}", "dim")
            if files:
                try:
                    self.on_drop(files)
                except Exception:
                    self.log("drop: ошибка обработки — см. darkflasher-trace.log", "err")
                    import traceback
                    traceback.print_exc()
        self.root.after(50, self._poll_drops)

    def _read_hdrop(self, hdrop) -> list[str]:
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        files: list[str] = []
        for i in range(min(int(count), 64)):
            length = shell32.DragQueryFileW(hdrop, i, None, 0)
            if length <= 0 or length > 32768:
                continue
            buf = ctypes.create_unicode_buffer(length + 1)
            got = shell32.DragQueryFileW(hdrop, i, buf, length + 1)
            if got and buf.value:
                files.append(buf.value)
        return files

    def _on_zone_enter(self, _event) -> None:
        self._hover = True
        if self.state == "idle":
            self._redraw_zone()

    def _on_zone_leave(self, _event) -> None:
        self._hover = False
        if self.state == "idle":
            self._redraw_zone()

    # ------------------------------------------------------------- misc
    def _set_icon(self) -> None:
        try:
            self.root.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass

    def _dark_titlebar(self) -> None:
        try:
            hwnd = user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):
                if dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def on_close(self) -> None:
        if self.busy:
            if not self.messagebox.askyesno(
                    "Идёт запись",
                    "Прервать прошивку?\nПлата останется в режиме загрузчика.",
                    parent=self.root):
                return
        self._closed = True
        try:
            self.watcher.stop()
        except Exception:
            pass
        self.root.destroy()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

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
        if res.written >= info.size - UF2_BLOCK:
            _cli_print("жду перезагрузку…")
            res.rebooted = wait_volume_gone(vol.path, REBOOT_WAIT)
            res.ok = res.rebooted or res.written == info.size
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

def launch_gui(start_path: str | None = None) -> None:
    global S
    import tkinter as tk

    dpi = enable_dpi()
    S = dpi / 96.0
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    App(root, start_path)
    root.mainloop()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Dark Flasher работает только на Windows")
    ensure_std_streams()
    argv = sys.argv[1:]
    if any(a in ("--list", "--check", "--flash", "--version", "--help", "-h")
           for a in argv):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        raise SystemExit(cli(argv))
    start = argv[0] if argv and os.path.isfile(argv[0]) else None
    launch_gui(start)


if __name__ == "__main__":
    main()
