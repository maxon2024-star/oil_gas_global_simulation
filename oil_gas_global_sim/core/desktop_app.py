# core/desktop_app.py
from collections import deque
import json
from pathlib import Path
import sys
from typing import Any, Deque, Dict, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.ipc_bus import SimulationIPCController


class GlobeIPCBridge(QObject):
    """
    Нативный мета-объектный мост QtWebChannel между процессом GUI PySide6
    и движком Three.js / Globe.gl. Работает через внутренний IPC Chromium без сокетов.
    """

    telemetryFrameReady = Signal(str)
    cameraFocusRequested = Signal(float, float, str)
    entityClickedIn3D = Signal(str)

    @Slot(str)
    def notifyEntitySelected(self, entity_id: str) -> None:
        self.entityClickedIn3D.emit(entity_id)


class LiveMarketChartWidget(QWidget):
    """
    Нативный аппаратный график котировок сырья (Brent, WTI, Urals, LNG)
    на базе Qt6 QPainter — не требует внешних библиотек построения графиков.
    """

    def __init__(self, max_points: int = 120, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(185)
        self.max_points = max_points
        self.series: Dict[str, Deque[float]] = {
            "BRENT": deque(maxlen=max_points),
            "WTI": deque(maxlen=max_points),
            "URALS": deque(maxlen=max_points),
            "LNG": deque(maxlen=max_points),
        }
        self.colors: Dict[str, QColor] = {
            "BRENT": QColor("#22c55e"),
            "WTI": QColor("#38bdf8"),
            "URALS": QColor("#f59e0b"),
            "LNG": QColor("#e879f9"),
        }

    def push_prices(self, prices: Dict[str, float]) -> None:
        for key in self.series:
            if key in prices:
                self.series[key].append(float(prices[key]))
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        painter.fillRect(rect, QColor("#091022"))

        margin_l, margin_r, margin_t, margin_b = 44, 14, 26, 22
        plot_w = max(10, rect.width() - margin_l - margin_r)
        plot_h = max(10, rect.height() - margin_t - margin_b)

        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(10, 16, "СПОТОВЫЕ КОТИРОВКИ ($/барр. и $/б.н.э.)")

        lx = rect.width() - 250
        for name, col in self.colors.items():
            painter.setPen(col)
            val = self.series[name][-1] if self.series[name] else 0.0
            painter.drawText(lx, 16, f"{name}: ${val:.1f}")
            lx += 62

        all_vals = [v for seq in self.series.values() for v in seq]
        if len(all_vals) < 2:
            return

        min_v = max(20.0, min(all_vals) - 5.0)
        max_v = max(all_vals) + 5.0
        span = max(1.0, max_v - min_v)

        grid_pen = QPen(QColor(148, 163, 184, 35), 1, Qt.PenStyle.DashLine)
        painter.setFont(QFont("Segoe UI", 8))
        for i in range(4):
            y = margin_t + int((plot_h / 3) * i)
            val_label = max_v - (span / 3) * i
            painter.setPen(grid_pen)
            painter.drawLine(margin_l, y, margin_l + plot_w, y)
            painter.setPen(QColor("#64748b"))
            painter.drawText(4, y + 4, f"${val_label:.0f}")

        for name, seq in self.series.items():
            if len(seq) < 2:
                continue
            path = QPainterPath()
            for idx, val in enumerate(seq):
                x = margin_l + (idx / max(1, self.max_points - 1)) * plot_w
                y = margin_t + (1.0 - (val - min_v) / span) * plot_h
                if idx == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            pen = QPen(self.colors[name], 2.0)
            painter.setPen(pen)
            painter.drawPath(path)


class OilGasSituationalCenterWindow(QMainWindow):
    """
    Главное десктопное окно Ситуационного Центра (Этап 5).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "GLOBAL OIL & GAS DIGITAL TWIN — Situational Center [IPC: SharedMemory + QtWebChannel]"
        )
        self.resize(1680, 960)
        self._apply_dark_theme()

        self.ipc_controller = SimulationIPCController(shm_name="oil_gas_twin_shm", random_seed=77)
        self.ipc_controller.start()

        self.globe_bridge = GlobeIPCBridge(self)
        self.current_state: Optional[Dict[str, Any]] = None
        self.is_paused = False

        self._build_ui()

        self.shm_timer = QTimer(self)
        self.shm_timer.timeout.connect(self._poll_shared_memory_frame)
        self.shm_timer.start(60)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #020617;
                color: #e2e8f0;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#Card {
                background-color: #091022;
                border: 1px solid #1e293b;
                border-radius: 6px;
            }
            QPushButton {
                background-color: #0f172a;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 5px;
                padding: 6px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton:hover {
                border-color: #38bdf8;
                background-color: #1e293b;
            }
            QPushButton#CrisisBtn {
                background-color: #2a0a10;
                border: 1px solid #dc2626;
                color: #fca5a5;
                text-align: left;
                padding: 7px 10px;
            }
            QPushButton#CrisisBtn:hover {
                background-color: #450a0a;
                border-color: #f87171;
            }
            QPushButton#ActionBtn {
                background-color: #0c2e24;
                border: 1px solid #10b981;
                color: #6ee7b7;
            }
            QTabWidget::pane {
                border: 1px solid #1e293b;
                background: #091022;
            }
            QTabBar::tab {
                background: #0f172a;
                color: #94a3b8;
                padding: 7px 12px;
                border: 1px solid #1e293b;
                font-size: 11px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #1e293b;
                color: #38bdf8;
                border-bottom: 2px solid #38bdf8;
            }
            QTableWidget {
                background-color: #091022;
                alternate-background-color: #0f172a;
                gridline-color: #1e293b;
                font-size: 11px;
                border: none;
            }
            QHeaderView::section {
                background-color: #0f172a;
                color: #38bdf8;
                padding: 4px;
                border: 1px solid #1e293b;
                font-size: 11px;
                font-weight: bold;
            }
        """)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        top_bar = QFrame()
        top_bar.setObjectName("Card")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)

        self.lbl_clock = QLabel("ДЕНЬ 01 | ЧАС 000.0")
        self.lbl_clock.setStyleSheet("color: #38bdf8; font-size: 15px; font-weight: bold;")
        top_layout.addWidget(self.lbl_clock)

        for label, spd in [("⏸ Пауза", 0), ("▶ 1x", 2.0), ("⏩ 4x", 6.0), ("⚡ 12x", 16.0), ("🚀 24x", 32.0)]:
            btn = QPushButton(label)
            if spd == 0:
                btn.clicked.connect(self._toggle_pause)
                self.btn_pause = btn
            else:
                btn.clicked.connect(lambda checked=False, s=spd: self._set_sim_speed(s))
            top_layout.addWidget(btn)

        btn_step = QPushButton("+1 Час")
        btn_step.clicked.connect(lambda: self.ipc_controller.send_command("STEP_ONCE"))
        top_layout.addWidget(btn_step)

        top_layout.addStretch()

        self.kpi_labels: Dict[str, QLabel] = {}
        kpi_specs = [
            ("brent", "BRENT: $82.5"),
            ("urals", "URALS: $68.0"),
            ("lng", "LNG: $95.0"),
            ("vlcc", "VLCC: $48,000/сут"),
            ("prod", "Добыча: 0.0M б"),
            ("profit", "Прибыль флота: $0.0M"),
            ("risk", "Воен. риск: 0.05"),
        ]
        for key, default_txt in kpi_specs:
            lbl = QLabel(default_txt)
            lbl.setStyleSheet(
                "background:#0f172a; border:1px solid #1e293b; border-radius:4px; padding:5px 9px; font-weight:bold; font-size:11px;"
            )
            self.kpi_labels[key] = lbl
            top_layout.addWidget(lbl)

        root_layout.addWidget(top_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left_panel = QFrame()
        left_panel.setObjectName("Card")
        left_panel.setMinimumWidth(295)
        left_panel.setMaximumWidth(330)
        left_layout = QVBoxLayout(left_panel)

        title_god = QLabel("⚡ ПУЛЬТ БОГА (CHAOS ENGINE)")
        title_god.setStyleSheet("color: #f87171; font-size: 13px; font-weight: bold;")
        left_layout.addWidget(title_god)

        crisis_buttons = [
            ("🌀 Ураган Кат.5 (Мексиканский залив)", "GULF_HURRICANE_CAT5"),
            ("🚀 Блокада Красного моря (Хуситы)", "RED_SEA_HOUTHI_CRISIS"),
            ("⚓ Блокада Ормузского пролива", "HORMUZ_STRAIT_BLOCKADE"),
            ("❄️ Экстремальный ледостав на СМП", "ARCTIC_WINTER_POLAR_LOCK"),
            ("💥 Диверсия на трубах БТС и «Дружба»", "DRUZHBA_BTS_SABOTAGE"),
            ("🚢 Авария танкера и закрытие Босфора", "BOSPORUS_TANKER_COLLISION"),
            ("🌊 Мега-шторм в Северной Атлантике", "NORTH_ATLANTIC_MEGASTORM"),
            ("📉 Санкционный шок G7 Price Cap $45", "G7_SANCTIONS_CAP_45"),
        ]
        for btn_text, scenario_key in crisis_buttons:
            b = QPushButton(btn_text)
            b.setObjectName("CrisisBtn")
            b.clicked.connect(
                lambda checked=False, k=scenario_key: self.ipc_controller.send_command(
                    "TRIGGER_CRISIS", scenario=k
                )
            )
            left_layout.addWidget(b)

        btn_sts = QPushButton("🔄 Запустить STS-эстафету в море (Греция)")
        btn_sts.setObjectName("ActionBtn")
        btn_sts.clicked.connect(lambda: self.ipc_controller.send_command("SPAWN_STS_DEMO"))
        left_layout.addWidget(btn_sts)

        btn_clear = QPushButton("✅ Снять все катаклизмы и блокады")
        btn_clear.setObjectName("ActionBtn")
        btn_clear.clicked.connect(lambda: self.ipc_controller.send_command("CLEAR_ALL_CRISES"))
        left_layout.addWidget(btn_clear)

        lbl_nav = QLabel("🎯 НАВЕДЕНИЕ 3D-КАМЕРЫ НА УЗЛЫ")
        lbl_nav.setStyleSheet("color: #38bdf8; font-size: 12px; font-weight: bold; margin-top: 8px;")
        left_layout.addWidget(lbl_nav)

        nav_grid = QGridLayout()
        hotspots = [
            ("Персидский залив", 26.2, 52.5),
            ("Суэц / Красное м.", 22.0, 38.0),
            ("Балтика / Европа", 55.5, 14.0),
            ("Ямал / Арктика", 71.2, 75.0),
            ("Козьмино / КНР", 35.0, 127.0),
            ("Хьюстон / США", 28.5, -94.0),
        ]
        for idx, (h_name, lat, lon) in enumerate(hotspots):
            nb = QPushButton(h_name)
            nb.clicked.connect(
                lambda checked=False, la=lat, lo=lon: self.globe_bridge.cameraFocusRequested.emit(
                    la, lo, ""
                )
            )
            nav_grid.addWidget(nb, idx // 2, idx % 2)
        left_layout.addLayout(nav_grid)

        lbl_active_cat = QLabel("АКТИВНЫЕ КАТАКЛИЗМЫ В МИРЕ:")
        lbl_active_cat.setStyleSheet("color: #fbbf24; font-size: 11px; font-weight: bold; margin-top: 6px;")
        left_layout.addWidget(lbl_active_cat)

        self.lbl_active_crises = QLabel("Нет активных кризисов (Штатный режим)")
        self.lbl_active_crises.setWordWrap(True)
        self.lbl_active_crises.setStyleSheet("color: #94a3b8; font-size: 11px;")
        left_layout.addWidget(self.lbl_active_crises)
        left_layout.addStretch()

        splitter.addWidget(left_panel)

        self.web_view = QWebEngineView()
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)

        self.web_channel = QWebChannel(self.web_view.page())
        self.web_channel.registerObject("globeBridge", self.globe_bridge)
        self.web_view.page().setWebChannel(self.web_channel)

        html_path = Path(__file__).resolve().parent.parent / "frontend_3d" / "index.html"
        self.web_view.setUrl(QUrl.fromLocalFile(str(html_path)))
        splitter.addWidget(self.web_view)

        right_panel = QFrame()
        right_panel.setObjectName("Card")
        right_panel.setMinimumWidth(520)
        right_layout = QVBoxLayout(right_panel)

        self.market_chart = LiveMarketChartWidget()
        right_layout.addWidget(self.market_chart)

        self.tabs = QTabWidget()

        self.tbl_fleet = QTableWidget(0, 7)
        self.tbl_fleet.setHorizontalHeaderLabels(
            ["ID", "Судно", "Класс", "Коалиция", "Статус", "Груз", "P&L ($M)"]
        )
        self.tbl_fleet.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl_fleet.cellDoubleClicked.connect(self._on_fleet_row_double_clicked)
        self.tabs.addTab(self.tbl_fleet, "🚢 Флот (42)")

        self.tbl_pipes = QTableWidget(0, 5)
        self.tbl_pipes.setHorizontalHeaderLabels(
            ["Магистраль", "Сорт", "Поток (барр/ч)", "Целостность", "Диагностика"]
        )
        self.tbl_pipes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_pipes, "🛢️ Трубопроводы (8)")

        self.tbl_contracts = QTableWidget(0, 6)
        self.tbl_contracts.setHorizontalHeaderLabels(
            ["ID", "Маршрут", "Сорт", "FOB->CIF", "Судно", "Статус"]
        )
        self.tbl_contracts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_contracts, "📜 Биржа НПЗ")

        self.tbl_logs = QTableWidget(0, 4)
        self.tbl_logs.setHorizontalHeaderLabels(["Час", "Категория", "Уровень", "Событие"])
        self.tbl_logs.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_logs, "📡 Журнал СМО")

        right_layout.addWidget(self.tabs)
        splitter.addWidget(right_panel)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        root_layout.addWidget(splitter, stretch=1)

    def _toggle_pause(self) -> None:
        self.is_paused = not self.is_paused
        self.ipc_controller.send_command("SET_PAUSED", value=self.is_paused)
        self.btn_pause.setText("▶ Пуск" if self.is_paused else "⏸ Пауза")

    def _set_sim_speed(self, ticks_per_sec: float) -> None:
        self.is_paused = False
        self.btn_pause.setText("⏸ Пауза")
        self.ipc_controller.send_command("SET_PAUSED", value=False)
        self.ipc_controller.send_command("SET_SPEED", value=ticks_per_sec)

    def _on_fleet_row_double_clicked(self, row: int, _col: int) -> None:
        if not self.current_state:
            return
        vessels = self.current_state.get("vessels", [])
        if 0 <= row < len(vessels):
            v = vessels[row]
            self.globe_bridge.cameraFocusRequested.emit(float(v["lat"]), float(v["lon"]), v["id"])

    def _poll_shared_memory_frame(self) -> None:
        """Чтение свежего кадра из Shared Memory и синхронное обновление Qt6 + 3D-Земли"""
        state = self.ipc_controller.poll_latest_state()
        if state is None:
            return

        self.current_state = state
        kpi = state["kpi"]
        mkt = state["market"]

        self.globe_bridge.telemetryFrameReady.emit(json.dumps(state, ensure_ascii=False))

        self.lbl_clock.setText(f"ДЕНЬ {kpi['sim_day']:02d} | ЧАС {kpi['sim_hour']:05.1f}")
        prices = mkt["prices"]
        self.kpi_labels["brent"].setText(f"BRENT: ${prices['BRENT']:.2f}")
        self.kpi_labels["urals"].setText(f"URALS: ${prices['URALS']:.2f}")
        self.kpi_labels["lng"].setText(f"LNG: ${prices['LNG']:.2f}")
        self.kpi_labels["vlcc"].setText(f"VLCC: ${mkt['charter']['VLCC']:,.0f}/сут")
        self.kpi_labels["prod"].setText(f"Добыча: {kpi['total_produced_mbbl']:.1f}M б")
        self.kpi_labels["profit"].setText(f"Прибыль флота: ${kpi['fleet_net_profit_musd']:.1f}M")
        self.kpi_labels["risk"].setText(f"Воен. риск: {mkt['war_risk']:.2f}")

        self.market_chart.push_prices(prices)

        cats = state.get("cataclysms", [])
        if cats:
            lines = [f"• {c['name']} ({c['remaining_hr']:.0f} ч)" for c in cats]
            self.lbl_active_crises.setText("\n".join(lines))
            self.lbl_active_crises.setStyleSheet("color: #f87171; font-size: 11px; font-weight: bold;")
        else:
            self.lbl_active_crises.setText("Нет активных кризисов (Штатный режим)")
            self.lbl_active_crises.setStyleSheet("color: #4ade80; font-size: 11px;")

        active_tab = self.tabs.currentIndex()
        if active_tab == 0:
            self._update_fleet_table(state.get("vessels", []))
        elif active_tab == 1:
            self._update_pipes_table(state.get("pipelines", []))
        elif active_tab == 2:
            self._update_contracts_table(state.get("contracts", []))
        elif active_tab == 3:
            self._update_logs_table(state.get("logs", []))

    def _update_fleet_table(self, vessels: list) -> None:
        self.tbl_fleet.setRowCount(len(vessels))
        for r, v in enumerate(vessels):
            vals = [
                v["id"],
                v["name"],
                v["vessel_class"],
                v["alignment"],
                v["state"],
                f"{v['cargo']} ({v['fill_pct']:.0f}%)",
                f"${v['net_profit_musd']:.2f}M",
            ]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c == 4 and v["state"] != "IDLE":
                    item.setForeground(QColor("#38bdf8"))
                self.tbl_fleet.setItem(r, c, item)

    def _update_pipes_table(self, pipes: list) -> None:
        self.tbl_pipes.setRowCount(len(pipes))
        for r, p in enumerate(pipes):
            vals = [
                p["name"],
                p["commodity"],
                f"{p['flow_bbl_hr']:,.0f}",
                f"{p['integrity_pct']:.0f}%",
                p["bottleneck"],
            ]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c == 4 and p["bottleneck"] != "OK":
                    item.setForeground(QColor("#ef4444"))
                self.tbl_pipes.setItem(r, c, item)

    def _update_contracts_table(self, contracts: list) -> None:
        self.tbl_contracts.setRowCount(len(contracts))
        for r, cnt in enumerate(reversed(contracts)):
            vals = [
                cnt["id"],
                cnt["route"],
                cnt["commodity"],
                f"${cnt['fob']:.1f}->${cnt['cif']:.1f}",
                cnt["vessel"],
                cnt["status"],
            ]
            for c, text in enumerate(vals):
                self.tbl_contracts.setItem(r, c, QTableWidgetItem(text))

    def _update_logs_table(self, logs: list) -> None:
        self.tbl_logs.setRowCount(len(logs))
        for r, lg in enumerate(reversed(logs)):
            vals = [f"{lg['hour']:.0f} ч", lg["cat"], lg["sev"], lg["msg"]]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if lg["sev"] == "CRITICAL":
                    item.setForeground(QColor("#f87171"))
                elif lg["sev"] == "WARNING":
                    item.setForeground(QColor("#fbbf24"))
                self.tbl_logs.setItem(r, c, item)

    def closeEvent(self, event: Any) -> None:
        self.shm_timer.stop()
        self.ipc_controller.stop()
        super().closeEvent(event)


def launch_3d_situational_center() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    win = OilGasSituationalCenterWindow()
    win.show()
    sys.exit(app.exec())
