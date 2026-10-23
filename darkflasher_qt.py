"""Dark Flasher 1.4.1 — интерфейс на PyQt6.

Логика записи UF2, поиска тома и COM взята из DarkFlasher.py и darktec_cfg.py;
здесь только окно, стили и связи.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import queue
import random
import re
import sys
import threading
import time
import traceback

from PyQt6.QtCore import (Qt, QTimer, QVariantAnimation, QEasingCurve,
                          QEvent, QFileSystemWatcher, QLockFile)
from PyQt6.QtGui import QIcon, QColor, QPainter
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QScrollArea, QTextBrowser, QTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget, QCheckBox,
)

import darktec_cfg as cfg
from DarkFlasher import (
    APP_NAME, APP_VERSION, FlashJob, FlashResult, Uf2Error, Uf2Info,
    UF2_FAMILY_NRF52840, VolumeWatcher, apply_factory_cfg_to_uf2,
    build_dfu_zip_from_image, family_name, fmt_size, pack_factory_payload,
    patch_factory_cfg, uf2_to_image,
)

# PIN режима разработчика. В коде лежит только PBKDF2-хеш: PBKDF2-HMAC-SHA256,
# 120 000 итераций, соль ниже. Сам PIN нигде не хранится.
DEV_PIN_SALT = b"DarkFlasher::dev::pin::v1"
DEV_PIN_ITERATIONS = 120_000
DEV_PIN_HASH = "2e0066cb6715194d0feb9a2c1f5400c9d99e7727399bd0c903d02d3880053705"


def dev_pin_ok(pin: str) -> bool:
    got = hashlib.pbkdf2_hmac("sha256", (pin or "").encode("utf-8"),
                              DEV_PIN_SALT, DEV_PIN_ITERATIONS).hex()
    return hmac.compare_digest(got, DEV_PIN_HASH)


class Check(QCheckBox):
    """Квадратик с галочкой: без текста он читается лучше."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#062018"))
        p.drawText(self.rect().adjusted(0, 0, -max(0, self.width() - 20), 0),
                   Qt.AlignmentFlag.AlignCenter, "✓")
        p.end()


STYLE = """
QWidget {
    background: #0b0e14;
    color: #e6ebf2;
    font-family: "Bahnschrift", "Segoe UI";
    font-weight: 600;
    font-size: 13px;
}
QLabel#title { font-size: 34px; font-weight: 700; }
QLabel#subtitle { color: #5d6a7c; font-size: 11px; }
QLabel#section { color: #5d6a7c; font-size: 11px; font-weight: 700; }
QLabel#fieldLabel { color: #98a3b3; }
QLabel#hint { color: #5d6a7c; font-size: 11px; }
QLabel#fileName { font-size: 14px; font-weight: 600; }
QLabel#meta { color: #5d6a7c; font-size: 12px; }
QLabel#status { color: #98a3b3; }
QLineEdit, QComboBox {
    background: #161e2b;
    border: 1px solid #222c3c;
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: #2f7a5f;
}
QLineEdit:hover, QComboBox:hover { border-color: #33415a; }
QLineEdit:focus, QComboBox:focus { border-color: #4ea583; }
QLineEdit[invalid="true"] { border-color: #d96a6a; }
QLineEdit:disabled, QComboBox:disabled {
    background: #0f141d;
    border-color: #1c2533;
    color: #4f5b6b;
}
QComboBox { background: #1b2433; padding-right: 9px; }
QComboBox::drop-down { border: none; width: 0px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView {
    background: #161e2b;
    border: 1px solid #33415a;
    selection-background-color: #235c47;
    outline: none;
}
QPushButton {
    background: #161e2b;
    border: 1px solid #222c3c;
    border-radius: 7px;
    padding: 8px 14px;
}
QPushButton:hover { background: #1d2735; border-color: #33415a; }
QPushButton:pressed { background: #131b27; }
QPushButton#primary {
    background: #235c47;
    border-color: #2f7a5f;
    font-weight: 700;
}
QPushButton#primary:hover { background: #2a6f56; }
QPushButton#primary:pressed { background: #1d4c3b; }
QPushButton#ota {
    background: #d9863a;
    border-color: #e6a059;
    color: #1b1108;
    font-weight: 700;
}
QPushButton#ota:hover { background: #e6944a; }
QPushButton#ota:pressed { background: #b86f2c; }
QPushButton#ota:disabled { background: #6b4a26; color: #2a2018; }
QProgressBar {
    background: #161e2b;
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #2f7a5f, stop:1 #57c99a);
}
QTextEdit {
    background: #111722;
    border: 1px solid #222c3c;
    border-radius: 9px;
    font-family: "Consolas";
    font-size: 12px;
    color: #98a3b3;
    padding: 6px;
}
QCheckBox { color: #98a3b3; spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #33415a; border-radius: 4px; background: #161e2b;
}
QCheckBox::indicator:checked {
    background: #4ea583; border-color: #4ea583;
}
QCheckBox::indicator:disabled {
    background: #10151f; border-color: #222c3c;
}
QCheckBox::indicator:checked:disabled {
    background: #2a6f56; border-color: #2a6f56;
}
QFrame#dropZone {
    background: #0e141d;
    border: 1px dashed #2c3a4f;
    border-radius: 10px;
}
QFrame#dropZone:hover { border-color: #4ea583; background: #101a22; }
QFrame#badge {
    background: #161e2b; border: 1px solid #222c3c; border-radius: 11px;
}
QLabel#badgeText { color: #98a3b3; font-size: 11px; font-weight: 600; }
QLabel#orLabel {
    color: #33415a; font-size: 13px; font-weight: 700;
}
QFrame#badge QLabel, QFrame#dropZone QLabel { background: transparent; }
QLabel#link { background: transparent; font-size: 12px; }
QLabel#devBadge {
    background: #3a2a10;
    border: 1px solid #d9863a;
    color: #f0b35e;
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 10px;
    font-weight: 700;
}
QPushButton#devGear {
    background: transparent;
    border: none;
    color: #5d6a7c;
    font-size: 14px;
    padding: 0px;
}
QPushButton#devGear:hover { color: #d9863a; }
QPushButton#iconBtn {
    background: #161e2b;
    border: 1px solid #222c3c;
    border-radius: 7px;
    color: #98a3b3;
    font-size: 15px;
    padding: 0px;
}
QPushButton#iconBtn:hover { color: #4ea583; border-color: #4ea583; }
QPushButton#newsLink {
    background: transparent;
    border: none;
    color: #4ea583;
    padding: 1px 4px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton#newsLink:hover { color: #7ee0b4; }
QPushButton#newsLink:disabled { color: #33415a; }
QPushButton#newsLink[new="true"] {
    background: #3a2a10;
    border: 1px solid #d9863a;
    border-radius: 9px;
    color: #f0b35e;
    padding: 2px 9px;
}
QPushButton#newsLink[new="true"]:hover { background: #4a3514; }
QTextBrowser {
    background: #111722;
    border: 1px solid #222c3c;
    border-radius: 9px;
    padding: 8px;
}
QLabel#dialogTitle { font-size: 20px; font-weight: 700; }
QLabel#pinError { color: #d96a6a; font-size: 11px; }
QMenu {
    background: #161e2b;
    border: 1px solid #33415a;
    padding: 4px;
}
QMenu::item {
    padding: 6px 28px 6px 12px;
    border-radius: 4px;
}
QMenu::item:selected { background: #235c47; }
"""


# ------------------------------------------------------------ заметки релиза

def _md_inline(text: str) -> str:
    """Мини-Markdown для заметок GitHub: жирный, курсив, код, ссылки."""
    s = html.escape(text)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2">\1</a>', s)
    return s


