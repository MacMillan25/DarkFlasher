"""Dark Flasher 1.1 — интерфейс на PyQt6.

Логика записи UF2, поиска тома и COM взята из DarkFlasher.py и darktec_cfg.py;
здесь только окно, стили и связи.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

from PyQt6.QtCore import Qt, QTimer, QVariantAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QPen, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QCheckBox,
)

import darktec_cfg as cfg
from DarkFlasher import (
    APP_NAME, APP_VERSION, FlashJob, FlashResult, Uf2Error, Uf2Info,
    UF2_FAMILY_NRF52840, VolumeWatcher, family_name, fmt_size,
    scan_uf2_volumes,
)

STYLE = """
QWidget {
    background: #0b0e14;
    color: #e6ebf2;
    font-family: "Segoe UI";
    font-size: 13px;
}
QLabel#title { font-size: 19px; font-weight: 700; }
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
QComboBox::drop-down { border: none; width: 22px; }
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
QFrame#badge QLabel, QFrame#dropZone QLabel { background: transparent; }
"""


def elide_text(text: str, width: int, bold: bool = False) -> str:
    f = QFont("Segoe UI", 10)
    f.setBold(bold)
    return QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideMiddle, width)


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
        self._progress = 0

        self.setWindowTitle("Dark Flasher — nRF52 UF2")
        self.setMinimumSize(820, 1040)
        self.resize(880, 1090)
        self.setAcceptDrops(True)
        icon = os.path.join(cfg.app_dir(), "app.ico")
        if os.path.isfile(icon):
            self.setWindowIcon(QIcon(icon))

        self._build()
        self._log_banner()

        self.watcher = VolumeWatcher(self.q)
        self.watcher.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(70)
        self.releases: list[dict] = []
        self.selected_release: dict | None = None
        self.rescan_firmware()
        self._load_releases()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        # header
        head = QHBoxLayout()
        head.setSpacing(12)
        logo = QLabel()
        icon_path = os.path.join(cfg.app_dir(), "app.ico")
        if os.path.isfile(icon_path):
            logo.setPixmap(QIcon(icon_path).pixmap(36, 36))
        else:
            logo.setPixmap(self._logo_pixmap(36))
        logo.setFixedSize(36, 36)
        head.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel(APP_NAME + " 👾")
        t1.setObjectName("title")
        sub = QLabel(f"nRF52 · UF2 · v{APP_VERSION}")
        sub.setObjectName("subtitle")
        titles.addWidget(t1)
        titles.addWidget(sub)
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
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(head)
        self._set_badge(False)

        # role row
        row = QHBoxLayout()
        row.setSpacing(8)
        role_title = QLabel("РОЛЬ")
        role_title.setObjectName("section")
        row.addWidget(role_title)
        self.role_box = QComboBox()
        for _slug, title, _env in cfg.ROLES:
            self.role_box.addItem(title)
        self.role_box.setCurrentText(cfg.role_title(self.role_slug))
        self.role_box.currentTextChanged.connect(self.on_role_change)
        self.role_box.setMinimumWidth(240)
        row.addWidget(self.role_box, 2)

        self.btn_online = QPushButton("Скачать с GitHub")
        self.btn_online.clicked.connect(self.download_online)
        row.addWidget(self.btn_online, 1)
        btn_rescan = QPushButton("Обновить")
        btn_rescan.clicked.connect(self.rescan_firmware)
        row.addWidget(btn_rescan)
        root.addLayout(row)

        # версия прошивки
        vrow = QHBoxLayout()
        vrow.setSpacing(8)
        v_title = QLabel("ВЕРСИЯ")
        v_title.setObjectName("section")
        vrow.addWidget(v_title)
        self.version_box = QComboBox()
        self.version_box.addItem("загружаю список…")
        self.version_box.setMinimumWidth(240)
        self.version_box.setEnabled(False)
        self.version_box.currentIndexChanged.connect(self._on_version_change)
        vrow.addWidget(self.version_box, 2)
        vrow.addStretch(1)
        root.addLayout(vrow)

        self.fw_lbl = QLabel("")
        self.fw_lbl.setObjectName("hint")
        self.fw_lbl.setWordWrap(False)
        root.addWidget(self.fw_lbl)

        # drop zone
        self.drop = QFrame()
        self.drop.setObjectName("dropZone")
        self.drop.setMinimumHeight(92)
        self.drop.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Fixed)
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(10, 10, 10, 10)
        arrow = QLabel("⭳")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setStyleSheet("color:#4ea583; font-size:26px;")
        dl.addWidget(arrow)
        dz = QLabel("перетащи .uf2 сюда или нажми, чтобы выбрать")
        dz.setObjectName("hint")
        dz.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dl.addWidget(dz)
        self.drop.mousePressEvent = lambda _e: self.choose_file()  # type: ignore
        root.addWidget(self.drop)

        # file info
        fi = QHBoxLayout()
        self.file_lbl = QLabel("файл не выбран")
        self.file_lbl.setObjectName("fileName")
        self.meta_lbl = QLabel("")
        self.meta_lbl.setObjectName("meta")
        self.meta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        fi.addWidget(self.file_lbl, 1)
        fi.addWidget(self.meta_lbl)
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
        sec = QLabel("НАСТРОЙКИ НОДЫ")
        sec.setObjectName("section")
        root.addWidget(sec)

        forms = QHBoxLayout()
        forms.setSpacing(26)
        self.fields: dict[str, QLineEdit] = {}
        left = QFormLayout()
        right = QFormLayout()
        for f in (left, right):
            f.setHorizontalSpacing(10)
            f.setVerticalSpacing(9)
            f.setLabelAlignment(Qt.AlignmentFlag.AlignRight |
                                Qt.AlignmentFlag.AlignVCenter)

        def add_field(form: QFormLayout, label: str, key: str,
                      hint: str = "") -> None:
            lb = QLabel(label)
            lb.setObjectName("fieldLabel")
            ed = QLineEdit(str(self.settings.get(key, "")))
            if hint:
                ed.setPlaceholderText(hint)
            ed.setFixedWidth(210)
            ed.textChanged.connect(self._on_setting_changed)
            form.addRow(lb, ed)
            self.fields[key] = ed

        add_field(left, "Имя ноды", "name", "не задано")
        add_field(left, "Частота, МГц", "freq")
        add_field(left, "Полоса, кГц", "bw")
        add_field(left, "SF", "sf", "5..12")
        add_field(left, "CR", "cr", "5..8")
        add_field(right, "Мощность, dBm", "tx", "-9..22")
        add_field(right, "Ёмкость, мАч", "capacity", "1000..10000")

        mod_lb = QLabel("Модуль")
        mod_lb.setObjectName("fieldLabel")
        self.module_box = QComboBox()
        for _i, title in cfg.MODULES:
            self.module_box.addItem(title)
        cur = int(self.settings.get("module", 0))
        idx = next((i for i, (m, _t) in enumerate(cfg.MODULES) if m == cur), 0)
        self.module_box.setCurrentIndex(idx)
        self.module_box.currentTextChanged.connect(
            lambda _t: self._on_setting_changed())
        self.module_box.setFixedWidth(210)
        right.addRow(mod_lb, self.module_box)

        add_field(right, "Коэф. АЦП", "adc", "1.750")
        forms.addLayout(left, 1)
        forms.addLayout(right, 1)
        root.addLayout(forms)

        self.send_cb = QCheckBox("отправлять настройки после прошивки")
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

        # log
        log_sec = QLabel("ЛОГ")
        log_sec.setObjectName("section")
        root.addWidget(log_sec)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(260)
        root.addWidget(self.log, 1)

    def _logo_pixmap(self, size: int) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#235c47"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, size, size, 10, 10)
        pen = QPen(QColor("#57c99a"), max(2, size // 14))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        m = size * 0.28
        p.drawRoundedRect(int(m), int(m), int(size - 2 * m), int(size - 2 * m),
                          3, 3)
        p.end()
        return pm

    # ------------------------------------------------------------------ лог
    def log_msg(self, text: str, kind: str = "info") -> None:
        low = text.lower()
        if kind == "info" and any(w in low for w in (
                "ошибка", "не удалось", "не появилась", "не найден",
                "не скачалось", "не отправлен", "недоступен", "отклонена")):
            kind = "err"
        color = {"ok": "#4ea583", "warn": "#d9a054", "err": "#d96a6a",
                 "dim": "#5d6a7c"}.get(kind, "#98a3b3")
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
        s["module"] = cfg.MODULES[self.module_box.currentIndex()][0]
        s["send_after_flash"] = bool(self.send_cb.isChecked())
        return s

    def _on_setting_changed(self) -> None:
        self.settings = self._collect()
        cfg.save_settings(self.settings)

    def _on_send_toggle(self) -> None:
        self.settings["send_after_flash"] = bool(self.send_cb.isChecked())
        cfg.save_settings(self.settings)

    # ------------------------------------------------------------------ роль
    def on_role_change(self, title: str) -> None:
        for slug, t, _env in cfg.ROLES:
            if t == title:
                self.role_slug = slug
                self.settings["role"] = slug
                cfg.save_settings(self.settings)
                break
        self.rescan_firmware()

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
        self.fw_lbl.setText(
            f"нет файла для роли «{cfg.role_title(self.role_slug)}» — "
            f"положи .uf2 в папку firmware или скачай с GitHub")
        self.file_lbl.setText("файл не выбран")
        self.meta_lbl.setText("")

    def _apply_file(self, path: str) -> None:
        env = cfg.role_env_from_filename(os.path.basename(path)) or self._env()
        self.file_path = path
        self.settings["last_env"] = env
        cfg.save_settings(self.settings)
        self.fw_lbl.setText(
            f"{cfg.role_title(cfg.role_for_env(env) or '')} · "
            f"{os.path.basename(path)}")
        try:
            info = Uf2Info.load(path)
        except (OSError, Uf2Error) as e:
            self.log_msg(str(e), "err")
            self.info = None
            return
        self.info = info
        self.file_lbl.setText(elide_text(os.path.basename(path), 420, True))
        self.meta_lbl.setText(f"{fmt_size(info.size)} · {info.num_blocks} бл.")

    def choose_file(self) -> None:
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбери прошивку .uf2", "",
            "UF2 firmware (*.uf2);;Все файлы (*)")
        if path:
            self._apply_file(path)

    def _load_releases(self) -> None:
        def work() -> None:
            try:
                rel = cfg.list_firmware_releases()
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"список версий недоступен: {e}", "err"))
                self.q.put(("releases", []))
                return
            self.q.put(("releases", rel))

        threading.Thread(target=work, daemon=True).start()

    def _populate_versions(self, releases: list[dict]) -> None:
        self.releases = releases
        self.version_box.blockSignals(True)
        self.version_box.clear()
        if not releases:
            self.version_box.addItem("нет релизов прошивки")
            self.version_box.setEnabled(False)
            self.version_box.blockSignals(False)
            return
        for r in releases:
            label = f"{r['tag']} · {r['date']}" if r["date"] else r["tag"]
            self.version_box.addItem(label)
        self.version_box.setEnabled(True)
        self.version_box.blockSignals(False)
        self._on_version_change(0)

    def _on_version_change(self, index: int) -> None:
        if 0 <= index < len(self.releases):
            self.selected_release = self.releases[index]
            self.log_msg(
                f"версия прошивки: {self.selected_release['tag']} · "
                f"{self.selected_release['date']}", "dim")

    def download_online(self) -> None:
        if self.busy:
            return
        env = self._env()
        if not env:
            return
        rel = self.selected_release
        if rel and rel.get("files", {}).get(env):
            self.log_msg(f"качаю {rel['tag']} для роли "
                         f"«{cfg.role_title(self.role_slug)}»", "info")

            def work_rel() -> None:
                path = cfg.download_firmware(
                    env, rel["files"][env],
                    log=lambda m: self.q.put(("log", m, "dim")))
                if path:
                    self.q.put(("online_ready", path))

            threading.Thread(target=work_rel, daemon=True).start()
            return

        self.log_msg("читаю манифест с GitHub…", "info")

        def work() -> None:
            try:
                man = cfg.fetch_manifest()
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"манифест недоступен: {e}", "err"))
                return
            files = man.get("files") if isinstance(man.get("files"), dict) else {}
            url = files.get(env)
            if not url:
                self.q.put(("log", f"в манифесте v{man.get('version', '?')} "
                                   f"нет файла для этой роли", "err"))
                return
            self.q.put(("log", f"качаю {os.path.basename(url)}", "info"))
            path = cfg.download_firmware(
                env, url, log=lambda m: self.q.put(("log", m, "dim")))
            if path:
                self.q.put(("online_ready", path))

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- прошивка
    def flash_current(self) -> None:
        if self.busy:
            return
        if not self.info or not self.file_path:
            self.rescan_firmware()
        if not self.info or not self.file_path:
            self.log_msg("нет прошивки для этой роли", "err")
            return
        if self.info.family != UF2_FAMILY_NRF52840:
            self.log_msg(f"это UF2 для {family_name(self.info.family)}, "
                         f"а нужен nRF52840", "err")
            return
        self.log_msg(f"прошиваю: {cfg.role_title(self.role_slug)}", "ok")
        if len(self.vols) == 1:
            self._start_flash(self.vols[0])
        else:
            self.set_status("жду плату в режиме загрузчика — дважды нажми Reset")
            self.log_msg("жду плату в режиме загрузчика…", "warn")

    def _start_flash(self, vol) -> None:
        if self.busy or not self.info:
            return
        self.busy = True
        self.watcher.pause()
        self.set_progress(0)
        self.set_status(f"пишу {fmt_size(self.info.size)} — НЕ ОТКЛЮЧАЙ плату")
        self.log_msg(f"пишу {fmt_size(self.info.size)} на {vol.title}", "info")
        FlashJob(self.q, self.info, vol).start()

    # -------------------------------------------------------------------- COM
    def _wait_nrf_port(self, timeout: float = 15.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ports = cfg.nrf_ports()
            if ports:
                return ports[0]
            time.sleep(0.5)
        return None

    def _send_thread(self, port: str, prefix: float) -> None:
        def progress(p: float) -> None:
            self.q.put(("settings_progress", prefix + (1.0 - prefix) * p))

        ok = cfg.send_commands_batch(
            port, cfg.build_commands(self.settings), progress=progress,
            log=lambda m: self.q.put(("log", m, "dim")))
        self.q.put(("settings_done", ok))

    def _start_settings_send(self) -> None:
        if not self.settings.get("send_after_flash", True):
            self.set_progress(1.0)
            return
        self.set_progress(0.74)
        self.set_status("ищу ноду — НЕ ОТКЛЮЧАЙ плату")
        self.log_msg("ищу ноду среди портов nRF (VID 239A/1915)…", "info")

        def work() -> None:
            port = self._wait_nrf_port(15.0)
            if not port:
                self.q.put(("log", "нода не появилась — настройки НЕ отправлены. "
                                   "Проверь кабель и нажми «Отправить настройки»", "err"))
                self.q.put(("settings_done", False))
                return
            self.q.put(("log", f"порт {port}: отправляю настройки пачкой", "ok"))
            self.q.put(("settings_progress", 0.78))
            self._send_thread(port, 0.78)

        threading.Thread(target=work, daemon=True).start()

    def send_settings_now(self) -> None:
        if self.busy:
            return
        ports = cfg.nrf_ports()
        if not ports:
            self.log_msg("портов nRF не видно: подключи плату по USB", "err")
            return
        self.settings = self._collect()
        cfg.save_settings(self.settings)
        self.log_msg(f"отправляю настройки на {ports[0]}", "info")
        self.set_progress(0.05)
        threading.Thread(target=self._send_thread,
                         args=(ports[0], 0.05), daemon=True).start()

    # ------------------------------------------------------------- прогресс
    def set_progress(self, value: float) -> None:
        target = max(0, min(1000, int(value * 1000)))
        self.anim.stop()
        self.anim.setStartValue(self.bar.value())
        self.anim.setEndValue(target)
        self.anim.start()
        self.pct_lbl.setText(f"{int(value * 100)}%")

    def set_status(self, text: str) -> None:
        self.status_lbl.setText(text)

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "volumes":
                    self.vols = ev[1]
                    self._update_badge()
                    if len(self.vols) == 1 and not self.busy and self.info:
                        if self.status_lbl.text().startswith("жду плату"):
                            self._start_flash(self.vols[0])
                elif kind == "progress":
                    done, total = ev[1], ev[2]
                    if total:
                        self.set_progress((done / total) * 0.72)
                        self.set_status(f"пишу {fmt_size(done)} из "
                                        f"{fmt_size(total)} — НЕ ОТКЛЮЧАЙ плату")
                elif kind == "flash":
                    self._on_flash(ev[1])
                elif kind == "status":
                    self.set_status(ev[1])
                elif kind == "log":
                    self.log_msg(ev[1], ev[2])
                elif kind == "online_ready":
                    self.log_msg("прошивка скачана", "ok")
                    self._apply_file(ev[1])
                elif kind == "releases":
                    self._populate_versions(ev[1])
                elif kind == "settings_progress":
                    self.set_progress(ev[1])
                elif kind == "settings_done":
                    if ev[1]:
                        self.set_progress(1.0)
                        self.set_status("готово: прошивка и настройки на месте")
                        self.log_msg("настройки записаны в ноду", "ok")
                    else:
                        self.set_status("прошивка записана, настройки — не полностью")
        except queue.Empty:
            pass

    def _set_badge(self, connected: bool) -> None:
        if connected:
            self.badge_lbl.setText("ПЛАТА ПОДКЛЮЧЕНА")
            self.badge_dot.setStyleSheet(
                "background:#4ea583;border-radius:4px;")
            self.badge.setStyleSheet(
                "QFrame#badge{background:#14301f;border:1px solid #2f7a5f;"
                "border-radius:13px;}")
        else:
            self.badge_lbl.setText("НЕТ ПЛАТЫ")
            self.badge_dot.setStyleSheet(
                "background:#d96a6a;border-radius:4px;")
            self.badge.setStyleSheet(
                "QFrame#badge{background:#2a1618;border:1px solid #6e2f35;"
                "border-radius:13px;}")

    def _update_badge(self) -> None:
        self._set_badge(bool(self.vols))

    def _on_flash(self, res: FlashResult) -> None:
        self.busy = False
        self.watcher.resume()
        if res.no_volume:
            self.log_msg("не успел поймать диск — жду ещё раз, нажми Reset", "warn")
            self.set_status("жду плату в режиме загрузчика — дважды нажми Reset")
            return
        if res.ok:
            self.set_progress(0.72)
            if res.rebooted:
                self.set_status(f"записано за {res.elapsed:.1f} с")
                self.log_msg(f"готово за {res.elapsed:.1f} с — плата "
                             f"перезагрузилась", "ok")
            else:
                self.set_status("записано, но плата не перезагрузилась — нажми Reset")
                self.log_msg("файл записан, но том не исчез", "warn")
            self._start_settings_send()
        else:
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
        self.log_msg(f"файл: {os.path.basename(path)}", "info")
        self.flash_current()

    def closeEvent(self, e) -> None:
        self.timer.stop()
        self.watcher.stop()
        super().closeEvent(e)


def run_gui() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


def main() -> None:
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"Dark Flasher {APP_VERSION}")
        return
    run_gui()


if __name__ == "__main__":
    main()
