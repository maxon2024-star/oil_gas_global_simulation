# engines/chaos.py
import math
from typing import TYPE_CHECKING, Dict, Generator, List, Optional, Tuple

import numpy as np
import simpy

from core.config import EARTH_RADIUS_NM, CommodityType
from core.models import (
    CataclysmEvent,
    CataclysmType,
    TransportEdge,
    WellOperationalStatus,
    WorldNode,
)
from core.world_graph import haversine_nm

if TYPE_CHECKING:
    from core.simulation import SimulationEngine


def generate_impact_polygon(
    center_lat: float, center_lon: float, radius_nm: float, num_points: int = 24
) -> List[Tuple[float, float]]:
    """
    Генерация сферического гео-полигона [(lat, lon), ...] вокруг эпицентра катаклизма
    для отрисовки зон штормов, блокад и аварий на 3D-глобусе (Three.js / Globe.gl).
    """
    angular_dist = radius_nm / EARTH_RADIUS_NM
    lat1 = math.radians(center_lat)
    lon1 = math.radians(center_lon)
    coords: List[Tuple[float, float]] = []
    for i in range(num_points + 1):
        bearing = 2.0 * math.pi * (i / num_points)
        lat2 = math.asin(
            math.sin(lat1) * math.cos(angular_dist)
            + math.cos(lat1) * math.sin(angular_dist) * math.cos(bearing)
        )
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(angular_dist) * math.cos(lat1),
            math.cos(angular_dist) - math.sin(lat1) * math.sin(lat2),
        )
        coords.append((round(math.degrees(lat2), 4), round(math.degrees(lon2), 4)))
    return coords