def _md_to_html(text: str, source_url: str = "") -> str:
    """Переводит заметки релиза в аккуратный тёмный HTML."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    list_kind = ""

    def flush_para() -> None:
        if para:
            out.append("<p>" + _md_inline(" ".join(x.strip() for x in para))
                       + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            out.append(f"</{list_kind}>")
            list_kind = ""

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_para()
            close_list()
            out.append(f"<h3>{_md_inline(heading.group(2))}</h3>")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or numbered:
            flush_para()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                out.append(f"<{kind}>")
                list_kind = kind
            item = (bullet or numbered).group(1)
            out.append(f"<li>{_md_inline(item)}</li>")
            continue
        if list_kind and (line.startswith("  ") or line.startswith("\t")):
            # продолжение пункта: строки в заметках переносятся с отступом
            if out and out[-1].endswith("</li>"):
                out[-1] = out[-1][:-5] + " " + _md_inline(stripped) + "</li>"
            continue
        if list_kind:
            close_list()
        para.append(stripped)
    flush_para()
    close_list()

    footer = ""
    if source_url:
        footer = ('<p style="margin-top:16px"><a href="'
                  + html.escape(source_url, quote=True)
                  + '">Открыть релиз на GitHub →</a></p>')
    style = """
        body { color:#c9d4e2; font-family:'Segoe UI'; font-size:13px;
               line-height:150%; }
        h3 { color:#e6ebf2; font-size:14px; margin:16px 0 6px 0; }
        p { margin:8px 0; }
        ul, ol { margin:6px 0 10px 16px; padding:0; }
        li { margin:5px 0; }
        b { color:#f0f4f8; }
        code { background:#1b2433; color:#7ee0b4; font-family:Consolas;
               font-size:12px; }
        a { color:#4ea583; }
    """
    return ("<html><head><style>" + style + "</style></head><body>"
            + "\n".join(out) + footer + "</body></html>")


class DevPinDialog(QDialog):
    """Маленькое окно ввода PIN для режима разработчика."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Режим разработчика")
        self.setModal(True)
        self.setFixedWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        title = QLabel("Режим разработчика")
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        hint = QLabel("Введи PIN — включится список локальных сборок.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.pin = QLineEdit()
        self.pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin.setMaxLength(16)
        self.pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pin.setFixedHeight(36)
        self.pin.setPlaceholderText("PIN")
        self.pin.returnPressed.connect(self._check)
        lay.addWidget(self.pin)

        self.err = QLabel("")
        self.err.setObjectName("pinError")
        self.err.setFixedHeight(16)
        lay.addWidget(self.err)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("Войти")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self._check)
        row.addWidget(ok)
        lay.addLayout(row)
        QTimer.singleShot(50, self.pin.setFocus)

    def _check(self) -> None:
        if dev_pin_ok(self.pin.text().strip()):
            self.accept()
            return
        self.err.setText("неверный PIN")
        self.pin.selectAll()
        self.pin.setFocus()


class NewsDialog(QDialog):
    """Заметки о прошивке: список доступных версий и текст релиза."""

    def __init__(self, releases: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.releases = [r for r in releases if (r.get("body") or "").strip()]
        self.setWindowTitle("Что нового в прошивке")
        self.resize(620, 580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("Что нового")
        title.setObjectName("dialogTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.combo = QComboBox()
        self.combo.setFixedWidth(280)
        for r in self.releases:
            label = f"{r['tag']} · {r['date']}" if r.get("date") else r["tag"]
            self.combo.addItem(label)
        head.addWidget(self.combo)
        lay.addLayout(head)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self.browser, 1)

        bottom = QHBoxLayout()
        note = QLabel("заметки берутся из релиза на GitHub")
        note.setObjectName("hint")
        bottom.addWidget(note, 1)
        close = QPushButton("Закрыть")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        lay.addLayout(bottom)

        self.combo.currentIndexChanged.connect(self._show)
        if self.releases:
            self._show(0)

    def _show(self, index: int) -> None:
        if not (0 <= index < len(self.releases)):
            return
        rel = self.releases[index]
        self.browser.setHtml(_md_to_html(rel.get("body", ""),
                                         rel.get("html_url", "")))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.q: queue.Queue = queue.Queue()
        self.vols = []
        self.info: Uf2Info | None = None
        self.file_path: str | None = None
        self.firmware: dict[str, str] = {}
        self.busy = False
        self.settings = cfg.load_settings()
        self.role_slug = self.settings.get("role", "companion_radio_ble")
        self.github_releases: list[dict] = []
        self.dev_items: list[dict] = []
        self.releases: list[dict] = []
        self.selected_release: dict | None = None
        self.dev_mode = False
        self.new_tag = ""
        self._announced_tag = ""
        self.port = ""
        self._after_flash = False
        self._release_loading = False
        self._dev_signature: tuple = ()
        self._module_pref = bool(self.settings.get("use_module", True))
        self._lr2021 = False
        self.manual_source = False
        self._manual_flash = False
        self._flashing = False
        self._flash_waiting = False
        self._deferred_refresh = False
        self._pending_releases: list[dict] | None = None
        self._dev_dirty = False
        self._save_error = False
        self._closing = False
        self._badge_cache = None

        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self._save_settings)

        self.setWindowTitle("Dark Flasher — nRF52 UF2")
        self.setMinimumSize(660, 600)
        self.resize(700, 840)
        self.setAcceptDrops(True)
        icon = os.path.join(cfg.app_dir(), "app.ico")
        if not os.path.isfile(icon):
            icon = os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico")
        if os.path.isfile(icon):
            self.setWindowIcon(QIcon(icon))

        self._build()
        self._install_glow()
        self._log_banner()
        self._apply_lr2021_state()
        self._fit_to_content()

        self.watcher = VolumeWatcher(self.q)
        self.watcher.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(70)
        self.badge_timer = QTimer(self)
        self.badge_timer.timeout.connect(self._update_badge)
        self.badge_timer.start(2000)

        # Автообновление списка прошивок: не чаще ~2 раз в час, с разбросом,
        # чтобы не долбить GitHub API и не ловить лимит.
        self.release_timer = QTimer(self)
        self.release_timer.setSingleShot(True)
        self.release_timer.timeout.connect(self._auto_release_scan)

        # Локальные dev-сборки не опрашиваем по таймеру: Qt сам сообщит,
        # когда в папке что-то изменилось. SSD за это спасибо скажет.
        self.dev_refresh_timer = QTimer(self)
        self.dev_refresh_timer.setSingleShot(True)
        self.dev_refresh_timer.setInterval(800)
        self.dev_refresh_timer.timeout.connect(self._refresh_dev_items)
        self.dev_watcher = QFileSystemWatcher(self)
        self.dev_watcher.directoryChanged.connect(self._on_dev_dir_changed)

        self.rescan_firmware()
        self._load_releases()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(9)

        # header
        head = QHBoxLayout()
        head.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel(APP_NAME + " 👾")
        t1.setObjectName("title")
        titles.addWidget(t1)

        subrow = QHBoxLayout()
        subrow.setSpacing(8)
        sub = QLabel(f"nRF52 · UF2 · v{APP_VERSION}")
        sub.setObjectName("subtitle")
        subrow.addWidget(sub)

        self.dev_badge = QLabel("РЕЖИМ РАЗРАБОТЧИКА")
        self.dev_badge.setObjectName("devBadge")
        self.dev_badge.setToolTip("Локальные сборки включены. Нажми, чтобы выйти.")
        self.dev_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dev_badge.mousePressEvent = lambda _e: self.exit_dev_mode()
        self.dev_badge.setVisible(False)
        subrow.addWidget(self.dev_badge)

        self.dev_btn = QPushButton("⚙")
        self.dev_btn.setObjectName("devGear")
        self.dev_btn.setFixedSize(20, 20)
        self.dev_btn.setToolTip("Режим разработчика")
        self.dev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dev_btn.clicked.connect(self.enter_dev_mode)
        # Кнопка видна только там, где есть папка со сборками, — то есть у меня.
        self.dev_btn.setVisible(
            os.path.isdir(cfg.dev_firmware_dir(self.settings)))
        subrow.addWidget(self.dev_btn)
        subrow.addStretch(1)
        titles.addLayout(subrow)

        head.addLayout(titles)
        head.addStretch(1)

        self.badge = QFrame()
        self.badge.setObjectName("badge")
        self.badge.setFixedHeight(26)
        self.badge.setMinimumWidth(170)
        bl = QHBoxLayout(self.badge)
        bl.setContentsMargins(12, 0, 12, 0)
        bl.setSpacing(7)
        self.badge_dot = QLabel()
        self.badge_dot.setFixedSize(9, 9)
        bl.addWidget(self.badge_dot)
        self.badge_lbl = QLabel("НЕТ ПЛАТЫ")
        self.badge_lbl.setObjectName("badgeText")
        bl.addWidget(self.badge_lbl)
        # Клик должен доходить до рамки, а не тонуть в дочерних QLabel.
        for child in (self.badge_dot, self.badge_lbl):
            child.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.badge.mousePressEvent = lambda _e: self.choose_port()
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(head)
        self._set_badge("off")

        # роль + версия слева, зона drag&drop справа
        top = QHBoxLayout()
        top.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(6)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        role_title = QLabel("РОЛЬ")
        role_title.setObjectName("section")
        role_title.setAlignment(Qt.AlignmentFlag.AlignLeft |
                                Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(role_title, 0, 0)
        self.role_box = QComboBox()
        for _slug, title, _env in cfg.ROLES:
            self.role_box.addItem(title)
        self.role_box.setCurrentText(cfg.role_title(self.role_slug))
        self.role_box.currentTextChanged.connect(self.on_role_change)
        self.role_box.setFixedWidth(220)
        grid.addWidget(self.role_box, 0, 1)

        v_title = QLabel("ВЕРСИЯ")
        v_title.setObjectName("section")
        v_title.setAlignment(Qt.AlignmentFlag.AlignLeft |
                             Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(v_title, 1, 0)
        self.version_box = QComboBox()
        self.version_box.addItem("загружаю список…")
        self.version_box.setFixedWidth(220)
        self.version_box.setEnabled(False)
        self.version_box.currentIndexChanged.connect(self._on_version_change)
        grid.addWidget(self.version_box, 1, 1)

        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setObjectName("iconBtn")
        self.refresh_btn.setFixedSize(30, 30)
        self.refresh_btn.setToolTip("Обновить список версий (авто — раз в ~30 минут)")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._refresh_releases)
        grid.addWidget(self.refresh_btn, 1, 2,
                       Qt.AlignmentFlag.AlignVCenter)
        left.addLayout(grid)
        top.addLayout(left, 0)

        or_lbl = QLabel("ИЛИ")
        or_lbl.setObjectName("orLabel")
        or_lbl.setFixedWidth(40)
        or_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(or_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        self.drop = QFrame()
        self.drop.setObjectName("dropZone")
        self.drop.setMinimumHeight(84)
        self.drop.setMinimumWidth(200)
        self.drop.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Fixed)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(8, 8, 8, 8)
        arrow = QLabel("⭳")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setStyleSheet("color:#4ea583; font-size:22px;")
        dl.addWidget(arrow)
        dz = QLabel("перетащи .uf2 сюда или нажми, чтобы выбрать")
        dz.setObjectName("hint")
        dz.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.setWordWrap(True)
        dz.setSizePolicy(QSizePolicy.Policy.Ignored,
                         QSizePolicy.Policy.Preferred)
        dl.addWidget(dz)
        self.drop.mousePressEvent = lambda _e: self.choose_file()  # type: ignore
        top.addWidget(self.drop, 1)

        root.addLayout(top)

        # file info — одна строка: файл, размер и «что нового»
        fi = QHBoxLayout()
        fi.setSpacing(10)
        self.file_lbl = QLabel("файл не выбран")
        self.file_lbl.setObjectName("fileName")
        self.file_lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                                    QSizePolicy.Policy.Preferred)
        self.meta_lbl = QLabel("")
        self.meta_lbl.setObjectName("meta")
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        fi.addWidget(self.file_lbl, 1)
        fi.addWidget(self.meta_lbl)

        self.news_btn = QPushButton("ⓘ что нового")
        self.news_btn.setObjectName("newsLink")
        self.news_btn.setToolTip("Что изменилось в выбранной версии прошивки")
        self.news_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.news_btn.clicked.connect(self.show_news)
        self.news_btn.setEnabled(False)
        fi.addWidget(self.news_btn, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(fi)

        # progress
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        self.anim = QVariantAnimation(self)
        self.anim.setDuration(180)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.valueChanged.connect(lambda v: self.bar.setValue(int(v)))

        st = QHBoxLayout()
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("status")
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        self.status_lbl.setToolTip("")
        self.pct_lbl = QLabel("")
        self.pct_lbl.setObjectName("status")
        st.addWidget(self.status_lbl, 1)
        st.addWidget(self.pct_lbl)
        root.addLayout(st)

        # settings
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#222c3c;")
        root.addWidget(sep)
        secrow = QHBoxLayout()
        secrow.setSpacing(8)
        sec = QLabel("НАСТРОЙКИ НОДЫ")
        sec.setObjectName("section")
        secrow.addWidget(sec)
        secrow.addStretch(1)
        legend = QLabel("галочка — передавать на ноду")
        legend.setObjectName("hint")
        secrow.addWidget(legend)
        root.addLayout(secrow)

        forms = QHBoxLayout()
        forms.setSpacing(20)
        self.fields: dict[str, QLineEdit] = {}
        self.combos: dict[str, QComboBox] = {}
        self.use_cbs: dict[str, QCheckBox] = {}
        self.radio_widgets: list[QWidget] = []
        BW = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125,
              203.125, 250, 406.25, 500]
        left = QFormLayout()
        right = QFormLayout()
        for form in (left, right):
            form.setHorizontalSpacing(6)
            form.setVerticalSpacing(8)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight |
                                   Qt.AlignmentFlag.AlignVCenter)
            form.setFormAlignment(Qt.AlignmentFlag.AlignLeft |
                                  Qt.AlignmentFlag.AlignTop)

        check_tips = {
            "use_name": "Передавать имя ноды",
            "use_radio": "Передавать частоту, полосу, SF и CR",
            "use_tx": "Передавать мощность передачи",
            "use_module": "Передавать радиомодуль",
            "use_capacity": "Передавать ёмкость батареи",
            "use_adc": "Передавать коэффициент АЦП по USB "
                       "(в UF2/OTA не вшивается)",
        }

        def field_row(widget, check_key):
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)
            hl.addWidget(widget)
            cb = Check()
            cb.setFixedSize(20, 20)
            cb.setToolTip(check_tips.get(check_key, "Передавать на ноду"))
            cb.setAccessibleName(check_tips.get(check_key, "Передавать на ноду"))
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.setChecked(bool(self.settings.get(check_key, True)))
            cb.toggled.connect(lambda on, w=widget: w.setEnabled(on))
            cb.toggled.connect(
                lambda on, k=check_key: self._on_use_toggle(k, on))
            hl.addWidget(cb)
            hl.addStretch(1)
            self.use_cbs[check_key] = cb
            widget.setEnabled(cb.isChecked())
            return row

        def add_line(form, label, key, hint="", check_key=None):
            lb = QLabel(label)
            lb.setObjectName("fieldLabel")
            ed = QLineEdit(str(self.settings.get(key, "")))
            if hint:
                ed.setPlaceholderText(hint)
            ed.setFixedWidth(150)
            ed.textChanged.connect(self._on_setting_changed)
            if check_key:
                form.addRow(lb, field_row(ed, check_key))
            else:
                form.addRow(lb, ed)
            self.fields[key] = ed

        def add_choice(form, label, key, values, fmt):
            lb = QLabel(label)
            lb.setObjectName("fieldLabel")
            cb = QComboBox()
            cb.setFixedWidth(150)
            for v in values:
                cb.addItem(fmt(v), v)
            cur = self.settings.get(key)
            try:
                cur_f = float(cur)
            except (TypeError, ValueError):
                cur_f = None
            idx = next((i for i, v in enumerate(values)
                        if cur_f is not None
                        and abs(float(v) - cur_f) < 1e-6), 0)
            cb.setCurrentIndex(idx)
            cb.currentIndexChanged.connect(
                lambda _i: self._on_setting_changed())
            form.addRow(lb, cb)
            self.combos[key] = cb

        add_line(left, "Имя ноды", "name", "не задано", "use_name")
        add_line(left, "Частота, МГц", "freq", "", "use_radio")
        add_choice(left, "Полоса, кГц", "bw", BW, lambda v: f"{v:g}")
        add_choice(left, "SF", "sf", list(range(5, 13)), lambda v: str(v))
        add_choice(left, "CR", "cr", list(range(5, 9)), lambda v: str(v))
        add_line(right, "Мощность, dBm", "tx", "-9..22", "use_tx")
        add_line(right, "Ёмкость, мАч", "capacity", "1000..10000",
                 "use_capacity")

        mod_lb = QLabel("Радиомодуль")
        mod_lb.setObjectName("fieldLabel")
        self.module_box = QComboBox()
        self.module_box.setFixedWidth(150)
        for _i, title in cfg.MODULES:
            self.module_box.addItem(title)
        cur = int(self.settings.get("module", 0))
        idx = next((i for i, (m, _t) in enumerate(cfg.MODULES) if m == cur), 0)
        self.module_box.setCurrentIndex(idx)
        self.module_box.currentTextChanged.connect(
            lambda _t: self._on_setting_changed())
        right.addRow(mod_lb, field_row(self.module_box, "use_module"))

        add_line(right, "Коэф. АЦП", "adc", "1.750", "use_adc")
        forms.addLayout(left, 1)
        forms.addLayout(right, 1)
        root.addLayout(forms)

        self.radio_widgets = [
            self.fields["freq"], self.combos["bw"], self.combos["sf"],
            self.combos["cr"],
        ]
        self._set_radio_enabled(self.use_cbs["use_radio"].isChecked())

        self.send_cb = Check("отправлять настройки после прошивки")
        self.send_cb.setChecked(bool(self.settings.get("send_after_flash", True)))
        self.send_cb.stateChanged.connect(self._on_send_toggle)
        root.addWidget(self.send_cb)

        # buttons
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_flash = QPushButton("ПРОШИТЬ")
        self.btn_flash.setObjectName("primary")
        self.btn_flash.setMinimumHeight(38)
        self.btn_flash.clicked.connect(self.flash_current)
        btns.addWidget(self.btn_flash, 1)
        self.btn_send = QPushButton("Отправить настройки")
        self.btn_send.setMinimumHeight(38)
        self.btn_send.clicked.connect(self.send_settings_now)
        btns.addWidget(self.btn_send, 1)
        root.addLayout(btns)

        btns2 = QHBoxLayout()
        btns2.setSpacing(8)
        self.btn_uf2 = QPushButton("Собрать UF2 с настройками")
        self.btn_uf2.setObjectName("ota")
        self.btn_uf2.setMinimumHeight(38)
        self.btn_uf2.clicked.connect(self.build_uf2)
        btns2.addWidget(self.btn_uf2, 1)
        self.btn_ota = QPushButton("Собрать OTA-ZIP с настройками")
        self.btn_ota.setObjectName("ota")
        self.btn_ota.setMinimumHeight(38)
        self.btn_ota.clicked.connect(self.build_ota)
        btns2.addWidget(self.btn_ota, 1)
        root.addLayout(btns2)

        # log
        log_sec = QLabel("ЛОГ")
        log_sec.setObjectName("section")
        root.addWidget(log_sec)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(170)
        root.addWidget(self.log, 1)

        links = QHBoxLayout()
        links.setSpacing(18)
        for text, url in (
            ("Группа в Telegram", "https://t.me/DarkElectronics"),
            ("Покупка нод DarkTec", "https://t.me/Dark_Electronics"),
            ("Прошивки beeline09", "https://beeline09.github.io/darktec/"),
            ("Документация на плату",
             "https://disk.yandex.ru/i/_Y-lxwDZrS1TRA"),
        ):
            lb = QLabel(f'<a href="{url}" style="color:#4ea583;'
                        f'text-decoration:none;">{text}</a>')
            lb.setObjectName("link")
            lb.setOpenExternalLinks(True)
            lb.setCursor(Qt.CursorShape.PointingHandCursor)
            links.addWidget(lb)
        links.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addLayout(links)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea{background:#0b0e14;border:none;}"
            "QScrollArea > QWidget > QWidget{background:#0b0e14;}")
        scroll.setWidget(central)
        self.setCentralWidget(scroll)

    def _fit_to_content(self) -> None:
        """Окно по высоте содержимого: при обычном размере скроллбара нет."""
        scroll = self.centralWidget()
        content = scroll.widget() if isinstance(scroll, QScrollArea) else scroll
        height = content.sizeHint().height() if content is not None else 840
        self.resize(700, max(600, height + 2))

    def _install_glow(self) -> None:
        """Неоновое свечение вокруг полей и кнопок при наведении."""
        widgets = (self.findChildren(QLineEdit) + self.findChildren(QComboBox)
                   + self.findChildren(QPushButton))
        skip = {"devGear", "iconBtn", "newsLink"}
        for w in widgets:
            if w.objectName() in skip:
                continue
            w.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            w.installEventFilter(self)
        for lb in self.findChildren(QLabel):
            if lb.objectName() == "link":
                lb.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
                lb.installEventFilter(self)

    def eventFilter(self, obj, event):
        is_link = (isinstance(obj, QLabel)
                   and obj.objectName() == "link")
        if isinstance(obj, (QLineEdit, QComboBox, QPushButton)) or is_link:
            if event.type() == QEvent.Type.Enter:
                # Оранжевым кнопкам — оранжевое свечение, остальным зелёное.
                if isinstance(obj, QPushButton) and obj.objectName() == "ota":
                    glow = QColor(230, 148, 74, 200)
                elif isinstance(obj, QPushButton) and obj.objectName() == "primary":
                    glow = QColor(78, 165, 131, 200)
                else:
                    glow = QColor(78, 165, 131, 160)
                eff = QGraphicsDropShadowEffect(obj)
                eff.setBlurRadius(18)
                eff.setColor(glow)
                eff.setOffset(0, 0)
                obj.setGraphicsEffect(eff)
            elif event.type() == QEvent.Type.Leave:
                obj.setGraphicsEffect(None)
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ лог
    def log_msg(self, text: str, kind: str = "info") -> None:
        low = text.lower()
        if kind == "info" and any(w in low for w in (
                "ошибка", "не удалось", "не появилась", "не найден",
                "не скачалось", "не отправлен", "недоступен", "отклонена")):
            kind = "err"
        color = {"ok": "#4ea583", "warn": "#d9a054", "err": "#d96a6a",
                 "dim": "#5d6a7c", "file": "#d9863a"}.get(kind, "#98a3b3")
        t = time.strftime("%H:%M:%S")
        self.log.append(
            f'<span style="color:#5d6a7c">{t}</span> '
            f'<span style="color:{color}">{self._esc(text)}</span>')

    @staticmethod
    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))

    def _log_banner(self) -> None:
        self.log_msg(f"Dark Flasher {APP_VERSION} · nRF52 UF2", "ok")
        self.log_msg("выбери роль, проверь настройки и нажми «Прошить»", "dim")

    # ------------------------------------------------------------- настройки
    def _collect(self) -> dict:
        s = dict(self.settings)
        for key, ed in self.fields.items():
            raw = ed.text().strip().replace(",", ".")
            if key == "name":
                s["name"] = ed.text().strip()
                continue
            if raw == "":
                continue
            try:
                s[key] = float(raw) if key in ("freq", "bw", "adc") else int(raw)
            except ValueError:
                pass
        for key, cb in self.combos.items():
            val = cb.currentData()
            if val is not None:
                s[key] = float(val) if key == "bw" else int(val)
        for key, cb in self.use_cbs.items():
            # У радиомодуля храним желание пользователя: при LR2021 галочка
            # временно снята, но настройку не теряем.
            s[key] = (bool(self._module_pref) if key == "use_module"
                      else bool(cb.isChecked()))
        s["module"] = cfg.MODULES[self.module_box.currentIndex()][0]
        s["send_after_flash"] = bool(self.send_cb.isChecked())
        return s

    def _on_setting_changed(self) -> None:
        self.settings = self._collect()
        self.save_timer.start()

    def _on_send_toggle(self) -> None:
        self.settings["send_after_flash"] = bool(self.send_cb.isChecked())
        self.save_timer.start()

    def _save_settings(self) -> None:
        if cfg.save_settings(self.settings):
            self._save_error = False
        elif not self._save_error:
            self._save_error = True
            self.log_msg(
                f"не удалось сохранить настройки в {cfg.config_path()} — "
                f"проверь права на папку", "warn")

    def _effective_settings(self) -> dict:
        """Настройки с учётом отключённых галочек и режима LR2021."""
        s = dict(self.settings)
        if self._lr2021:
            s["use_module"] = False
        return s

    def _mark_field(self, key: str, message: str) -> None:
        ed = self.fields.get(key)
        if ed is None:
            return
        ed.setProperty("invalid", bool(message))
        ed.setToolTip(message or "")
        style = ed.style()
        style.unpolish(ed)
        style.polish(ed)

    def _read_number(self, key: str, kind: str):
        ed = self.fields[key]
        raw = ed.text().strip().replace(",", ".")
        if not raw:
            return None, "пустое значение"
        try:
            return (float(raw) if kind == "float" else int(raw)), ""
        except ValueError:
            return None, "не число"

    def _validate_inputs(self) -> bool:
        for key in self.fields:
            self._mark_field(key, "")
        errors: list[str] = []
        use_name = self.use_cbs["use_name"].isChecked()
        use_radio = self.use_cbs["use_radio"].isChecked()
        use_tx = self.use_cbs["use_tx"].isChecked()
        use_capacity = self.use_cbs["use_capacity"].isChecked()
        use_adc = self.use_cbs["use_adc"].isChecked()

        if use_name:
            name = self.fields["name"].text().strip()
            if len(name.encode("utf-8")) > 31:
                self._mark_field("name", "имя длиннее 31 байта UTF-8")
                errors.append("Имя ноды: не длиннее 31 байта UTF-8")
        if use_radio:
            freq, err = self._read_number("freq", "float")
            if err or not (100.0 <= freq <= 6000.0):
                self._mark_field("freq", err or "100..6000 МГц")
                errors.append("Частота: 100..6000 МГц")
        if use_tx:
            tx, err = self._read_number("tx", "int")
            if err or not (-9 <= tx <= 22):
                self._mark_field("tx", err or "-9..22 дБм")
                errors.append("Мощность: -9..22 дБм")
        if use_capacity:
            cap, err = self._read_number("capacity", "int")
            if err or not (1000 <= cap <= 10000):
                self._mark_field("capacity", err or "1000..10000 мАч")
                errors.append("Ёмкость: 1000..10000 мАч")
        if use_adc:
            adc, err = self._read_number("adc", "float")
            if err or not (0.1 <= adc <= 10.0):
                self._mark_field("adc", err or "0.1..10")
                errors.append("Коэф. АЦП: 0.1..10")

        if errors:
            QMessageBox.warning(self, "Проверь настройки",
                                "\n".join(errors))
            return False
        return True

    def _is_lr2021(self) -> bool:
        rel = self.selected_release
        if rel:
            text = " ".join(str(rel.get(k) or "")
                            for k in ("name", "path", "tag")).lower()
            return "lr2021" in text
        return "lr2021" in os.path.basename(self.file_path or "").lower()

    def _set_radio_enabled(self, on: bool) -> None:
        for widget in self.radio_widgets:
            widget.setEnabled(on)

    def _apply_lr2021_state(self) -> None:
        lr = self._is_lr2021()
        changed = lr != self._lr2021
        self._lr2021 = lr
        cb = self.use_cbs.get("use_module")
        if cb is None:
            return
        cb.blockSignals(True)
        if lr:
            cb.setChecked(False)
            cb.setEnabled(False)
            cb.setToolTip("Для прошивки LR2021 радиомодуль не передаётся")
            self.module_box.setEnabled(False)
        else:
            cb.setEnabled(True)
            cb.setChecked(self._module_pref)
            cb.setToolTip("Передавать радиомодуль")
            self.module_box.setEnabled(self._module_pref)
        cb.blockSignals(False)
        if changed:
            if lr:
                self.log_msg("LR2021: радиомодуль не передаётся", "warn")
            else:
                self.log_msg("обычная прошивка: радиомодуль снова доступен",
                             "dim")

    def _on_use_toggle(self, key: str, on: bool) -> None:
        if key == "use_radio":
            self._set_radio_enabled(on)
        elif key == "use_module":
            if self._lr2021:
                return
            self._module_pref = bool(on)
            self.module_box.setEnabled(on)
        self._on_setting_changed()

    @staticmethod
    def _skipped_text(settings: dict) -> str:
        off = []
        if not settings.get("use_name", True):
            off.append("имя")
        if not settings.get("use_radio", True):
            off.append("радио")
        if not settings.get("use_tx", True):
            off.append("мощность")
        if not settings.get("use_module", True):
            off.append("радиомодуль")
        if not settings.get("use_capacity", True):
            off.append("ёмкость")
        if not settings.get("use_adc", True):
            off.append("АЦП")
        return ", ".join(off)

    def _log_skipped(self, settings: dict) -> None:
        off = self._skipped_text(settings)
        if off:
            self.log_msg("не передаю: " + off, "dim")

    # ------------------------------------------------------------------ роль
    def on_role_change(self, title: str) -> None:
        if self.busy:
            return
        for slug, t, _env in cfg.ROLES:
            if t == title:
                self.role_slug = slug
                self.settings["role"] = slug
                self.save_timer.start()
                break
        self.manual_source = False
        self.rescan_firmware()
        if self.dev_mode:
            self._refresh_dev_items()
        else:
            self._rebuild_version_list()

    def _env(self) -> str:
        return cfg.ROLE_ENVS.get(self.role_slug, "")

    def rescan_firmware(self) -> None:
        self.firmware = cfg.scan_firmware()
        env = self._env()
        path = self.firmware.get(env)
        if path:
            self._apply_file(path)
            return
        self.file_path = None
        self.info = None
        self._apply_lr2021_state()
        self.file_lbl.setText("файл не выбран")
        self.meta_lbl.setText(
            f"нет файла для роли «{cfg.role_title(self.role_slug)}» — "
            f"положи .uf2 в firmware")

    def _apply_file(self, path: str) -> None:
        self.file_path = path
        try:
            info = Uf2Info.load(path)
        except (OSError, Uf2Error) as e:
            self.log_msg(str(e), "err")
            self.info = None
            return
        self.info = info
        self._apply_lr2021_state()
        self.save_timer.start()
        self.file_lbl.setText(self._elide_file(os.path.basename(path)))
        self.file_lbl.setToolTip(path)
        self.meta_lbl.setText(f"{fmt_size(info.size)} · {info.num_blocks} бл.")

    def _elide_file(self, text: str) -> str:
        width = max(120, self.file_lbl.width() - 4)
        return self.file_lbl.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideMiddle, width)

    def _set_file_hint(self, text: str, meta: str) -> None:
        self.file_path = None
        self.info = None
        self.file_lbl.setText(text)
        self.file_lbl.setToolTip("")
        self.meta_lbl.setText(meta)

    def choose_file(self) -> None:
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбери прошивку .uf2", "",
            "UF2 firmware (*.uf2);;Все файлы (*)")
        if not path:
            return
        self._apply_file(path)
        if self.info is None:
            return
        self.manual_source = True
        self.selected_release = None
        self._rebuild_version_list()

    # ---------------------------------------------------------- список версий
    def _load_releases(self, silent: bool = False) -> None:
        if self._release_loading:
            if not silent:
                self.log_msg("список уже обновляется…", "dim")
            return
        self._release_loading = True

        def work() -> None:
            rel: list[dict] | None = None
            try:
                rel = cfg.list_firmware_releases()
            except Exception as e:  # noqa: BLE001
                if not silent:
                    if self.dev_mode:
                        self.q.put(("log", "GitHub недоступен — работают "
                                           "локальные dev-сборки", "warn"))
                    else:
                        self.q.put(("log", f"GitHub недоступен — список версий "
                                           f"не обновлён ({e})", "warn"))
            self.q.put(("releases", rel, silent))

        threading.Thread(target=work, daemon=True).start()

    def _arm_release_timer(self, seconds: int | None = None) -> None:
        # Раз в 25-35 минут — это ~2 запроса в час при лимите GitHub 60.
        if seconds is None:
            seconds = random.randint(25 * 60, 35 * 60)
        self.release_timer.start(seconds * 1000)

    def _auto_release_scan(self) -> None:
        if self.busy or self._release_loading:
            self._arm_release_timer(10 * 60)
            return
        self._load_releases(silent=True)

    def _refresh_releases(self) -> None:
        if self.busy:
            self.log_msg("идёт операция — обновлю список после неё", "warn")
            return
        self.log_msg("обновляю список версий…", "dim")
        self._load_releases()

    @staticmethod
    def _release_signature(releases: list[dict] | None) -> tuple:
        return tuple(
            (r.get("tag"), r.get("date"), (r.get("body") or ""),
             tuple(sorted((r.get("files") or {}).keys())))
            for r in (releases or []))

    @staticmethod
    def _dev_signature_of(items: list[dict]) -> tuple:
        return tuple((f.get("path"), f.get("mtime"), f.get("size"))
                     for f in items)

    def _refresh_dev_items(self) -> None:
        if self.busy:
            self._dev_dirty = True
            return
        try:
            items = cfg.scan_dev_firmware(
                self._env(), cfg.dev_firmware_dir(self.settings))
        except Exception as e:  # noqa: BLE001
            items = []
            self.log_msg(f"папка dev-сборок: {e}", "err")
        self.dev_items = items
        self._dev_signature = self._dev_signature_of(items)
        self._rebuild_version_list()

    def _on_dev_dir_changed(self, _path: str) -> None:
        # Папка может измениться пачкой файлов — подождём и просканируем один раз.
        if not self.dev_mode:
            return
        if self.busy:
            self._dev_dirty = True
            return
        self.dev_refresh_timer.start()

    def _populate_versions(self, releases: list[dict] | None,
                           silent: bool = False) -> None:
        self._release_loading = False
        if releases is None:
            # Сеть/API не ответили: старый список не трогаем, а к следующей
            # попытке подходим осторожно, чтобы не злить GitHub.
            self._arm_release_timer(45 * 60)
            if self.dev_mode:
                # В dev-режиме локальные сборки не зависят от GitHub.
                # Пересканируем папку и оставляем её список на экране.
                self._refresh_dev_items()
                return
            if not self.github_releases:
                self.version_box.blockSignals(True)
                self.version_box.clear()
                self.version_box.addItem("GitHub недоступен — нажми ⟳")
                self.version_box.setEnabled(False)
                self.version_box.blockSignals(False)
            return

        if self.busy:
            # Во время прошивки список не перестраиваем: иначе можно
            # подменить выбранный релиз прямо посреди операции.
            self._pending_releases = releases
            self._deferred_refresh = True
            self._arm_release_timer()
            return

        had_releases = bool(self.github_releases)
        release_changed = (self._release_signature(releases)
                           != self._release_signature(self.github_releases))
        self.github_releases = releases

        dev_changed = False
        if self.dev_mode:
            try:
                items = cfg.scan_dev_firmware(
                    self._env(), cfg.dev_firmware_dir(self.settings))
            except Exception:  # noqa: BLE001
                items = []
            signature = self._dev_signature_of(items)
            dev_changed = signature != self._dev_signature
            self.dev_items = items
            self._dev_signature = signature
        else:
            self.dev_items = []
            self._dev_signature = ()

        if release_changed:
            latest = self.github_releases[0] if self.github_releases else None
            seen = str(self.settings.get("seen_release") or "")
            self.new_tag = (latest["tag"]
                            if (latest and latest["tag"] != seen) else "")
            if self.new_tag and self.new_tag != self._announced_tag:
                self._announced_tag = self.new_tag
                self.log_msg(
                    f"вышла новая прошивка {self.new_tag} — "
                    f"нажми «что нового»", "warn")

        if release_changed or dev_changed or not had_releases:
            self._rebuild_version_list()
            if release_changed and not silent and had_releases:
                self.log_msg("список версий обновлён", "dim")
        elif not silent:
            self.log_msg("список версий актуален", "dim")

        self._arm_release_timer()

    def _rebuild_version_list(self) -> None:
        prev = self.selected_release.get("tag") if self.selected_release else ""
        items: list[dict] = []
        if self.manual_source and self.file_path and self.info:
            items.append({
                "tag": "__manual__",
                "name": "файл вручную",
                "date": "",
                "published": "",
                "body": "",
                "html_url": "",
                "files": {},
                "manual": True,
                "path": self.file_path,
                "size": self.info.size,
            })
            if prev not in ("__manual__",):
                prev = "__manual__"
        if self.dev_mode:
            items.extend(self._dev_entry(f) for f in self.dev_items)
        items.extend(self.github_releases)
        self.releases = items

        self.version_box.blockSignals(True)
        self.version_box.clear()
        if not items:
            self.version_box.addItem("нет релизов прошивки")
            self.version_box.setEnabled(False)
            self.version_box.blockSignals(False)
            self.selected_release = None
            self._update_news_button(None)
            return
        for r in items:
            self.version_box.addItem(self._release_label(r))
            self.version_box.setItemData(
                self.version_box.count() - 1,
                r.get("path") or r.get("html_url") or "",
                Qt.ItemDataRole.ToolTipRole)
        # Попап не должен резать длинные теги/даты: считаем ширину по тексту.
        labels = [self._release_label(r) for r in items]
        fm = self.version_box.fontMetrics()
        max_w = max((fm.horizontalAdvance(t) for t in labels), default=0)
        view = self.version_box.view()
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        view.setMinimumWidth(min(560, max(240, max_w + 28)))
        self.version_box.setEnabled(True)
        index = next((i for i, r in enumerate(items) if r.get("tag") == prev),
                     0)
        self.version_box.setCurrentIndex(index)
        self.version_box.blockSignals(False)
        self._on_version_change(index)

    @staticmethod
    def _dev_entry(f: dict) -> dict:
        return {
            "tag": "dev:" + f["name"],
            "name": f["name"],
            "build": f.get("build"),
            "date": f.get("date", ""),
            "published": "",
            "body": "",
            "html_url": "",
            "files": {},
            "local": True,
            "path": f["path"],
            "size": f.get("size", 0),
        }

    @staticmethod
    def _release_label(r: dict) -> str:
        if r.get("manual"):
            return "📄 файл вручную"
        if r.get("local"):
            parts = ["⚡ dev"]
            if r.get("build"):
                parts.append(f"d{r['build']}")
            stem = os.path.basename(r.get("path", ""))[:-4].lower()
            if "lr2021" in stem:
                parts.append("lr2021")
            if r.get("date"):
                parts.append(r["date"])
            return " · ".join(parts)
        return f"{r['tag']} · {r['date']}" if r.get("date") else r["tag"]

    def _on_version_change(self, index: int) -> None:
        rel = self.releases[index] if 0 <= index < len(self.releases) else None
        self.selected_release = rel
        self.version_box.setToolTip(self.version_box.currentText())
        if rel is None:
            self._apply_lr2021_state()
            self._update_news_button(None)
            return
        if rel.get("manual"):
            self.manual_source = True
            self._apply_lr2021_state()
            self._update_news_button(rel)
            self.log_msg(
                f"файл вручную: {os.path.basename(rel['path'])}", "warn")
            return
        self.manual_source = False
        if rel.get("local"):
            self._apply_lr2021_state()
            self._update_news_button(rel)
            self.log_msg(
                f"локальная сборка: {os.path.basename(rel['path'])}", "warn")
            self._apply_file(rel["path"])
            return
        self._apply_lr2021_state()
        self._update_news_button(rel)
        # Выбран GitHub-релиз: локальный файл из firmware сейчас не источник.
        self._set_file_hint("файл версии ещё не скачан",
                            "скачается при прошивке или сборке")
        self.log_msg(
            f"версия прошивки: {rel['tag']} · {rel.get('date', '')}", "dim")

    def _update_news_button(self, rel: dict | None) -> None:
        latest = self.github_releases[0] if self.github_releases else None
        new_rel = (latest if (self.new_tag and latest
                              and latest.get("tag") == self.new_tag) else None)
        has_new_notes = bool(new_rel and (new_rel.get("body") or "").strip())
        has_notes = bool(rel and not rel.get("local") and rel.get("body"))
        if has_new_notes:
            build = new_rel.get("build")
            text = f"● что нового в d{build}" if build else "● что нового"
            enabled = True
            is_new = True
        else:
            text = "ⓘ что нового"
            enabled = has_notes
            is_new = False
        self.news_btn.setText(text)
        self.news_btn.setEnabled(enabled)
        self.news_btn.setProperty("new", is_new)
        style = self.news_btn.style()
        style.unpolish(self.news_btn)
        style.polish(self.news_btn)
        self.news_btn.update()

    def show_news(self) -> None:
        releases = [r for r in self.github_releases
                    if (r.get("body") or "").strip()]
        if not releases:
            self.log_msg("в релизах пока нет заметок о изменениях", "dim")
            return
        index = 0
        cur = self.selected_release
        if self.new_tag:
            index = next((i for i, r in enumerate(releases)
                          if r.get("tag") == self.new_tag), 0)
        elif cur and not cur.get("local"):
            index = next((i for i, r in enumerate(releases)
                          if r.get("tag") == cur.get("tag")), 0)
        dlg = NewsDialog(releases, self)
        if 0 <= index < dlg.combo.count():
            dlg.combo.setCurrentIndex(index)
        dlg.exec()

        latest = self.github_releases[0] if self.github_releases else None
        if latest and self.settings.get("seen_release") != latest["tag"]:
            self.settings["seen_release"] = latest["tag"]
            cfg.save_settings(self.settings)
            self._announced_tag = latest["tag"]
            self.new_tag = ""
            self._update_news_button(self.selected_release)

    # -------------------------------------------------- режим разработчика
    def enter_dev_mode(self) -> None:
        if self.dev_mode:
            return
        dlg = DevPinDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        folder = cfg.dev_firmware_dir(self.settings)
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Режим разработчика",
                                f"Папка не найдена:\n{folder}")
            return
        self.dev_mode = True
        self.dev_badge.setVisible(True)
        self.dev_btn.setVisible(False)
        if folder not in self.dev_watcher.directories():
            self.dev_watcher.addPath(folder)
        self.log_msg(f"режим разработчика включён: {folder}", "warn")
        # В dev-режиме сразу показываем свежую локальную сборку.
        self.manual_source = False
        self.selected_release = None
        self._refresh_dev_items()

    def exit_dev_mode(self, _event=None) -> None:
        if not self.dev_mode:
            return
        self.dev_mode = False
        self.dev_badge.setVisible(False)
        self.dev_btn.setVisible(
            os.path.isdir(cfg.dev_firmware_dir(self.settings)))
        folder = cfg.dev_firmware_dir(self.settings)
        if folder in self.dev_watcher.directories():
            self.dev_watcher.removePath(folder)
        self.dev_items = []
        self._dev_signature = ()
        self.manual_source = False
        self.log_msg("режим разработчика выключен", "dim")
        self.rescan_firmware()
        self._rebuild_version_list()

    def flash_current(self) -> None:
        if self.busy:
            return
        self._manual_flash = False
        rel = self.selected_release
        if rel and rel.get("manual"):
            self._manual_flash = True
            self._apply_file(rel["path"])
            self._begin_flash()
            return
        if not self._validate_inputs():
            return
        if rel and rel.get("local"):
            self._apply_file(rel["path"])
            self._begin_flash()
            return
        env = self._env()
        info = rel["files"].get(env) if rel else None
        if rel is not None and not info:
            self.log_msg(
                f"в версии {rel.get('tag', '')} нет файла для роли "
                f"«{cfg.role_title(self.role_slug)}» — выбери другую версию",
                "err")
            return
        if info:
            local = cfg.scan_firmware().get(env)
            sig_ok = False
            if local and os.path.isfile(local) and info.get("sha256"):
                try:
                    sig_ok = cfg.file_sha256(local) == info["sha256"]
                except OSError:
                    sig_ok = False
            if not sig_ok:
                self._download_and_flash()
                return
            self._apply_file(local)
            self._begin_flash()
            return
        # Релиз не выбран — работаем с тем, что лежит в firmware.
        if not self.info or not self.file_path or not os.path.isfile(self.file_path):
            self.rescan_firmware()
        if not self.info or not self.file_path:
            self.log_msg("нет файла: выбери версию прошивки или положи .uf2 "
                         "в папку firmware", "err")
            return
        self._begin_flash()

    def _download_and_flash(self) -> None:
        env = self._env()
        rel = self.selected_release
        info = rel["files"].get(env) if rel else None
        if not info or not info.get("url"):
            self.log_msg("нет файла: выбери версию прошивки или положи .uf2 "
                         "в папку firmware", "err")
            return
        self._download_async(env, info, "flash")

    def _download_async(self, env: str, info: dict, purpose: str) -> None:
        self._set_busy(True)
        self.set_progress(0)
        self.set_status("скачивание прошивки…")
        rel = self.selected_release or {}
        self.log_msg(f"скачиваю {rel.get('tag', env)} для "
                     f"«{cfg.role_title(self.role_slug)}»", "info")

        def work() -> None:
            def prog(done: int, total: int, speed: float) -> None:
                self.q.put(("dl_progress", done, total, speed))

            path = None
            try:
                path = cfg.download_firmware(
                    env, info["url"], info.get("sha256", ""), progress=prog,
                    log=lambda m: self.q.put(("log", m, "dim")))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"ошибка скачивания: {e}", "err"))
            finally:
                self.q.put(("dl_done", path, purpose))

        threading.Thread(target=work, daemon=True).start()

    def _begin_flash(self) -> None:
        if not self.info:
            self.set_status("нет файла для прошивки — выбери версию или .uf2")
            return
        if self.info.family != UF2_FAMILY_NRF52840:
            self.set_status(f"это UF2 для {family_name(self.info.family)}, нужен nRF52840")
            self.log_msg(f"это UF2 для {family_name(self.info.family)}, "
                         f"а нужен nRF52840", "err")
            return
        self.log_msg(f"прошиваю: {cfg.role_title(self.role_slug)}", "ok")
        if len(self.vols) == 1:
            self._start_flash(self.vols[0])
        else:
            self._flash_waiting = True
            self._set_busy(True)
            self.set_status("жду плату в режиме загрузчика — быстро нажми RESET на плате ДВА раза")
            self.log_msg("жду плату в режиме загрузчика… (быстро нажми RESET два раза)", "warn")

    def _start_flash(self, vol) -> None:
        if self._flashing or not self.info:
            return
        self._flashing = True
        self._flash_waiting = False
        self._set_busy(True)
        self.watcher.pause()
        self.set_progress(0.15)
        self.set_status(f"пишу {fmt_size(self.info.size)} — НЕ ОТКЛЮЧАЙ плату")
        self.log_msg(f"пишу {fmt_size(self.info.size)} на {vol.title}", "info")
        FlashJob(self.q, self.info, vol).start()

    # ------------------------------------------------------- СБОРКА ФАЙЛОВ
    def build_uf2(self) -> None:
        """Собирает UF2 с вшитыми настройками."""
        self._begin_build("uf2")

    def build_ota(self) -> None:
        """Собирает OTA-пакет (DFU zip) с вшитыми настройками."""
        self._begin_build("ota")

    def _begin_build(self, kind: str) -> None:
        if self.busy:
            self.log_msg("идёт операция — дождись окончания", "warn")
            return
        if not self._validate_inputs():
            return
        rel = self.selected_release
        if rel and rel.get("manual"):
            self._apply_file(rel["path"])
            self._start_build(rel["path"], self._env(), kind)
            return
        if rel and rel.get("local"):
            self._apply_file(rel["path"])
            self._start_build(rel["path"], self._env(), kind)
            return
        env = self._env()
        info = rel["files"].get(env) if rel else None
        if rel is not None and not info:
            self.log_msg(
                f"в версии {rel.get('tag', '')} нет файла для роли "
                f"«{cfg.role_title(self.role_slug)}» — выбери другую версию",
                "err")
            return
        if info:
            local = cfg.scan_firmware().get(env)
            fresh = False
            if local and os.path.isfile(local) and info.get("sha256"):
                try:
                    fresh = cfg.file_sha256(local) == info["sha256"]
                except OSError:
                    fresh = False
            if fresh:
                self._apply_file(local)
                self._start_build(local, env, kind)
            else:
                self._download_and_build(kind, env, info)
            return
        if not self.file_path or not os.path.isfile(self.file_path):
            self.rescan_firmware()
        if not self.file_path or not os.path.isfile(self.file_path):
            self.log_msg("нет файла: выбери версию прошивки или положи .uf2 "
                         "в папку firmware", "err")
            return
        self._start_build(self.file_path, env, kind)

    def _download_and_build(self, kind: str, env: str, info: dict) -> None:
        if not info or not info.get("url"):
            self.log_msg("нет файла для этой роли", "err")
            return
        self._download_async(env, info, kind)

    def _start_build(self, uf2_path: str, env: str, kind: str) -> None:
        if self.busy:
            return
        try:
            info = Uf2Info.load(uf2_path)
        except (OSError, Uf2Error) as e:
            self.set_status("не могу собрать файл — смотри лог")
            self.log_msg(f"не могу собрать: {e}", "err")
            return
        if info.family != UF2_FAMILY_NRF52840:
            self.set_status(f"это не nRF52840: {family_name(info.family)}")
            self.log_msg(f"это UF2 для {family_name(info.family)}, "
                         f"а нужен nRF52840", "err")
            return
        self._set_busy(True)
        stamp = time.strftime("%Y-%m-%d_%H-%M")
        out_dir = os.path.join(os.path.dirname(os.path.abspath(uf2_path)),
                               "built")
        ext = "uf2" if kind == "uf2" else "zip"
        out = os.path.join(out_dir, f"{env}-{kind}-{stamp}.{ext}")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self._set_busy(False)
            self.set_status("не удалось создать папку для сборки")
            self.log_msg(f"папка сборки недоступна: {e}", "err")
            return
        self.settings = self._collect()
        self.save_timer.start()
        settings = self._effective_settings()

        self.set_progress(0.05)
        self.set_status(f"собираю {ext.upper()} с настройками…")
        self.log_msg(f"сборка {ext.upper()}: {os.path.basename(uf2_path)}", "ok")
        parts = []
        if settings.get("use_name", True) and settings.get("name"):
            parts.append(str(settings["name"]))
        if settings.get("use_radio", True):
            parts.append(f"{settings.get('freq')} МГц · "
                         f"{float(settings.get('bw', 0)):g} кГц · "
                         f"SF{settings.get('sf')} · CR{settings.get('cr')}")
        if settings.get("use_tx", True):
            parts.append(f"{settings.get('tx')} дБм")
        if settings.get("use_module", True):
            mod = int(settings.get("module", 0))
            mod_title = next((t for m, t in cfg.MODULES if m == mod), "")
            if mod_title:
                parts.append(mod_title)
        if settings.get("use_capacity", True):
            parts.append(f"{settings.get('capacity')} мАч")
        if settings.get("use_adc", True):
            parts.append(f"АЦП {settings.get('adc')}")
        if parts:
            self.log_msg("параметры: " + " · ".join(parts), "dim")
        if settings.get("use_radio", True) != settings.get("use_tx", True):
            self.log_msg(
                "в UF2/OTA радио и мощность вшиваются только вместе; "
                "при разных галочках в образ не попадут — раздельно по USB",
                "warn")
        self._log_skipped(settings)

        def work() -> None:
            try:
                if kind == "uf2":
                    self.q.put(("log", "вписываю настройки в образ UF2…", "dim"))
                    self.q.put(("ota_progress", 0.3))
                    stats = apply_factory_cfg_to_uf2(uf2_path, out, settings)
                    stats["kind"] = "uf2"
                else:
                    self.q.put(("log", "вписываю настройки в образ…", "dim"))
                    self.q.put(("ota_progress", 0.25))
                    image, base, blocks = uf2_to_image(uf2_path)
                    img = bytearray(image)
                    payload, _flags = pack_factory_payload(settings)
                    patch = patch_factory_cfg(img, payload)
                    self.q.put(("log", f"блок настроек: смещение 0x{patch['offset']:X} · "
                                       f"crc32 0x{patch['crc']:08X}", "dim"))
                    self.q.put(("ota_progress", 0.6))
                    stats = build_dfu_zip_from_image(
                        bytes(img), out, base=base, blocks=blocks,
                        progress=lambda p: self.q.put(("ota_progress", 0.6 + 0.35 * p)))
                    stats["kind"] = "zip"
                    stats["patch_offset"] = patch["offset"]
                self.q.put(("ota_done", (True, out, stats)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("ota_done", (False, str(e), {"kind": kind})))

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------------- COM
    def _wait_nrf_port(self, timeout: float = 15.0) -> str | None:
        proto = ("frames" if self.role_slug in ("companion_radio_ble",
                                                "companion_radio_usb")
                 else "text")
        # Выбранный вручную порт пробуем первым, но принимаем только после
        # реального ответа MeshCore: чужую nRF-плату (или зависший COM) не берём.
        return cfg.find_meshcore_port(proto, timeout, preferred=self.port)

    def _send_thread(self, port: str, prefix: float) -> None:
        after_flash = bool(self._after_flash)
        try:
            eff = self._effective_settings()
            skipped = self._skipped_text(eff)
            if skipped:
                self.q.put(("log", "не передаю: " + skipped, "dim"))
            commands = cfg.build_commands(eff)
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"ошибка настроек: {e}", "err"))
            self.q.put(("settings_done", False, after_flash))
            return
        if not commands:
            self.q.put(("log", "все настройки выключены — отправлять нечего",
                        "warn"))
            self.q.put(("settings_done", True, after_flash, True))
            return
        proto = ("frames" if self.role_slug in ("companion_radio_ble",
                                              "companion_radio_usb")
                 else "text")
        try:
            ver = cfg.read_version(port, protocol=proto)
            if ver:
                self.q.put(("log", f"нода: {ver}", "info"))
                if proto == "frames" and "1.18.0d" not in ver:
                    self.q.put(("log", "прошивка на ноде старая — часть команд "
                                       "неизвестна. Прошей darktec-v1.18.0d7",
                                "warn"))

            def progress(p: float) -> None:
                self.q.put(("settings_progress", prefix + (1.0 - prefix) * p))

            ok = cfg.send_commands_batch(
                port, commands, protocol=proto,
                progress=progress,
                log=lambda m: self.q.put(("log", m, "dim")))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"ошибка отправки настроек: {e}", "err"))
            ok = False
        self.q.put(("settings_done", ok, after_flash))

    def _start_settings_send(self) -> None:
        if not self.settings.get("send_after_flash", True):
            self._set_busy(False)
            self.set_progress(1.0)
            self.set_status("готово — плату можно отключать")
            self.log_msg(
                "ПРОШИВКА ЗАВЕРШЕНА — плату можно отключать и пользоваться",
                "ok")
            return
        self._after_flash = True
        self._set_busy(True)
        self.set_progress(0.74)
        self.set_status("ищу ноду — НЕ ОТКЛЮЧАЙ плату")
        self.log_msg("ищу ноду среди портов nRF (VID 239A/1915)…", "info")

        def work() -> None:
            port = self._wait_nrf_port(15.0)
            if not port:
                self.q.put(("log", "нода не появилась — настройки НЕ отправлены. "
                                   "Проверь кабель и нажми «Отправить настройки»", "err"))
                self.q.put(("settings_done", False, True))
                return
            self.q.put(("log", f"порт {port}: отправляю настройки пачкой", "ok"))
            self.q.put(("settings_progress", 0.78))
            self._send_thread(port, 0.78)

        threading.Thread(target=work, daemon=True).start()

    def send_settings_now(self) -> None:
        if self.busy or not self._validate_inputs():
            return
        self.settings = self._collect()
        self.save_timer.start()
        self._after_flash = False
        self._set_busy(True)
        self.set_progress(0.05)
        self.set_status("ищу ноду…")

        def work() -> None:
            port = self._wait_nrf_port(10.0)
            if not port:
                self.q.put(("log", "нода не ответила ни на одном порту nRF — "
                                   "подключи плату по USB", "err"))
                self.q.put(("settings_done", False, False))
                return
            self.q.put(("log", f"отправляю настройки на {port}", "info"))
            self._send_thread(port, 0.05)

        threading.Thread(target=work, daemon=True).start()

    def set_progress(self, value: float) -> None:
        target = max(0, min(1000, int(value * 1000)))
        self.anim.stop()
        self.anim.setStartValue(self.bar.value())
        self.anim.setEndValue(target)
        self.anim.start()
        self.pct_lbl.setText(f"{int(value * 100)}%")

    def set_status(self, text: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setToolTip(text)

    def _set_controls_enabled(self, enabled: bool) -> None:
        widgets = [
            self.role_box, self.refresh_btn, self.send_cb,
            self.btn_flash, self.btn_send, self.btn_uf2, self.btn_ota,
            self.module_box,
        ]
        widgets += list(self.fields.values())
        widgets += list(self.combos.values())
        widgets += list(self.use_cbs.values())
        for widget in widgets:
            widget.setEnabled(enabled)
        self.version_box.setEnabled(enabled and bool(self.releases))
        if not enabled:
            return
        self.fields["name"].setEnabled(
            self.use_cbs["use_name"].isChecked())
        self.fields["tx"].setEnabled(
            self.use_cbs["use_tx"].isChecked())
        self.fields["capacity"].setEnabled(
            self.use_cbs["use_capacity"].isChecked())
        self.fields["adc"].setEnabled(
            self.use_cbs["use_adc"].isChecked())
        self._set_radio_enabled(self.use_cbs["use_radio"].isChecked())
        self._apply_lr2021_state()

    def _set_busy(self, busy: bool, refresh: bool = True) -> None:
        self.busy = bool(busy)
        self._set_controls_enabled(not busy)
        if busy or not refresh:
            return
        pending = self._pending_releases
        self._pending_releases = None
        if pending is not None:
            self._deferred_refresh = False
            self._populate_versions(pending, silent=True)
        elif self._deferred_refresh:
            self._deferred_refresh = False
            self._populate_versions(self.github_releases, silent=True)
        if self._dev_dirty:
            self._dev_dirty = False
            self._refresh_dev_items()

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "volumes":
                    self.vols = ev[1]
                    self._update_badge()
                    if (len(self.vols) == 1 and self._flash_waiting
                            and not self._flashing):
                        self._start_flash(self.vols[0])
                elif kind == "progress":
                    done, total = ev[1], ev[2]
                    if total:
                        self.set_progress(0.15 + (done / total) * 0.55)
                        self.set_status(f"пишу {fmt_size(done)} из "
                                        f"{fmt_size(total)} — НЕ ОТКЛЮЧАЙ плату")
                elif kind == "dl_progress":
                    done, total, speed = ev[1], ev[2], ev[3]
                    frac = (done / total) if total else 0.5
                    self.set_progress(0.15 * frac)
                    mb = speed / (1024 * 1024)
                    self.set_status(f"скачивание: {frac * 100:.0f}% · "
                                    f"{mb:.1f} МБ/с")
                elif kind == "dl_done":
                    path = ev[1]
                    purpose = ev[2] if len(ev) > 2 else ""
                    self._set_busy(False, refresh=False)
                    if path and purpose in ("uf2", "ota"):
                        self._apply_file(path)
                        if self.info:
                            self.set_progress(0.1)
                            self._start_build(path, self._env(), purpose)
                            if not self.busy:
                                self.set_status("сборка не началась — смотри лог")
                                self._set_busy(False)
                        else:
                            self.set_status("скачан битый файл — смотри лог")
                            self._set_busy(False)
                    elif path:
                        self._apply_file(path)
                        self.set_progress(0.15)
                        if self.info:
                            self._begin_flash()
                            if not self.busy:
                                self._set_busy(False)
                        else:
                            self.set_status("скачан битый файл — смотри лог")
                            self._set_busy(False)
                    else:
                        self.set_progress(0)
                        self.set_status("скачивание не удалось — смотри лог")
                        self._set_busy(False)
                elif kind == "flash":
                    self._on_flash(ev[1])
                elif kind == "status":
                    self.set_status(ev[1])
                elif kind == "log":
                    self.log_msg(ev[1], ev[2])
                elif kind == "releases":
                    self._populate_versions(
                        ev[1], bool(ev[2]) if len(ev) > 2 else False)
                elif kind == "ota_progress":
                    self.set_progress(ev[1])
                elif kind == "ota_done":
                    ok, payload, stats = ev[1]
                    self._set_busy(False)
                    if ok and stats:
                        self.set_progress(1.0)
                        build_kind = stats.get("kind", "uf2")
                        word = "UF2" if build_kind == "uf2" else "OTA-пакет"
                        self.set_status(f"{word} готов: "
                                        f"{os.path.basename(payload)}")
                        crc = stats.get("crc", 0)
                        crc_text = (f"crc32 0x{crc:08X}" if build_kind == "uf2"
                                    else f"crc16 0x{crc:04X}")
                        self.log_msg(
                            f"образ {fmt_size(stats.get('image', 0))} · "
                            f"{stats.get('blocks', 0)} блоков · база "
                            f"0x{stats.get('base', 0):08X} · {crc_text}",
                            "dim")
                        self.log_msg(
                            f"{word} {fmt_size(stats.get('size', 0))} сохранён: "
                            f"{payload}", "file")
                    else:
                        self.set_progress(0)
                        word = "UF2" if (stats or {}).get("kind") == "uf2" else "OTA"
                        self.set_status(f"сборка {word} не удалась — смотри лог")
                        self.log_msg(f"сборка {word}: {payload}", "err")
                elif kind == "settings_progress":
                    self.set_progress(ev[1])
                elif kind == "settings_done":
                    ok = bool(ev[1])
                    after_flash = bool(ev[2]) if len(ev) > 2 else False
                    noop = bool(ev[3]) if len(ev) > 3 else False
                    if noop:
                        self.set_progress(1.0 if after_flash else 0)
                        if after_flash:
                            self.set_status("прошивка записана; настройки не выбраны")
                            self.log_msg(
                                "ПРОШИВКА ЗАВЕРШЕНА — настройки не выбраны", "warn")
                        else:
                            self.set_status("настройки не выбраны")
                    elif ok:
                        self.set_progress(1.0)
                        self.log_msg("настройки записаны в ноду", "ok")
                        if after_flash:
                            self.set_status(
                                "готово — плату можно отключать и пользоваться")
                            self.log_msg(
                                "ПРОШИВКА ЗАВЕРШЕНА — плату можно "
                                "отключать и пользоваться", "ok")
                        else:
                            self.set_status("готово: настройки отправлены")
                    elif after_flash:
                        self.set_progress(1.0)
                        self.set_status(
                            "прошивка записана, настройки — не полностью")
                        self.log_msg(
                            "ПРОШИВКА ЗАПИСАНА — плату можно отключать; "
                            "настройки проверь вручную", "warn")
                    else:
                        self.set_progress(0)
                        self.set_status("настройки не отправились — смотри лог")
                    self._after_flash = False
                    self._set_busy(False)
        except queue.Empty:
            pass
        except Exception as e:  # noqa: BLE001 — UI не должен умирать от одного события
            self.log_msg(f"внутренняя ошибка обработчика: {e}", "err")

    def _set_badge(self, state: str, port: str = "",
                   port_count: int = 0) -> None:
        styles = {
            "boot": ("BOOT · можно прошивать", "#4ea583",
                     "background:#14301f;border:1px solid #2f7a5f;"
                     "border-radius:13px;"),
            "com": (f"{port} · нода на связи" if port else "COM · нода на связи",
                    "#6cb6ff",
                    "background:#132133;border:1px solid #2f5f8a;"
                    "border-radius:13px;"),
            "off": ("НЕТ ПЛАТЫ", "#d96a6a",
                    "background:#2a1618;border:1px solid #6e2f35;"
                    "border-radius:13px;"),
        }
        text, color, frame = styles.get(state, styles["off"])
        if state == "com" and port_count > 1:
            text += " ▾"

        if state == "boot":
            tip = "Плата в загрузчике — можно прошивать"
            cursor = Qt.CursorShape.ArrowCursor
        elif state == "com":
            tip = f"nRF52 на {port}" if port else "nRF52 на связи"
            if port_count > 1:
                tip += " · нажми, чтобы выбрать порт"
                cursor = Qt.CursorShape.PointingHandCursor
            else:
                cursor = Qt.CursorShape.ArrowCursor
        else:
            tip = "nRF52 не найден"
            cursor = Qt.CursorShape.ArrowCursor

        signature = (text, color, frame, tip, cursor)
        if signature == self._badge_cache:
            return
        self._badge_cache = signature
        self.badge_lbl.setText(text)
        self.badge_dot.setStyleSheet(f"background:{color};border-radius:4px;")
        self.badge.setStyleSheet("QFrame#badge{" + frame + "}")
        self.badge.setToolTip(tip)
        self.badge.setCursor(cursor)

    def _update_badge(self) -> None:
        if self.vols:
            self._set_badge("boot")
            return
        try:
            ports = cfg.nrf_ports()
        except Exception:
            ports = []
        if not ports:
            self.port = ""
            self._set_badge("off")
            return
        if self.port not in ports:
            self.port = ports[0]
        self._set_badge("com", self.port, len(ports))

    def choose_port(self) -> None:
        """Меню портов: показываем только nRF52 и только если их больше одного."""
        if self.vols:
            return
        ports = cfg.nrf_ports()
        if len(ports) < 2:
            return
        menu = QMenu(self)
        for port in ports:
            act = menu.addAction(port)
            act.setCheckable(True)
            act.setChecked(port == self.port)
            act.triggered.connect(
                lambda _checked=False, p=port: self.select_port(p))
        try:
            menu.exec(self.badge.mapToGlobal(self.badge.rect().bottomLeft()))
        finally:
            menu.deleteLater()

    def select_port(self, port: str) -> None:
        ports = cfg.nrf_ports()
        if port not in ports:
            self.log_msg(f"порт {port} уже недоступен", "warn")
            self._update_badge()
            return
        self.port = port
        self._update_badge()
        self.log_msg(f"выбран порт {port}", "info")

    def _on_flash(self, res: FlashResult) -> None:
        self._flashing = False
        self.watcher.resume()
        manual = self._manual_flash
        self._manual_flash = False
        if res.no_volume:
            self._manual_flash = manual
            self._flash_waiting = True
            self._set_busy(True)
            self.log_msg("не успел поймать диск — жду ещё раз, нажми Reset", "warn")
            self.set_status("жду плату в режиме загрузчика — быстро нажми RESET на плате ДВА раза")
            return
        if res.ok:
            self.set_progress(0.70)
            if res.rebooted:
                self.set_status(f"записано за {res.elapsed:.1f} с")
                self.log_msg(f"готово за {res.elapsed:.1f} с — плата "
                             f"перезагрузилась", "ok")
            else:
                self.set_status("записано, но плата не перезагрузилась — нажми Reset")
                self.log_msg("файл записан, но том не исчез", "warn")
            if not manual:
                self._start_settings_send()
            else:
                self._set_busy(False)
                self.set_progress(1.0)
                self.set_status("готово — плату можно отключать "
                                "(настройки не отправлялись)")
                self.log_msg("настройки не отправлялись: файл выбран вручную",
                             "warn")
                self.log_msg(
                    "ПРОШИВКА ЗАВЕРШЕНА — плату можно отключать и пользоваться",
                    "ok")
        else:
            self._flash_waiting = False
            self._set_busy(False)
            self.set_progress(0)
            msg = res.message
            self.set_status(f"{msg} · записано {res.percent}%")
            self.log_msg(f"ошибка: {msg}", "err")

    # ---------------------------------------------------------------- drag&drop
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".uf2") and os.path.isfile(path):
                self._drop_file(path)
                e.acceptProposedAction()
                return

    def _drop_file(self, path: str) -> None:
        if self.busy:
            self.log_msg("идёт запись — дождись окончания", "warn")
            return
        self._apply_file(path)
        if not self.info:
            return
        # Файл выбран вручную: шьём именно его, без сверки с релизом,
        # и не отправляем настройки — прошивка может быть чужая.
        self._manual_flash = True
        self.manual_source = True
        self.selected_release = None
        self._rebuild_version_list()
        self.log_msg(f"файл: {os.path.basename(path)} — шью как есть", "info")
        self._begin_flash()

    def closeEvent(self, e) -> None:
        if self.busy:
            answer = QMessageBox.question(
                self, "Идёт операция",
                "Прошивка/сборка/отправка ещё не закончена.\n"
                "Прервать и закрыть программу?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self._closing = True
        self.timer.stop()
        self.release_timer.stop()
        self.dev_refresh_timer.stop()
        self.badge_timer.stop()
        self.save_timer.stop()
        self._save_settings()
        watched = self.dev_watcher.directories()
        if watched:
            self.dev_watcher.removePaths(watched)
        self.watcher.stop()
        super().closeEvent(e)


def run_gui() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    lock = QLockFile(cfg.lock_path())
    lock.setStaleLockTime(5000)
    if not lock.tryLock(100):
        QMessageBox.warning(None, APP_NAME,
                            "Dark Flasher уже запущен — закрой другое окно.")
        return

    def excepthook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(os.path.join(cfg.user_data_dir(), "darkflasher-error.log"),
                      "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + text + "\n")
        except OSError:
            pass

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    try:
        code = app.exec()
    finally:
        lock.unlock()
    sys.exit(code)


def main() -> None:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"Dark Flasher {APP_VERSION}")
        return
    run_gui()


if __name__ == "__main__":
    main()
