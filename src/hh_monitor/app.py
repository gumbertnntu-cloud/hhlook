from __future__ import annotations

import copy
import html
import json
import logging
import os
import re
import sys
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .auth import interactive_auth, validate_state
from .export_xlsx import export_ui_tables_xlsx
from .logging_conf import configure_logging
from .models import ChangeRow, SeenVacancyRow, Vacancy
from .runner import RunResult, run_pipeline
from .settings import AppSettings, load_settings, save_settings
from .storage import get_run_seen_rows, get_vacancy_by_id

HELP_TEXT = """HH Monitor — короткая инструкция

1) Первый запуск
- Откройте папку проекта
- Дважды кликните HH Monitor.app (macOS) или HHLook.exe (Windows)
- Если macOS блокирует запуск:
  xattr -dr com.apple.quarantine "/path/to/HH Monitor.app"

2) Авторизация в hh.ru
- Нажмите «Авторизоваться»
- Откроется Chromium, войдите в аккаунт hh.ru вручную
- После входа сессия сохранится в state/state.json
- В интерфейсе state должен стать valid

3) Поиск
- Целевые позиции и блокеры: через /
- При необходимости нажмите «Сохранить настройки» (без запуска поиска)
- Запустите «Запустить поиск»
- В таблице «Найденные вакансии» включите тумблеры DD
- Нажмите «↓ deep-dive» для переноса в «Результат deep-dive»

4) Deep-dive
- Отметьте вакансии тумблером DD
- Нажмите «↓ deep-dive» (deep-dive запускается сразу)
- Справа обновятся детали выбранной вакансии

5) Отчеты
- Preview HTML: reports/latest.html
- Export XLSX: 2 листа (Общий поиск + Deep-dive)
"""


class HelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Инструкция")
        self.resize(760, 620)

        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Как пользоваться HH Monitor")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(HELP_TEXT)
        layout.addWidget(text, 1)

        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.accepted.connect(self.accept)
        close_box.rejected.connect(self.reject)
        close_box.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        layout.addWidget(close_box)


@dataclass(slots=True)
class AppContext:
    project_root: Path
    settings_path: Path
    settings: AppSettings
    logger: logging.Logger


@dataclass(slots=True)
class DeepQueueEntry:
    vacancy_id: str
    title: str
    company: str
    url: str
    added_at: str
    status: str = "queued"  # queued|in_progress|done|failed
    last_result_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "vacancy_id": self.vacancy_id,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "added_at": self.added_at,
            "status": self.status,
            "last_result_at": self.last_result_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> DeepQueueEntry | None:
        if not isinstance(payload, dict):
            return None
        vacancy_id = str(payload.get("vacancy_id", "")).strip()
        title = str(payload.get("title", "")).strip()
        if not vacancy_id or not title:
            return None
        company = str(payload.get("company", "")).strip()
        url = str(payload.get("url", "")).strip()
        added_at = str(payload.get("added_at") or payload.get("updated_at") or "").strip()
        status = str(payload.get("status", "queued")).strip() or "queued"
        last_result_at = str(
            payload.get("last_result_at") or payload.get("updated_at") or ""
        ).strip()
        return cls(
            vacancy_id=vacancy_id,
            title=title,
            company=company,
            url=url,
            added_at=added_at,
            status=status,
            last_result_at=last_result_at,
        )


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = checked
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(34, 18)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool) -> None:
        if self._checked == value:
            return
        self._checked = value
        self.update()
        self.toggled.emit(value)

    def mousePressEvent(self, event: object) -> None:
        # Toggle on mouse release to avoid accidental cross-toggle when rows
        # are re-rendered (e.g. Hide removes a row immediately).
        if hasattr(event, "button") and event.button() == Qt.LeftButton:
            if hasattr(event, "accept"):
                event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: object) -> None:
        if hasattr(event, "button") and event.button() == Qt.LeftButton:
            self.set_checked(not self._checked)
            if hasattr(event, "accept"):
                event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: object) -> None:
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        track_color = QColor("#2E4B42") if self._checked else QColor("#3A3A3F")
        knob_color = QColor("#E8F4EE") if self._checked else QColor("#C8C8CD")

        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 9, 9)

        knob_x = self.width() - 16 if self._checked else 2
        painter.setBrush(knob_color)
        painter.drawEllipse(knob_x, 2, 14, 14)