class ChaosAndGeopoliticsEngine:
    """
    Этап 4: Движок стохастических катаклизмов, геополитических блокад и техногенных аварий.
    Поддерживает:
    1. Ручной запуск любого из 8 кризисных сценариев («Пульт Бога» / God Mode);
    2. Автоматический пуассоновский генератор случайных кризисов в фоне SimPy;
    3. Полное восстановление инфраструктуры после завершения таймера события.
    """

    SCENARIO_PRESETS: Dict[str, Dict] = {
        "GULF_HURRICANE_CAT5": {
            "name": "Ураган 5-й категории «Laura-X» в Мексиканском заливе",
            "type": CataclysmType.HURRICANE_STORM,
            "lat": 28.50,
            "lon": -93.50,
            "radius_nm": 650.0,
            "severity": 0.92,
            "duration_hours": 72.0,
            "price_shocks": {CommodityType.WTI: 9.5, CommodityType.LNG: 11.0, CommodityType.BRENT: 5.5},
        },
        "RED_SEA_HOUTHI_CRISIS": {
            "name": "Военная блокада Баб-эль-Мандебского пролива (Красное море)",
            "type": CataclysmType.STRAIT_MILITARY_BLOCKADE,
            "target_node_id": "CHK_BAB_EL_MANDEB",
            "lat": 12.58,
            "lon": 43.33,
            "radius_nm": 550.0,
            "severity": 0.95,
            "duration_hours": 168.0,
            "price_shocks": {CommodityType.BRENT: 11.5, CommodityType.ARAB_LIGHT: 10.5, CommodityType.LNG: 14.0},
        },
        "HORMUZ_STRAIT_BLOCKADE": {
            "name": "Геополитическая блокада Ормузского пролива (Персидский залив)",
            "type": CataclysmType.STRAIT_MILITARY_BLOCKADE,
            "target_node_id": "CHK_HORMUZ",
            "lat": 26.56,
            "lon": 56.25,
            "radius_nm": 480.0,
            "severity": 1.0,
            "duration_hours": 120.0,
            "price_shocks": {CommodityType.BRENT: 22.0, CommodityType.ARAB_LIGHT: 25.0, CommodityType.LNG: 28.0},
        },
        "ARCTIC_WINTER_POLAR_LOCK": {
            "name": "Экстремальный ледостав на Северном морском пути (Арктика)",
            "type": CataclysmType.ARCTIC_ICE_LOCK,
            "lat": 70.50,
            "lon": 115.00,
            "radius_nm": 1400.0,
            "severity": 0.88,
            "duration_hours": 240.0,
            "price_shocks": {CommodityType.LNG: 8.5, CommodityType.ESPO: 4.0},
        },
        "DRUZHBA_BTS_SABOTAGE": {
            "name": "Авария / диверсия на насосной станции нефтепроводов БТС и «Дружба»",
            "type": CataclysmType.PIPELINE_SABOTAGE,
            "target_pipelines": [
                ("FLD_WEST_SIBERIA", "PORT_PRIMORSK"),
                ("FLD_WEST_SIBERIA", "PORT_SCHWEDT"),
            ],
            "lat": 57.50,
            "lon": 38.00,
            "radius_nm": 350.0,
            "severity": 0.90,
            "duration_hours": 96.0,
            "price_shocks": {CommodityType.BRENT: 7.5, CommodityType.URAL: -4.0},
        },
        "BOSPORUS_TANKER_COLLISION": {
            "name": "Навигационная авария и перекрытие пролива Босфор",
            "type": CataclysmType.STRAIT_MILITARY_BLOCKADE,
            "target_node_id": "CHK_BOSPORUS",
            "lat": 41.12,
            "lon": 29.08,
            "radius_nm": 250.0,
            "severity": 0.85,
            "duration_hours": 84.0,
            "price_shocks": {CommodityType.KASHAGAN_CPC: 9.0, CommodityType.BRENT: 5.0},
        },
        "NORTH_ATLANTIC_MEGASTORM": {
            "name": "Атлантический штормовой циклон (Северное море — Ла-Манш)",
            "type": CataclysmType.HURRICANE_STORM,
            "lat": 54.50,
            "lon": 2.50,
            "radius_nm": 750.0,
            "severity": 0.82,
            "duration_hours": 64.0,
            "price_shocks": {CommodityType.BRENT: 6.5},
        },
        "G7_SANCTIONS_CAP_45": {
            "name": "Ужесточение санкций G7: снижение потолка цен Price Cap до $45/барр.",
            "type": CataclysmType.SANCTIONS_PRICE_CAP_SHOCK,
            "lat": 50.85,
            "lon": 4.35,
            "radius_nm": 400.0,
            "severity": 0.90,
            "duration_hours": 240.0,
            "price_shocks": {CommodityType.BRENT: 4.5, CommodityType.URAL: -7.5},
        },
    }

    def __init__(self, engine: "SimulationEngine", seed: int = 101) -> None:
        self.engine = engine
        self.world = engine.world
        self.rng = np.random.default_rng(seed)
        self.active_cataclysms: Dict[str, CataclysmEvent] = {}
        self.cataclysm_history: List[CataclysmEvent] = []
        self._event_seq: int = 0

    def trigger_scenario(
        self,
        scenario_key: str,
        custom_duration_hours: Optional[float] = None,
    ) -> CataclysmEvent:
        """Инъекция именованного катаклизма из каталога SCENARIO_PRESETS."""
        if scenario_key not in self.SCENARIO_PRESETS:
            raise ValueError(f"Неизвестный сценарий катаклизма: {scenario_key}")

        preset = self.SCENARIO_PRESETS[scenario_key]
        duration = custom_duration_hours or preset["duration_hours"]
        self._event_seq += 1
        ev_id = f"CAT_{self._event_seq:03d}_{scenario_key[:10]}"

        polygon = generate_impact_polygon(
            center_lat=preset["lat"],
            center_lon=preset["lon"],
            radius_nm=preset["radius_nm"],
        )

        event = CataclysmEvent(
            id=ev_id,
            name=preset["name"],
            cataclysm_type=preset["type"],
            severity=preset["severity"],
            center_lat=preset["lat"],
            center_lon=preset["lon"],
            radius_nm=preset["radius_nm"],
            start_hour=round(self.engine.env.now, 2),
            duration_hours=duration,
            remaining_hours=duration,
            is_active=True,
            polygon_coords=polygon,
            description=f"Сценарий {scenario_key} активирован на {duration:.0f} ч.",
        )

        self._apply_cataclysm_effects(event, preset)
        self.active_cataclysms[ev_id] = event
        self.cataclysm_history.append(event)

        self.engine.env.process(self._cataclysm_lifecycle_process(event, preset))
        return event

    def _apply_cataclysm_effects(self, event: CataclysmEvent, preset: Dict) -> None:
        """Применение физических, навигационных и рыночных эффектов катаклизма к графу мира"""
        ctype = event.cataclysm_type

        # 1. Тропический ураган или океанский шторм
        if ctype == CataclysmType.HURRICANE_STORM:
            for nid, node in self.world.nodes.items():
                dist = haversine_nm(event.center_lat, event.center_lon, node.lat, node.lon)
                if dist <= event.radius_nm:
                    attenuation = max(
                        0.10, 1.0 - event.severity * math.exp(-((dist / (event.radius_nm * 0.6)) ** 2))
                    )
                    node.weather_efficiency = round(min(node.weather_efficiency, attenuation), 2)
                    event.affected_node_ids.append(nid)

            for u, v, attr in self.world.maritime_graph.edges(data=True):
                edge: TransportEdge = attr["data"]
                n1, n2 = self.world.nodes[u], self.world.nodes[v]
                mid_lat, mid_lon = (n1.lat + n2.lat) / 2.0, (n1.lon + n2.lon) / 2.0
                if haversine_nm(event.center_lat, event.center_lon, mid_lat, mid_lon) <= event.radius_nm:
                    edge.weather_speed_factor = round(max(0.38, 1.0 - event.severity * 0.60), 2)

        # 2. Военная блокада или навигационное перекрытие узкого пролива
        elif ctype == CataclysmType.STRAIT_MILITARY_BLOCKADE:
            target_id = preset.get("target_node_id")
            if target_id and target_id in self.world.nodes:
                strait = self.world.nodes[target_id]
                strait.is_blocked = True
                event.affected_node_ids.append(target_id)

            for u, v, attr in self.world.maritime_graph.edges(data=True):
                edge: TransportEdge = attr["data"]
                if u == target_id or v == target_id:
                    edge.piracy_or_war_risk = min(1.0, edge.piracy_or_war_risk + 0.75)
                else:
                    n1, n2 = self.world.nodes[u], self.world.nodes[v]
                    if haversine_nm(event.center_lat, event.center_lon, n1.lat, n1.lon) <= event.radius_nm:
                        edge.piracy_or_war_risk = min(1.0, edge.piracy_or_war_risk + 0.45)

        # 3. Зимний ледостав в Арктике (СМП)
        elif ctype == CataclysmType.ARCTIC_ICE_LOCK:
            self.engine.winter_arctic_freeze = True
            for nid, node in self.world.nodes.items():
                if node.lat >= 64.0:
                    node.weather_efficiency = round(min(node.weather_efficiency, 0.72), 2)
                    event.affected_node_ids.append(nid)

        # 4. Диверсия / разрыв магистрального трубопровода
        elif ctype == CataclysmType.PIPELINE_SABOTAGE:
            target_pipes = preset.get("target_pipelines", [])
            for u, v in target_pipes:
                if self.world.pipeline_graph.has_edge(u, v):
                    edge: TransportEdge = self.world.pipeline_graph[u][v]["data"]
                    edge.integrity_pct = 0.0
                    edge.is_active = False
                    event.affected_pipelines.append((u, v))

        # 5. Пожар / авария на терминале или НПЗ
        elif ctype == CataclysmType.TERMINAL_FIRE_OUTAGE:
            target_id = preset.get("target_node_id")
            if target_id and target_id in self.world.nodes:
                node = self.world.nodes[target_id]
                node.is_blocked = True
                node.well_status = WellOperationalStatus.OFFLINE_DAMAGED
                event.affected_node_ids.append(target_id)

        # 6. Санкционный шок (снижение потолка цен G7 Price Cap)
        elif ctype == CataclysmType.SANCTIONS_PRICE_CAP_SHOCK:
            self.engine.router.G7_CRUDE_PRICE_CAP_USD = 45.0
            self.engine.market.g7_price_cap_usd = 45.0
            self.engine.market.sanctions_discount_urals_usd = 19.5
            self.engine.market.sanctions_discount_espo_usd = 10.5
            for nid, node in self.world.nodes.items():
                if node.sanctioned_for_western_fleet:
                    event.affected_node_ids.append(nid)

        # Мгновенный ценовой импульс на товарной бирже
        for comm, delta in preset.get("price_shocks", {}).items():
            self.engine.market.apply_instant_price_shock(comm, delta)

        self.engine.log_event(
            category="CHAOS",
            severity="CRITICAL",
            subject_id=event.id,
            message=(
                f"КАТАКЛИЗМ АКТИВИРОВАН: {event.name} (длительность {event.duration_hours:.0f} ч, "
                f"радиус {event.radius_nm:.0f} NM, затронуто узлов: {len(event.affected_node_ids)})."
            ),
            financial_impact_usd=event.severity * 2_500_000.0,
        )

    def _cataclysm_lifecycle_process(
        self, event: CataclysmEvent, preset: Dict
    ) -> Generator[simpy.Event, None, None]:
        """Процесс SimPy: обратный отсчёт времени катаклизма и восстановление мира"""
        while event.remaining_hours > 0.0 and event.is_active:
            step = min(self.engine.tick_hours, event.remaining_hours)
            yield self.engine.env.timeout(step)
            event.remaining_hours = round(max(0.0, event.remaining_hours - step), 2)

        if event.is_active:
            self._revert_cataclysm_effects(event, preset)

    def _revert_cataclysm_effects(self, event: CataclysmEvent, preset: Dict) -> None:
        """Снятие блокировок, ремонт трубопроводов и нормализация погоды после окончания кризиса"""
        event.is_active = False
        event.remaining_hours = 0.0
        ctype = event.cataclysm_type

        if ctype == CataclysmType.HURRICANE_STORM:
            for nid in event.affected_node_ids:
                self.world.nodes[nid].weather_efficiency = 1.0
            for _, _, attr in self.world.maritime_graph.edges(data=True):
                attr["data"].weather_speed_factor = 1.0

        elif ctype == CataclysmType.STRAIT_MILITARY_BLOCKADE:
            target_id = preset.get("target_node_id")
            if target_id and target_id in self.world.nodes:
                self.world.nodes[target_id].is_blocked = False
            for _, _, attr in self.world.maritime_graph.edges(data=True):
                edge: TransportEdge = attr["data"]
                edge.piracy_or_war_risk = max(0.0, edge.piracy_or_war_risk - 0.60)

        elif ctype == CataclysmType.ARCTIC_ICE_LOCK:
            self.engine.winter_arctic_freeze = False
            for nid in event.affected_node_ids:
                self.world.nodes[nid].weather_efficiency = 1.0

        elif ctype == CataclysmType.PIPELINE_SABOTAGE:
            for u, v in event.affected_pipelines:
                if self.world.pipeline_graph.has_edge(u, v):
                    edge: TransportEdge = self.world.pipeline_graph[u][v]["data"]
                    edge.integrity_pct = 100.0
                    edge.is_active = True

        elif ctype == CataclysmType.TERMINAL_FIRE_OUTAGE:
            for nid in event.affected_node_ids:
                node = self.world.nodes[nid]
                node.is_blocked = False
                node.well_status = WellOperationalStatus.ACTIVE

        elif ctype == CataclysmType.SANCTIONS_PRICE_CAP_SHOCK:
            self.engine.router.G7_CRUDE_PRICE_CAP_USD = 60.0
            self.engine.market.g7_price_cap_usd = 60.0
            self.engine.market.sanctions_discount_urals_usd = 12.5
            self.engine.market.sanctions_discount_espo_usd = 6.5

        self.active_cataclysms.pop(event.id, None)
        self.engine.log_event(
            category="CHAOS",
            severity="INFO",
            subject_id=event.id,
            message=f"КРИЗИС ЗАВЕРШЕН: «{event.name}». Инфраструктура и навигация восстановлены.",
        )

    def stochastic_chaos_loop(self, mean_interval_hours: float = 95.0) -> Generator[simpy.Event, None, None]:
        """Фоновый пуассоновский генератор случайных катаклизмов и геополитических шоков."""
        scenario_keys = list(self.SCENARIO_PRESETS.keys())
        while True:
            wait_hours = float(self.rng.exponential(mean_interval_hours))
            yield self.engine.env.timeout(max(24.0, wait_hours))

            if len(self.active_cataclysms) >= 3:
                continue

            chosen_key = str(self.rng.choice(scenario_keys))
            if any(chosen_key[:10] in cid for cid in self.active_cataclysms):
                continue

            self.trigger_scenario(chosen_key)