class Worker(QThread):
    success = Signal(object)
    failure = Signal(str)
    progress = Signal(str)

    def __init__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            result = self.func(*self.args, progress_callback=self.progress.emit, **self.kwargs)
            self.success.emit(result)
        except TypeError:
            try:
                result = self.func(*self.args, **self.kwargs)
                self.success.emit(result)
            except Exception as exc:  # noqa: BLE001
                self.failure.emit(f"{exc}\n{traceback.format_exc()}")
        except Exception as exc:  # noqa: BLE001
            self.failure.emit(f"{exc}\n{traceback.format_exc()}")


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.ctx = context

        self.main_source_rows: list[SeenVacancyRow] = []
        self.main_rows: list[SeenVacancyRow] = []
        self.last_change_rows: list[ChangeRow] = []
        self.deep_queue: list[DeepQueueEntry] = []
        self.deep_marks: dict[str, bool] = {}
        self.hidden_vacancy_ids: set[str] = set()
        self.applied_vacancy_ids: set[str] = set()
        self.deep_table_ids: list[str] = []
        self.vacancy_cache: dict[str, Vacancy] = {}
        self.active_deep_target_ids: list[str] = []
        self.deep_in_progress = False
        self.active_run_id: int | None = None

        self.current_worker: Worker | None = None
        self.partial_refresh_timer = QTimer(self)
        self.partial_refresh_timer.setInterval(900)
        self.partial_refresh_timer.timeout.connect(self._poll_partial_fast_results)
        self.last_run_mode = "fast"

        self.setWindowTitle("hhороший сканер")
        self.resize(1560, 1060)

        self._build_ui()
        self._apply_styles()
        self._load_from_settings()
        self._load_hidden_state()
        self._load_applied_state()
        self._load_queue_state()
        self._render_deep_table()
        self._refresh_state_status()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout()
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        central.setLayout(root)

        header = QFrame()
        header.setObjectName("HeaderBar")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(14, 12, 14, 12)
        header_layout.setSpacing(10)
        header.setLayout(header_layout)

        brand_col = QVBoxLayout()
        brand_col.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.setContentsMargins(0, 0, 0, 0)
        hh_badge = QLabel("hh")
        hh_badge.setObjectName("HeaderBadge")
        hh_badge.setAlignment(Qt.AlignCenter)
        hh_badge.setFixedSize(42, 42)
        title = QLabel("ороший сканер")
        title.setObjectName("HeaderTitle")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title.setFixedHeight(42)
        subtitle = QLabel("Fast-поиск обновляет выдачу, deep-dive работает только по очереди")
        subtitle.setObjectName("HeaderSub")
        title_row.addWidget(hh_badge, 0, Qt.AlignVCenter)
        title_row.addWidget(title, 0, Qt.AlignVCenter)
        title_row.addStretch(1)
        brand_col.addLayout(title_row)
        brand_col.addWidget(subtitle)
        header_layout.addLayout(brand_col, 1)

        header_actions = QHBoxLayout()
        header_actions.setSpacing(8)
        self.auth_btn = QPushButton("Авторизация")
        self.auth_btn.setObjectName("AuthButton")
        self.preview_btn = QPushButton("Preview HTML")
        self.export_btn = QPushButton("Экспорт XLS")
        self.help_btn = QPushButton("Инструкция")

        self.auth_btn.clicked.connect(self.on_auth_clicked)
        self.preview_btn.clicked.connect(self.on_preview_clicked)
        self.export_btn.clicked.connect(self.on_export_clicked)
        self.help_btn.clicked.connect(self.on_help_clicked)

        header_actions.addWidget(self.auth_btn)
        header_actions.addWidget(self.preview_btn)
        header_actions.addWidget(self.export_btn)
        header_actions.addWidget(self.help_btn)
        header_layout.addLayout(header_actions)

        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 1)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar_layout = QVBoxLayout()
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(10)
        sidebar.setLayout(sidebar_layout)

        side_title = QLabel("Параметры поиска")
        side_title.setObjectName("PanelTitle")
        side_hint = QLabel("1) Поиск -> 2) Отметка DD -> 3) ↓ deep-dive")
        side_hint.setObjectName("PanelHint")
        side_hint.setWordWrap(True)
        sidebar_layout.addWidget(side_title)
        sidebar_layout.addWidget(side_hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.positions_input = QPlainTextEdit()
        self.positions_input.setPlaceholderText(
            "директор по трансформации\nchief of staff\nисполнительный директор\nceo"
        )
        self.positions_input.setFixedHeight(92)
        self.positions_input.textChanged.connect(self._update_query_preview)

        self.blockers_input = QPlainTextEdit()
        self.blockers_input.setPlaceholderText("стажер\njunior\nбез опыта\nassistant")
        self.blockers_input.setFixedHeight(92)
        self.blockers_input.textChanged.connect(self._update_query_preview)

        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 100)

        self.age_spin = QSpinBox()
        self.age_spin.setRange(1, 365)

        self.min_salary_input = QLineEdit()
        self.min_salary_input.setPlaceholderText("optional")

        form.addRow("Целевые позиции (/)", self.positions_input)
        form.addRow("Блокеры (/)", self.blockers_input)
        form.addRow("Макс. страниц", self.pages_spin)
        form.addRow("Возраст вакансий", self.age_spin)
        form.addRow("Мин. зарплата", self.min_salary_input)
        sidebar_layout.addLayout(form)

        self.query_preview = QLabel()
        self.query_preview.setWordWrap(True)
        self.query_preview.setObjectName("QueryPreview")
        sidebar_layout.addWidget(self.query_preview)

        self.fast_run_btn = QPushButton("Запустить поиск")
        self.fast_run_btn.setObjectName("FastButton")
        self.fast_run_btn.clicked.connect(self.on_run_fast_clicked)
        self.save_settings_btn = QPushButton("Сохранить настройки")
        self.save_settings_btn.clicked.connect(self.on_save_settings_clicked)

        sidebar_layout.addWidget(self.save_settings_btn)
        sidebar_layout.addWidget(self.fast_run_btn)
        sidebar_layout.addStretch(1)

        body.addWidget(sidebar, 0)

        content_col = QVBoxLayout()
        content_col.setSpacing(10)
        body.addLayout(content_col, 1)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        content_col.addLayout(top_row, 1)

        self.found_group = QGroupBox("")
        found_layout = QVBoxLayout()

        self.main_table = QTableWidget(0, 7)
        self.main_table.setHorizontalHeaderLabels(
            ["DD", "Скрыть", "★", "Название", "Компания", "Город", "Ссылка"]
        )
        self._style_dd_header_cell(self.main_table, 0)
        self._style_hide_header_cell(self.main_table, 1)
        self._style_applied_header_cell(self.main_table, 2)
        self.main_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.main_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.main_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.main_table.verticalHeader().setVisible(False)
        self.main_table.itemSelectionChanged.connect(self._on_main_selection_changed)

        main_header = self.main_table.horizontalHeader()
        main_header.setSectionsMovable(False)
        main_header.setStretchLastSection(False)
        main_header.setMinimumSectionSize(56)
        main_header.setSectionResizeMode(0, QHeaderView.Fixed)
        main_header.setSectionResizeMode(1, QHeaderView.Fixed)
        main_header.setSectionResizeMode(2, QHeaderView.Fixed)
        main_header.setSectionResizeMode(3, QHeaderView.Interactive)
        main_header.setSectionResizeMode(4, QHeaderView.Interactive)
        main_header.setSectionResizeMode(5, QHeaderView.Interactive)
        main_header.setSectionResizeMode(6, QHeaderView.Interactive)
        self.main_table.setColumnWidth(0, 62)
        self.main_table.setColumnWidth(1, 72)
        self.main_table.setColumnWidth(2, 44)
        self.main_table.setColumnWidth(3, 320)
        self.main_table.setColumnWidth(4, 140)
        self.main_table.setColumnWidth(5, 130)
        self.main_table.setColumnWidth(6, 140)

        found_layout.addWidget(self.main_table)

        collect_row = QHBoxLayout()
        self.collect_btn = QPushButton("↓ deep-dive")
        self.collect_btn.setObjectName("CollectButton")
        self.collect_btn.clicked.connect(self.on_collect_deep_clicked)
        self.collect_btn_opacity = QGraphicsOpacityEffect(self.collect_btn)
        self.collect_btn_opacity.setOpacity(1.0)
        self.collect_btn.setGraphicsEffect(self.collect_btn_opacity)
        self.collect_btn_pulse = QPropertyAnimation(self.collect_btn_opacity, b"opacity", self)
        self.collect_btn_pulse.setDuration(1800)
        self.collect_btn_pulse.setStartValue(1.0)
        self.collect_btn_pulse.setKeyValueAt(0.5, 0.55)
        self.collect_btn_pulse.setEndValue(1.0)
        self.collect_btn_pulse.setEasingCurve(QEasingCurve.InOutSine)
        self.collect_btn_pulse.setLoopCount(-1)
        collect_row.addWidget(self.collect_btn)
        collect_row.addStretch(1)
        found_layout.addLayout(collect_row)

        collect_hint = QLabel(
            "Включите тумблеры DD и нажмите «↓ deep-dive»."
        )
        collect_hint.setObjectName("PanelHint")
        collect_hint.setWordWrap(True)
        found_layout.addWidget(collect_hint)

        self.found_group.setLayout(found_layout)
        top_row.addWidget(self.found_group, 3)

        self.summary_group = QGroupBox("")
        summary_layout = QVBoxLayout()
        self.summary_text = QTextBrowser()
        self.summary_text.setOpenExternalLinks(True)
        self.summary_text.setObjectName("DetailText")
        self.summary_text.setHtml(
            "Кликните по строке в таблице «Найденные вакансии», чтобы увидеть описание."
        )
        summary_layout.addWidget(self.summary_text, 1)
        self.summary_group.setLayout(summary_layout)
        top_row.addWidget(self.summary_group, 2)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)
        content_col.addLayout(bottom_row, 2)

        self.deep_group = QGroupBox("")
        deep_layout = QVBoxLayout()

        self.deep_table = QTableWidget(0, 4)
        self.deep_table.setHorizontalHeaderLabels(["Скрыть", "Вакансия", "Компания", "Ссылка"])
        self._style_hide_header_cell(self.deep_table, 0)
        self.deep_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.deep_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.deep_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.deep_table.verticalHeader().setVisible(False)
        self.deep_table.itemSelectionChanged.connect(self._on_deep_selection_changed)

        deep_header = self.deep_table.horizontalHeader()
        deep_header.setSectionResizeMode(0, QHeaderView.Fixed)
        deep_header.setSectionResizeMode(1, QHeaderView.Stretch)
        deep_header.setSectionResizeMode(2, QHeaderView.Stretch)
        deep_header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.deep_table.setColumnWidth(0, 72)
        self.deep_table.setColumnWidth(3, 140)

        deep_layout.addWidget(self.deep_table)

        deep_hint = QLabel("Эта таблица собирается кнопкой «↓ deep-dive» из отмеченных вакансий.")
        deep_hint.setObjectName("PanelHint")
        deep_hint.setWordWrap(True)
        deep_layout.addWidget(deep_hint)

        self.deep_group.setLayout(deep_layout)
        bottom_row.addWidget(self.deep_group, 3)

        self.details_group = QGroupBox("")
        details_layout = QVBoxLayout()

        details_caption = QLabel("Данные с hh.ru (deep-dive)")
        details_caption.setObjectName("PanelHint")
        details_layout.addWidget(details_caption)

        self.details_text = QTextBrowser()
        self.details_text.setOpenExternalLinks(True)
        self.details_text.setObjectName("DetailsEditor")
        self.details_text.setHtml(self._deep_details_placeholder_html())
        details_layout.addWidget(self.details_text, 1)
        self.details_group.setLayout(details_layout)
        bottom_row.addWidget(self.details_group, 2)

        logs_group = QGroupBox("Логи")
        logs_layout = QVBoxLayout()
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        logs_layout.addWidget(self.log_text)
        logs_group.setLayout(logs_layout)
        root.addWidget(logs_group)

        self.status_label = QLabel("Готово")
        self.status_label.setObjectName("PanelHint")
        root.addWidget(self.status_label)
        self._refresh_section_titles()

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            QWidget {
                background: #0B0B0E;
                color: #E7E7EA;
                font-family: 'SF Pro (SFNS)', 'SF Pro Text', '.SF NS Text';
                font-size: 12px;
            }
            QFrame#HeaderBar {
                background: #121216;
                border: 1px solid #2A2A2E;
                border-radius: 12px;
            }
            QLabel#HeaderBadge {
                background: #FF2D20;
                color: #FFFFFF;
                border-radius: 8px;
                font-size: 30px;
                font-weight: 700;
            }
            QLabel#HeaderTitle {
                font-size: 30px;
                font-weight: 700;
                color: #FFFFFF;
            }
            QLabel#HeaderSub { color: #7C7C82; font-size: 12px; }
            QLabel#Chip {
                background: #FF5C0018;
                color: #FF7A2A;
                border: 1px solid #3A2A24;
                border-radius: 999px;
                padding: 4px 10px;
                font-weight: 600;
            }
            QFrame#Sidebar {
                background: #141417;
                border: 1px solid #FF5C00;
                border-radius: 12px;
                min-width: 330px;
                max-width: 350px;
            }
            QLabel#PanelTitle { font-size: 34px; font-weight: 700; color: #FFFFFF; }
            QLabel#PanelHint { color: #7C7C82; }
            QLabel#QueryPreview {
                color: #B2B2B8;
                background: #0F0F12;
                border: 1px solid #2A2A2E;
                border-radius: 8px;
                padding: 8px;
            }
            QLineEdit, QSpinBox, QPlainTextEdit, QTextBrowser {
                background: #0F0F12;
                border: 1px solid #2A2A2E;
                border-radius: 8px;
                padding: 6px;
                color: #D9D9DE;
            }
            QGroupBox {
                background: #141417;
                border: 1px solid #1F1F23;
                border-radius: 12px;
                margin-top: 0px;
                padding-top: 20px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: padding;
                subcontrol-position: top left;
                top: 1px;
                left: 12px;
                padding: 0px 4px;
                color: #FFFFFF;
                background: transparent;
                border-radius: 6px;
                font-size: 12px;
            }
            QPushButton {
                background: #1B1F28;
                border: 1px solid #2F4761;
                border-radius: 8px;
                padding: 6px 10px;
                color: #D8E8FA;
                font-weight: 700;
            }
            QPushButton#AuthButton[sessionState="valid"] {
                background: #1F3F2E;
                border-color: #3D7A5A;
                color: #DFF5EA;
            }
            QPushButton#AuthButton[sessionState="invalid"] {
                background: #4A1F24;
                border-color: #7B2F39;
                color: #F8DDE1;
            }
            QPushButton:pressed {
                background: #151D29;
                border-color: #41658C;
                padding-top: 7px;
                padding-bottom: 5px;
            }
            QPushButton:disabled {
                background: #171A21;
                color: #6B778A;
                border-color: #242B37;
            }
            QPushButton#FastButton {
                background: #2E4B42;
                border-color: #3E665A;
                color: #E8F4EE;
            }
            QPushButton#CollectButton {
                background: #1A2533;
                border-color: #2F4761;
                color: #D8E8FA;
                padding: 5px 10px;
            }
            QPushButton#CollectButton:pressed {
                background: #28435F;
                border-color: #5A90C6;
                padding-top: 6px;
                padding-bottom: 4px;
            }
            QPushButton#TinyCopy {
                background: #243321;
                border: 1px solid #3A5A32;
                color: #D8ECD1;
                border-radius: 6px;
                padding: 1px 6px;
                font-size: 10px;
            }
            QPushButton#TinyOpen {
                background: #1D2736;
                border: 1px solid #324966;
                color: #D5E4F8;
                border-radius: 6px;
                padding: 1px 6px;
                font-size: 10px;
            }
            QPushButton#AppliedStar {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 11px;
                color: rgba(205, 211, 220, 0.45);
                padding: 0px;
                min-width: 22px;
                max-width: 22px;
                min-height: 22px;
                max-height: 22px;
                font-size: 15px;
                font-weight: 700;
            }
            QPushButton#AppliedStar:checked {
                color: #F1CD53;
                background: #2B2410;
                border-color: #4B3F1D;
            }
            QPushButton#AppliedStar:pressed, QPushButton#AppliedStar:checked:pressed {
                padding-top: 0px;
                padding-bottom: 0px;
            }
            QTableWidget {
                background: #111113;
                border: 1px solid #2A2A2E;
                border-radius: 8px;
                gridline-color: #2A2A2E;
            }
            QHeaderView::section {
                background: #17171A;
                color: #CFCFD4;
                border: 1px solid #2A2A2E;
                padding: 6px;
                font-weight: 700;
                font-size: 10px;
            }
            QTextBrowser#DetailText, QTextBrowser#DetailsEditor {
                color: #D0D0D4;
                line-height: 1.25;
            }
            QTextBrowser a {
                color: #B7D3F3;
                text-decoration: none;
            }
            """)

    def _split_slash_terms(self, value: str) -> list[str]:
        normalized = value.replace("\n", "/").replace(",", "/")
        terms = [chunk.strip() for chunk in normalized.split("/")]
        return [term for term in terms if term]

    def _compose_query_text(self, positions: list[str], blockers: list[str]) -> str:
        parts: list[str] = []
        for term in positions:
            parts.append(f'("{term}")' if " " in term else term)
        query = " OR ".join(parts)

        blocker_parts: list[str] = []
        for blocker in blockers:
            blocker_parts.append(f'-"{blocker}"' if " " in blocker else f"-{blocker}")

        if blocker_parts:
            query = f"{query} {' '.join(blocker_parts)}"
        return query.strip()

    def _build_query_text(
        self,
        *,
        allow_existing_positions_when_empty: bool = False,
    ) -> tuple[str, list[str], list[str]]:
        positions = self._split_slash_terms(self.positions_input.toPlainText())
        blockers = self._split_slash_terms(self.blockers_input.toPlainText())
        if not positions and allow_existing_positions_when_empty:
            positions = [term for term in self.ctx.settings.filters.include_keywords if term]
        if not positions:
            raise ValueError("Заполните поле 'Целевые позиции (/)'")
        return self._compose_query_text(positions, blockers), positions, blockers

    def _update_query_preview(self) -> None:
        try:
            query_text, _, _ = self._build_query_text()
        except ValueError:
            self.query_preview.setText("Собранный query: (заполните целевые позиции)")
            return
        self.query_preview.setText(f"Собранный query: {query_text}")

    def _load_from_settings(self) -> None:
        settings = self.ctx.settings
        positions = settings.filters.include_keywords or [settings.search.query_text]
        blockers = settings.filters.exclude_keywords

        self.positions_input.setPlainText("\n".join([term for term in positions if term]))
        self.blockers_input.setPlainText("\n".join(blockers))
        self.pages_spin.setValue(settings.search.max_pages)
        self.age_spin.setValue(settings.search.max_age_days)
        self.min_salary_input.setText(
            "" if settings.filters.min_salary is None else str(settings.filters.min_salary)
        )
        self._update_query_preview()

    def _collect_to_settings(self, mode: str) -> AppSettings:
        settings = copy.deepcopy(self.ctx.settings)
        query_text, positions, blockers = self._build_query_text()

        settings.search.mode = mode
        settings.search.max_pages = int(self.pages_spin.value())
        settings.search.max_age_days = int(self.age_spin.value())
        settings.search.query_text = query_text

        settings.filters.include_keywords = positions
        settings.filters.exclude_keywords = blockers

        min_salary_raw = self.min_salary_input.text().strip()
        settings.filters.min_salary = int(min_salary_raw) if min_salary_raw else None
        return settings

    def _db_path(self) -> Path:
        return self.ctx.project_root / self.ctx.settings.paths.db_path

    def _queue_path(self) -> Path:
        return self.ctx.project_root / "state" / "deep_queue.json"

    def _hidden_path(self) -> Path:
        return self.ctx.project_root / "state" / "hidden_vacancies.json"

    def _applied_path(self) -> Path:
        return self.ctx.project_root / "state" / "applied_vacancies.json"

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    def _human_time(self, raw: str) -> str:
        if not raw:
            return ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%H:%M")
        except Exception:
            return raw

    def _refresh_state_status(self) -> None:
        state_path = self.ctx.project_root / self.ctx.settings.paths.state_path
        is_valid = validate_state(self.ctx.settings.search.base_url, state_path, self.ctx.logger)
        self._set_auth_button_state(is_valid)
        status = "valid" if is_valid else "missing/expired"
        self._append_log(f"state: {status}")

    def _set_auth_button_state(self, is_valid: bool) -> None:
        self.auth_btn.setProperty("sessionState", "valid" if is_valid else "invalid")
        self.auth_btn.style().unpolish(self.auth_btn)
        self.auth_btn.style().polish(self.auth_btn)
        self.auth_btn.update()

    def _set_busy(self, busy: bool) -> None:
        for btn in [
            self.auth_btn,
            self.preview_btn,
            self.export_btn,
            self.help_btn,
            self.save_settings_btn,
            self.fast_run_btn,
            self.collect_btn,
        ]:
            btn.setDisabled(busy)

    def _on_worker_progress(self, message: str) -> None:
        marker = "__RUN_ID__:"
        if message.startswith(marker):
            raw = message[len(marker) :].strip()
            try:
                self.active_run_id = int(raw)
            except ValueError:
                self._append_log(message)
                return
            if self.last_run_mode == "fast" and not self.partial_refresh_timer.isActive():
                self.partial_refresh_timer.start()
            return
        self._append_log(message)

    def _poll_partial_fast_results(self) -> None:
        if self.last_run_mode != "fast" or self.active_run_id is None:
            return
        try:
            rows = get_run_seen_rows(self._db_path(), self.active_run_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("partial refresh failed: %s", exc)
            return
        if not rows:
            return
        self.main_source_rows = rows
        visible_rows = [
            row for row in self.main_source_rows if row.vacancy_id not in self.hidden_vacancy_ids
        ]
        self._populate_main_table(visible_rows, preserve_selection=True)
        self.status_label.setText(f"Выполняется fast-запуск... найдено: {len(rows)}")

    def _set_deep_running_ui(self, running: bool) -> None:
        self.deep_in_progress = running
        if running:
            self.collect_btn.setText("⏳ deep-dive...")
            self.collect_btn_pulse.stop()
            self.collect_btn_pulse.start()
            self.status_label.setText("deep-dive выполняется...")
        else:
            self.collect_btn_pulse.stop()
            self.collect_btn_opacity.setOpacity(1.0)
            self.collect_btn.setText("↓ deep-dive")

    def _deep_details_placeholder_html(self) -> str:
        return (
            "Подробности вакансии появятся только после deep-dive.<br><br>"
            "1) Отметьте вакансию в столбце DD<br>"
            "2) Нажмите «↓ deep-dive»<br>"
            "3) Выберите вакансию в таблице «Результат deep-dive»"
        )

    def _append_log(self, message: str) -> None:
        self.log_text.appendPlainText(message)
        self.ctx.logger.info(message)

    def _load_hidden_state(self) -> None:
        self.hidden_vacancy_ids = set()
        path = self._hidden_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                self.hidden_vacancy_ids = {str(item) for item in payload if str(item).strip()}
        except Exception:  # noqa: BLE001
            self.hidden_vacancy_ids = set()

    def _save_hidden_state(self) -> None:
        path = self._hidden_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = sorted(self.hidden_vacancy_ids)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_applied_state(self) -> None:
        self.applied_vacancy_ids = set()
        path = self._applied_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                self.applied_vacancy_ids = {str(item) for item in payload if str(item).strip()}
        except Exception:  # noqa: BLE001
            self.applied_vacancy_ids = set()

    def _save_applied_state(self) -> None:
        path = self._applied_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = sorted(self.applied_vacancy_ids)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_queue_state(self) -> None:
        # Queue resets on each app launch: deep-dive is tied to the current fast run.
        self.deep_queue = []
        self.deep_marks = {}
        path = self._queue_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _save_queue_state(self) -> None:
        path = self._queue_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [entry.to_dict() for entry in self.deep_queue]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _queue_sort_key(self, item: DeepQueueEntry) -> tuple[int, str]:
        status_rank = {"in_progress": 0, "queued": 1, "failed": 2, "done": 3}.get(item.status, 4)
        ts = item.last_result_at or item.added_at
        return (status_rank, ts)

    def _render_queue_chip(self) -> None:
        return

    def _set_group_title_elided(self, group: QGroupBox, full_title: str) -> None:
        available = max(group.width() - 30, 60)
        metrics = QFontMetrics(group.font())
        group.setTitle(metrics.elidedText(full_title, Qt.ElideRight, available))

    def _refresh_section_titles(self) -> None:
        self._set_group_title_elided(
            self.found_group,
            f"Найденные вакансии: {len(self.main_rows)}",
        )
        self._set_group_title_elided(self.summary_group, "Полное описание вакансии")
        self._set_group_title_elided(
            self.deep_group,
            f"Результат deep-dive: {len(self.deep_queue)}",
        )
        self._set_group_title_elided(self.details_group, "Подробности вакансии")

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._refresh_section_titles()


    def _set_table_item(self, table: QTableWidget, row: int, col: int, value: str) -> None:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() ^ Qt.ItemIsEditable)
        table.setItem(row, col, item)

    def _style_hide_header_cell(self, table: QTableWidget, col: int) -> None:
        header_item = table.horizontalHeaderItem(col)
        if header_item is None:
            return
        header_item.setBackground(QColor("#180506"))
        header_item.setForeground(QColor("#F3D7D9"))

    def _style_dd_header_cell(self, table: QTableWidget, col: int) -> None:
        header_item = table.horizontalHeaderItem(col)
        if header_item is None:
            return
        header_item.setBackground(QColor("#040A1A"))
        header_item.setForeground(QColor("#D5E4FF"))

    def _style_applied_header_cell(self, table: QTableWidget, col: int) -> None:
        header_item = table.horizontalHeaderItem(col)
        if header_item is None:
            return
        header_item.setBackground(QColor("#111216"))
        header_item.setForeground(QColor("#C7CBD2"))

    def _make_toggle_widget(self, vacancy_id: str) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet(
            "background:#040A1A; border:1px solid #0C1736; border-radius:6px;"
        )
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)

        toggle = ToggleSwitch(checked=self.deep_marks.get(vacancy_id, False))
        toggle.toggled.connect(
            lambda checked, vid=vacancy_id: self._on_deep_mark_changed(vid, checked)
        )
        layout.addWidget(toggle)
        return wrapper

    def _make_hide_widget(self, vacancy_id: str) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet(
            "background:#180506; border:1px solid #2A0B0D; border-radius:6px;"
        )
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)

        toggle = ToggleSwitch(checked=vacancy_id in self.hidden_vacancy_ids)
        toggle.toggled.connect(
            lambda checked, vid=vacancy_id: self._on_hidden_mark_changed(vid, checked)
        )
        layout.addWidget(toggle)
        return wrapper

    def _make_applied_widget(self, vacancy_id: str) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet(
            "background:#111216; border:1px solid #22252D; border-radius:6px;"
        )
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)

        star_btn = QPushButton("★")
        star_btn.setObjectName("AppliedStar")
        star_btn.setCheckable(True)
        star_btn.setChecked(vacancy_id in self.applied_vacancy_ids)
        star_btn.setCursor(Qt.PointingHandCursor)
        star_btn.setFocusPolicy(Qt.ClickFocus)
        star_btn.toggled.connect(
            lambda checked, vid=vacancy_id: self._on_applied_mark_changed(vid, checked)
        )
        layout.addWidget(star_btn)
        return wrapper

    def _make_deep_hide_widget(self, vacancy_id: str) -> QWidget:
        wrapper = QWidget()
        wrapper.setStyleSheet(
            "background:#180506; border:1px solid #2A0B0D; border-radius:6px;"
        )
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)

        toggle = ToggleSwitch(checked=False)
        toggle.toggled.connect(
            lambda checked, vid=vacancy_id: self._on_deep_hidden_mark_changed(vid, checked)
        )
        layout.addWidget(toggle)
        return wrapper

    def _make_link_widget(self, url: str) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignCenter)

        copy_btn = QPushButton("copy")
        copy_btn.setObjectName("TinyCopy")
        copy_btn.clicked.connect(lambda _=False, value=url: self._copy_url(value))

        open_btn = QPushButton("open")
        open_btn.setObjectName("TinyOpen")
        open_btn.clicked.connect(lambda _=False, value=url: self._open_url(value))

        layout.addWidget(copy_btn)
        layout.addWidget(open_btn)
        return wrapper

    def _copy_url(self, url: str) -> None:
        QApplication.clipboard().setText(url)
        self._append_log(f"copied: {url}")

    def _open_url(self, url: str) -> None:
        if not url:
            return
        QDesktopServices.openUrl(QUrl(url))
        self._append_log(f"opened: {url}")

    def _on_deep_mark_changed(self, vacancy_id: str, checked: bool) -> None:
        self.deep_marks[vacancy_id] = checked

    def _on_hidden_mark_changed(self, vacancy_id: str, checked: bool) -> None:
        selected_deep_before = self._selected_deep_id()
        if checked:
            self.hidden_vacancy_ids.add(vacancy_id)
            self.deep_marks.pop(vacancy_id, None)
            self.deep_queue = [item for item in self.deep_queue if item.vacancy_id != vacancy_id]
            self._save_queue_state()
            self._append_log(f"hidden vacancy: {vacancy_id}")
        else:
            self.hidden_vacancy_ids.discard(vacancy_id)

        self._save_hidden_state()
        visible = [
            row
            for row in self.main_source_rows
            if row.vacancy_id not in self.hidden_vacancy_ids
        ]
        self._populate_main_table(visible)
        self._render_deep_table()
        self._restore_deep_context_after_hide(
            preferred_id=None if selected_deep_before == vacancy_id else selected_deep_before
        )

    def _on_applied_mark_changed(self, vacancy_id: str, checked: bool) -> None:
        if checked:
            self.applied_vacancy_ids.add(vacancy_id)
        else:
            self.applied_vacancy_ids.discard(vacancy_id)
        self._save_applied_state()

    def _on_deep_hidden_mark_changed(self, vacancy_id: str, checked: bool) -> None:
        if not checked:
            return
        self._on_hidden_mark_changed(vacancy_id, True)
        self._append_log(f"hidden from deep-dive: {vacancy_id}")

    def _restore_deep_context_after_hide(self, preferred_id: str | None) -> None:
        if not self.deep_table_ids:
            self.details_text.setHtml(self._deep_details_placeholder_html())
            return
        target_id = preferred_id if preferred_id in self.deep_table_ids else self.deep_table_ids[0]
        target_row = self.deep_table_ids.index(target_id)
        self.deep_table.selectRow(target_row)
        self._show_deep_context(target_id)

    def _is_in_queue(self, vacancy_id: str) -> bool:
        return any(item.vacancy_id == vacancy_id for item in self.deep_queue)

    def _populate_main_table(
        self,
        rows: list[SeenVacancyRow],
        *,
        preserve_selection: bool = False,
    ) -> None:
        selected_vacancy_id: str | None = None
        if preserve_selection:
            selected = self._selected_main_row()
            if selected is not None:
                selected_vacancy_id = selected.vacancy_id

        self.main_rows = rows
        self.main_table.setRowCount(len(rows))
        self._refresh_section_titles()

        for idx, row in enumerate(rows):
            if self._is_in_queue(row.vacancy_id):
                self.deep_marks[row.vacancy_id] = True

            self.main_table.setCellWidget(idx, 0, self._make_toggle_widget(row.vacancy_id))
            self.main_table.setCellWidget(idx, 1, self._make_hide_widget(row.vacancy_id))
            self.main_table.setCellWidget(idx, 2, self._make_applied_widget(row.vacancy_id))
            self._set_table_item(self.main_table, idx, 3, row.title)
            self._set_table_item(self.main_table, idx, 4, row.company)
            self._set_table_item(self.main_table, idx, 5, row.area)
            self.main_table.setCellWidget(idx, 6, self._make_link_widget(row.url))

        if not rows:
            self.summary_text.setHtml("Найденных вакансий пока нет.")
            self.details_text.setHtml(self._deep_details_placeholder_html())
            return

        if preserve_selection:
            if selected_vacancy_id:
                for idx, row in enumerate(rows):
                    if row.vacancy_id == selected_vacancy_id:
                        self.main_table.selectRow(idx)
                        return
            self.main_table.clearSelection()
            return

        self.main_table.clearSelection()
        self.summary_text.setHtml(
            "Кликните по строке в таблице «Найденные вакансии», чтобы увидеть описание."
        )
        self.details_text.setHtml(self._deep_details_placeholder_html())

    def _find_queue_index(self, vacancy_id: str) -> int:
        for idx, item in enumerate(self.deep_queue):
            if item.vacancy_id == vacancy_id:
                return idx
        return -1

    def _upsert_deep_queue_entry(self, row: SeenVacancyRow) -> None:
        now = self._now_iso()
        idx = self._find_queue_index(row.vacancy_id)
        if idx >= 0:
            item = self.deep_queue[idx]
            item.title = row.title
            item.company = row.company
            item.url = row.url
            if item.status == "done":
                item.status = "queued"
            if not item.added_at:
                item.added_at = now
            return
        self.deep_queue.append(
            DeepQueueEntry(
                vacancy_id=row.vacancy_id,
                title=row.title,
                company=row.company,
                url=row.url,
                added_at=now,
                status="queued",
            )
        )

    def _reset_deep_state(self) -> None:
        self.deep_queue = []
        self.deep_marks = {}
        self.deep_table_ids = []
        self.active_deep_target_ids = []
        self._save_queue_state()
        self._render_deep_table()
        self.details_text.setHtml(self._deep_details_placeholder_html())

    def _render_deep_table(self) -> None:
        self.deep_queue.sort(key=self._queue_sort_key)
        self.deep_table_ids = [item.vacancy_id for item in self.deep_queue]
        self.deep_table.setRowCount(len(self.deep_queue))
        self._refresh_section_titles()

        for idx, item in enumerate(self.deep_queue):
            self.deep_table.setCellWidget(idx, 0, self._make_deep_hide_widget(item.vacancy_id))
            self._set_table_item(self.deep_table, idx, 1, item.title)
            self._set_table_item(self.deep_table, idx, 2, item.company)
            self.deep_table.setCellWidget(idx, 3, self._make_link_widget(item.url))

        self._render_queue_chip()

    def _is_deep_completed(self, vacancy_id: str) -> bool:
        idx = self._find_queue_index(vacancy_id)
        if idx < 0:
            return False
        return self.deep_queue[idx].status == "done"

    def _deep_status(self, vacancy_id: str) -> str:
        idx = self._find_queue_index(vacancy_id)
        if idx < 0:
            return "missing"
        return self.deep_queue[idx].status

    def _selected_main_row(self, row_idx: int | None = None) -> SeenVacancyRow | None:
        if row_idx is None:
            selected = self.main_table.selectionModel().selectedRows()
            if not selected:
                return None
            idx = selected[0].row()
        else:
            idx = row_idx
        if idx < 0 or idx >= len(self.main_rows):
            return None
        return self.main_rows[idx]

    def _selected_deep_id(self) -> str | None:
        selected = self.deep_table.selectionModel().selectedRows()
        if not selected:
            return None
        idx = selected[0].row()
        if idx < 0 or idx >= len(self.deep_table_ids):
            return None
        return self.deep_table_ids[idx]

    def _get_vacancy(self, vacancy_id: str) -> Vacancy | None:
        cached = self.vacancy_cache.get(vacancy_id)
        if cached is not None:
            return cached
        vacancy = get_vacancy_by_id(self._db_path(), vacancy_id)
        if vacancy is not None:
            self.vacancy_cache[vacancy_id] = vacancy
        return vacancy

    def _as_html_text(self, value: str) -> str:
        return html.escape(value).replace("\n", "<br>")

    def _format_deep_full_text_html(self, value: str) -> str:
        lines = value.splitlines() if value else []
        if not lines:
            return ""

        def _render_inline(text: str) -> str:
            parts = re.split(r"(\*\*.+?\*\*)", text)
            rendered: list[str] = []
            for part in parts:
                if part.startswith("**") and part.endswith("**") and len(part) > 4:
                    content = part[2:-2].strip()
                    if content:
                        rendered.append(
                            "<span style='font-weight:700; color:#F0F0F3;'>"
                            f"{html.escape(content)}"
                            "</span>"
                        )
                    continue
                rendered.append(html.escape(part))
            return "".join(rendered)

        # HH can occasionally split ":" to the next line after a heading.
        normalized_lines: list[str] = []
        glue_next_to_previous = False
        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                glue_next_to_previous = False
                normalized_lines.append(raw)
                continue

            if stripped.startswith(":") and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()} {stripped}"
                continue

            if stripped == "," and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()},"
                glue_next_to_previous = True
                continue

            if stripped in {";", ".", "!", "?"} and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()}{stripped}"
                continue

            if stripped == "/" and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()} /"
                glue_next_to_previous = True
                continue

            if stripped.startswith(",") and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()}{stripped}"
                continue

            if glue_next_to_previous and normalized_lines:
                normalized_lines[-1] = f"{normalized_lines[-1].rstrip()} {stripped}"
                glue_next_to_previous = False
                continue

            normalized_lines.append(raw)

        formatted: list[str] = []

        def _ensure_blank_before_heading() -> None:
            if formatted and formatted[-1] != "":
                formatted.append("")
        for raw in normalized_lines:
            stripped = raw.strip()
            if not stripped:
                formatted.append("")
                continue

            stripped = re.sub(r"\s+([,.;:!?])", r"\1", stripped)
            stripped = re.sub(r"([а-яёa-z])([A-ZА-ЯЁ]{2,})", r"\1 \2", stripped)
            stripped = re.sub(r"([A-ZА-ЯЁ]{2,})([а-яёa-z])", r"\1 \2", stripped)
            stripped = re.sub(
                r"\b([A-ZА-ЯЁ]{2,})\s*/\s*([A-ZА-ЯЁ]{2,})\b",
                r"\1/\2",
                stripped,
            )

            colon_idx = stripped.find(":")
            if colon_idx > 0:
                heading = stripped[:colon_idx].strip()
                tail = stripped[colon_idx + 1 :].strip()
                heading_plain = re.sub(r"\*\*(.+?)\*\*", r"\1", heading)
                heading_has_letters = any(ch.isalpha() for ch in heading_plain)
                heading_starts_upper = heading_plain[:1].isupper() if heading_plain else False
                looks_like_heading = (
                    heading
                    and heading_has_letters
                    and heading_starts_upper
                    and len(heading_plain) <= 48
                    and heading_plain.count(" ") <= 6
                    and not any(ch in heading_plain for ch in ",.;")
                )
                if looks_like_heading:
                    _ensure_blank_before_heading()
                    heading_html = (
                        "<span style='color:#E6C15A; font-weight:700;'>"
                        f"{_render_inline(heading)}:"
                        "</span>"
                    )
                    if tail:
                        formatted.append(f"{heading_html} {_render_inline(tail)}")
                    else:
                        formatted.append(heading_html)
                    continue

            if stripped.endswith(":"):
                _ensure_blank_before_heading()
            formatted.append(_render_inline(stripped))

        return "<br>".join(formatted)

    def _full_description_text(self, vacancy: Vacancy) -> str:
        raw = vacancy.description or vacancy.snippet or ""
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines:
            return "Описание вакансии пока не загружено. Выполните deep-dive."
        return "\n".join(lines)

    def _summary_description_text(self, vacancy: Vacancy) -> str:
        return self._full_description_text(vacancy)

    def _format_summary_description_html(self, text: str) -> str:
        lines = text.splitlines() if text else []
        if not lines:
            return ""

        html_lines: list[str] = []
        in_mandatory_block = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                in_mandatory_block = False
                html_lines.append("")
                continue

            lower = stripped.lower()
            is_mandatory_header = lower.startswith("обязательно")
            if is_mandatory_header:
                in_mandatory_block = True
                html_lines.append(
                    "<span style='color:#7A1E1E; font-weight:700;'>"
                    f"{html.escape(stripped)}"
                    "</span>"
                )
                continue

            # Stop mandatory block on obvious new section headers.
            if in_mandatory_block and stripped.endswith(":") and not stripped.startswith("-"):
                in_mandatory_block = False

            if in_mandatory_block:
                html_lines.append(f"<span style='color:#7A1E1E;'>{html.escape(stripped)}</span>")
            else:
                html_lines.append(html.escape(stripped))

        return "<br>".join(html_lines)

    def _text_looks_truncated(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if stripped.endswith("...") or stripped.endswith("…"):
            return True
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        return any(line.endswith("...") or line.endswith("…") for line in lines)

    def _build_summary_html(self, vacancy: Vacancy | None) -> str:
        if vacancy is None:
            return "Данные по вакансии пока не загружены."

        raw_description = self._summary_description_text(vacancy)
        description_text = self._format_summary_description_html(raw_description)
        company_html = (
            f'<span style="color:#6EDB8A; font-size:16px; font-weight:700;">'
            f"{html.escape(vacancy.company)}"
            "</span>"
        )
        link_html = (
            f'<a href="{html.escape(vacancy.url)}">ссылка</a>' if vacancy.url else "не указана"
        )
        truncated_notice = ""
        if self._text_looks_truncated(raw_description):
            truncated_notice = (
                "<br><br><span style='color:#E2B96E;'>"
                "В выдаче hh.ru текст может быть сокращен. "
                "Полный текст появится после deep-dive."
                "</span>"
            )
        return (
            f"<b>Вакансия:</b> {html.escape(vacancy.title)}<br>"
            f"<b>Компания:</b> {company_html}<br>"
            f"<b>ЗП:</b> {html.escape(vacancy.salary_raw or 'не указана')}<br>"
            f"<b>Локация:</b> {html.escape(vacancy.area or 'не указана')}<br>"
            f"<b>Ссылка:</b> {link_html}<br><br>"
            f"<b>Описание:</b><br>{description_text}"
            f"{truncated_notice}"
        )

    def _build_structured_details_html(
        self,
        vacancy: Vacancy | None,
        *,
        deep_completed: bool,
        deep_status: str,
    ) -> str:
        if vacancy is None:
            return (
                "Выберите вакансию в «Результат deep-dive», чтобы увидеть структурированные детали."
            )
        if not deep_completed:
            if deep_status == "in_progress":
                return (
                    "Deep-dive выполняется для этой вакансии.<br><br>"
                    "Пожалуйста, дождитесь загрузки полной информации с hh.ru."
                )
            if deep_status == "failed":
                return (
                    "Не удалось загрузить полные детали этой вакансии "
                    "в прошлом запуске deep-dive.<br><br>"
                    "Нажмите «↓ deep-dive» еще раз для повторной попытки."
                )
            return (
                "Deep-dive для этой вакансии еще не запущен.<br><br>"
                "1) Отметьте DD в верхней таблице<br>"
                "2) Нажмите «↓ deep-dive»"
            )

        full_text = self._full_description_text(vacancy)
        updated = vacancy.activity_text or (
            vacancy.published_at.isoformat() if vacancy.published_at else "не указано"
        )

        link_html = (
            f'<a href="{html.escape(vacancy.url)}">ссылка</a>' if vacancy.url else "не указана"
        )
        return "".join(
            [
                f"<b>Вакансия:</b> {html.escape(vacancy.title)}<br>",
                f"<b>Компания:</b> {html.escape(vacancy.company)}<br>",
                f"<b>Ссылка:</b> {link_html}<br>",
                f"<b>Зарплата:</b> {html.escape(vacancy.salary_raw or 'не указана')}<br>",
                f"<b>Формат:</b> {html.escape(vacancy.area or 'не указано')}<br>",
                "<br><b>Полный текст вакансии с hh.ru:</b> ",
                f"{self._format_deep_full_text_html(full_text)}<br><br>",
                f"<b>Обновлено:</b> {html.escape(updated)}",
            ]
        )

    def _show_summary_context(self, vacancy_id: str) -> None:
        vacancy = self._get_vacancy(vacancy_id)
        self.summary_text.setHtml(self._build_summary_html(vacancy))
        self.details_text.setHtml(self._deep_details_placeholder_html())

    def _show_deep_context(self, vacancy_id: str) -> None:
        vacancy = self._get_vacancy(vacancy_id)
        self.summary_text.setHtml(self._build_summary_html(vacancy))
        self.details_text.setHtml(
            self._build_structured_details_html(
                vacancy,
                deep_completed=self._is_deep_completed(vacancy_id),
                deep_status=self._deep_status(vacancy_id),
            )
        )

    def _on_main_selection_changed(self) -> None:
        selected = self._selected_main_row()
        if selected is None:
            return
        self._show_summary_context(selected.vacancy_id)

    def _on_deep_selection_changed(self) -> None:
        vacancy_id = self._selected_deep_id()
        if vacancy_id is None:
            return
        self._show_deep_context(vacancy_id)

    def on_collect_deep_clicked(self) -> None:
        if self.deep_in_progress:
            QMessageBox.information(
                self,
                "deep-dive",
                "deep-dive уже выполняется. Дождитесь завершения текущего запуска.",
            )
            return

        selected = [row for row in self.main_rows if self.deep_marks.get(row.vacancy_id, False)]
        if not selected:
            QMessageBox.information(
                self, "deep-dive", "Отметьте вакансии тумблером DD в верхней таблице."
            )
            return

        for row in selected:
            self._upsert_deep_queue_entry(row)

        self._save_queue_state()
        self._render_deep_table()
        self._append_log(f"deep-dive queue updated: {len(selected)}")
        self.on_run_deep_clicked()

    def on_auth_clicked(self) -> None:
        self._append_log("Запуск интерактивной авторизации...")
        self._set_busy(True)
        state_path = self.ctx.project_root / self.ctx.settings.paths.state_path

        self.current_worker = Worker(
            interactive_auth,
            self.ctx.settings.search.base_url,
            state_path,
            self.ctx.logger,
        )
        self.current_worker.progress.connect(self._append_log)
        self.current_worker.success.connect(self._on_auth_done)
        self.current_worker.failure.connect(self._on_worker_error)
        self.current_worker.finished.connect(lambda: self._set_busy(False))
        self.current_worker.start()

    def _on_auth_done(self, ok: object) -> None:
        if bool(ok):
            self._append_log("Авторизация завершена. Сессия сохранена.")
        else:
            self._append_log("Авторизация не завершена (таймаут или ошибка).")
        self._refresh_state_status()

    def on_help_clicked(self) -> None:
        HelpDialog(self).exec()

    def on_run_fast_clicked(self) -> None:
        self.partial_refresh_timer.stop()
        self.active_run_id = None
        self.active_deep_target_ids = []
        self._reset_deep_state()
        self._start_run(mode="fast")

    def on_save_settings_clicked(self) -> None:
        try:
            query_text, positions, blockers = self._build_query_text(
                allow_existing_positions_when_empty=True
            )
            settings = copy.deepcopy(self.ctx.settings)
            settings.search.query_text = query_text
            settings.search.max_pages = int(self.pages_spin.value())
            settings.search.max_age_days = int(self.age_spin.value())
            settings.filters.include_keywords = positions
            settings.filters.exclude_keywords = blockers
            min_salary_raw = self.min_salary_input.text().strip()
            settings.filters.min_salary = int(min_salary_raw) if min_salary_raw else None
            self.ctx.settings = settings
            save_settings(self.ctx.settings, self.ctx.settings_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка сохранения", str(exc))
            return

        self.status_label.setText("Настройки сохранены.")
        self._append_log("settings saved manually")

    def on_run_deep_clicked(self) -> None:
        if self.deep_in_progress:
            return
        runnable = list(self.deep_queue)
        if not runnable:
            QMessageBox.information(
                self, "deep-dive", "Очередь deep-dive пуста. Сначала выберите вакансии."
            )
            return

        self.deep_queue.sort(key=self._queue_sort_key)
        self.active_deep_target_ids = [item.vacancy_id for item in self.deep_queue]
        for item in self.deep_queue:
            if item.vacancy_id in self.active_deep_target_ids:
                item.status = "in_progress"
        self._save_queue_state()
        self._render_deep_table()
        self._set_deep_running_ui(True)

        self._start_run(mode="deep", deep_target_ids=self.active_deep_target_ids)

    def _start_run(self, mode: str, deep_target_ids: list[str] | None = None) -> None:
        try:
            self.ctx.settings = self._collect_to_settings(mode)
            save_settings(self.ctx.settings, self.ctx.settings_path)
        except Exception as exc:  # noqa: BLE001
            if mode == "deep":
                self._set_deep_running_ui(False)
            QMessageBox.critical(self, "Ошибка настроек", str(exc))
            return

        self.last_run_mode = mode
        if mode != "fast":
            self.partial_refresh_timer.stop()
            self.active_run_id = None
        self._set_busy(True)
        self.status_label.setText(f"Выполняется {mode}-запуск...")
        self._append_log(
            f"Run started: mode={mode}, pages={self.ctx.settings.search.max_pages}, "
            "max_age_days="
            f"{self.ctx.settings.search.max_age_days}, "
            f"query={self.ctx.settings.search.query_text}"
        )

        self.current_worker = Worker(
            run_pipeline,
            project_root=self.ctx.project_root,
            settings=self.ctx.settings,
            logger=self.ctx.logger,
            deep_target_ids=deep_target_ids,
        )
        self.current_worker.progress.connect(self._on_worker_progress)
        self.current_worker.success.connect(self._on_run_done)
        self.current_worker.failure.connect(self._on_worker_error)
        self.current_worker.finished.connect(lambda: self._set_busy(False))
        self.current_worker.start()

    def _on_run_done(self, result: object) -> None:
        self.partial_refresh_timer.stop()
        self.active_run_id = None
        if not isinstance(result, RunResult):
            self._append_log("Unexpected run result type.")
            self._set_deep_running_ui(False)
            return

        run_result = result
        self.vacancy_cache.clear()
        self.last_change_rows = run_result.rows

        if self.last_run_mode == "fast":
            self._reset_deep_state()
            self.main_source_rows = run_result.seen_rows
            visible_rows = [
                row
                for row in self.main_source_rows
                if row.vacancy_id not in self.hidden_vacancy_ids
            ]
            self._populate_main_table(visible_rows)
        else:
            processed = set(run_result.processed_vacancy_ids)
            seen_map = {row.vacancy_id: row for row in run_result.seen_rows}
            now = self._now_iso()
            for item in self.deep_queue:
                if item.vacancy_id not in self.active_deep_target_ids:
                    continue
                if item.vacancy_id in seen_map:
                    item.title = seen_map[item.vacancy_id].title
                    item.company = seen_map[item.vacancy_id].company
                    item.url = seen_map[item.vacancy_id].url
                item.status = "done" if item.vacancy_id in processed else "failed"
                item.last_result_at = now
            self._save_queue_state()
            self._render_deep_table()
            if self.active_deep_target_ids:
                self._show_deep_context(self.active_deep_target_ids[0])
            self.active_deep_target_ids = []
            self._set_deep_running_ui(False)

        self.status_label.setText(
            f"Run {run_result.run_id} [{self.last_run_mode}] | "
            f"found={len(run_result.seen_rows)} | "
            f"new={run_result.stats.new_count}, "
            f"updated={run_result.stats.updated_count}, "
            f"removed={run_result.stats.removed_count}"
        )
        self._append_log(
            f"Run completed: run_id={run_result.run_id}, html={run_result.html_report_path}, "
            f"errors={len(run_result.stats.errors)}"
        )

    def _on_worker_error(self, message: str) -> None:
        self.partial_refresh_timer.stop()
        self.active_run_id = None
        if self.last_run_mode == "deep" and self.active_deep_target_ids:
            for item in self.deep_queue:
                if item.vacancy_id in self.active_deep_target_ids:
                    item.status = "queued"
            self.active_deep_target_ids = []
            self._save_queue_state()
            self._render_deep_table()
        if self.last_run_mode == "deep":
            self._set_deep_running_ui(False)

        if "SessionInvalidError" in message or "Session state is missing or expired" in message:
            self._set_auth_button_state(False)
            self.status_label.setText("Сессия невалидна, нужна повторная авторизация.")
            QMessageBox.warning(self, "Сессия истекла", "Сессия невалидна. Нажмите 'Авторизация'.")
        else:
            self.status_label.setText("Ошибка выполнения.")
            QMessageBox.critical(self, "Ошибка", message.splitlines()[0])
        self._append_log(message)

    def on_preview_clicked(self) -> None:
        report_path = self.ctx.project_root / self.ctx.settings.paths.reports_dir / "latest.html"
        if not report_path.exists():
            QMessageBox.information(self, "Preview", "Файл отчета еще не создан.")
            return
        webbrowser.open(report_path.resolve().as_uri())
        self._append_log(f"Preview opened: {report_path}")

    def on_export_clicked(self) -> None:
        search_rows = list(self.main_rows)
        deep_rows = [
            {
                "vacancy_id": item.vacancy_id,
                "title": item.title,
                "company": item.company,
                "url": item.url,
                "status": item.status,
                "added_at": item.added_at,
                "last_result_at": item.last_result_at,
            }
            for item in self.deep_queue
        ]
        if not search_rows and not deep_rows:
            QMessageBox.information(
                self,
                "Экспорт",
                "Нет данных для экспорта. Сначала выполните поиск или deep-dive.",
            )
            return

        out_path = (
            self.ctx.project_root
            / self.ctx.settings.paths.exports_dir
            / f"hh_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        export_ui_tables_xlsx(
            search_rows=search_rows,
            deep_rows=deep_rows,
            out_path=out_path,
        )
        self._append_log(f"Excel exported: {out_path}")
        QMessageBox.information(self, "Экспорт", f"Экспорт выполнен:\n{out_path}")


def build_context(project_root: Path) -> AppContext:
    settings_path = project_root / "config" / "settings.json"
    settings = load_settings(settings_path)
    settings.ensure_runtime_dirs(project_root)
    logger = configure_logging(project_root / settings.paths.logs_dir)
    return AppContext(
        project_root=project_root,
        settings_path=settings_path,
        settings=settings,
        logger=logger,
    )


def main() -> int:
    if getattr(sys, "frozen", False):
        default_root = Path.home() / "HHLook"
        override_root = os.environ.get("HHLOOK_HOME", "").strip()
        project_root = Path(override_root).expanduser() if override_root else default_root
    else:
        project_root = Path.cwd()

    project_root.mkdir(parents=True, exist_ok=True)
    ctx = build_context(project_root)

    app = QApplication(sys.argv)
    window = MainWindow(ctx)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
