# core/simulation.py
import math
from typing import Dict, Generator, List, Optional, Tuple

import simpy

from core.config import (
    BUNKER_FUEL_PRICE_USD_TON,
    COMMODITY_SPECS,
    VESSEL_SPECS,
    CommodityType,
    VesselClass,
)
from core.models import (
    CargoContract,
    ContractStatus,
    EdgeType,
    FleetAlignment,
    NodeType,
    PipelineBottleneck,
    PortServiceReceipt,
    SimEventLog,
    StorageTank,
    TankerAgentModel,
    TransportEdge,
    VesselState,
    WellOperationalStatus,
    WorldNode,
)
from core.world_graph import WorldLogisticsGraph, haversine_nm
from engines.chaos import ChaosAndGeopoliticsEngine
from engines.market import GlobalHydrocarbonMarketEngine
from engines.routing import MaritimeRoutingEngine


def calculate_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расчёт истинного навигационного курса (азимута 0..360°) между двумя точками"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    initial_bearing = math.degrees(math.atan2(x, y))
    return (initial_bearing + 360.0) % 360.0


class PortFacilitySMO:
    """СМО морского нефтеналивного или СПГ-терминала."""

    def __init__(self, env: simpy.Environment, node: WorldNode, engine: "SimulationEngine") -> None:
        self.env = env
        self.node = node
        self.engine = engine
        capacity = max(1, node.berth_count) if node.berth_count > 0 else 1
        self.berths = simpy.PriorityResource(env, capacity=capacity)
        self.free_laytime_hours: float = 4.0

    def process_vessel_call(
        self,
        vessel: TankerAgentModel,
        operation: str,
        commodity: CommodityType,
        target_volume_bbl: Optional[float] = None,
        priority: int = 10,
    ) -> Generator[simpy.Event, None, PortServiceReceipt]:
        v_spec = VESSEL_SPECS[vessel.vessel_class]
        arrival_hr = self.env.now

        if target_volume_bbl is None:
            target_volume_bbl = v_spec.capacity_bbl if operation == "LOAD" else vessel.cargo_volume_bbl

        vessel.current_node_id = self.node.id
        vessel.current_lat = self.node.lat
        vessel.current_lon = self.node.lon
        vessel.current_speed_knots = 0.0
        vessel.state = VesselState.QUEUED_FOR_LOAD if operation == "LOAD" else VesselState.QUEUED_FOR_UNLOAD

        self.node.queued_vessels += 1
        queue_wait_hr = 0.0
        demurrage_usd = 0.0
        boil_off_total_bbl = 0.0

        with self.berths.request(priority=priority) as req:
            while not req.triggered:
                yield self.env.timeout(self.engine.tick_hours)
                queue_wait_hr += self.engine.tick_hours
                boil_off_total_bbl += self.engine.apply_vessel_boil_off(vessel, self.engine.tick_hours)
                if queue_wait_hr > self.free_laytime_hours:
                    dem_mult = self.engine.market.demurrage_multipliers.get(vessel.vessel_class, 1.0)
                    hr_dem = v_spec.demurrage_rate_usd_hr * dem_mult * self.engine.tick_hours
                    demurrage_usd += hr_dem
                    vessel.accumulated_demurrage_usd += hr_dem
                    vessel.voyage_cost_usd += hr_dem
                    self.node.cumulative_demurrage_usd += hr_dem

            while self.node.is_blocked or self.node.weather_efficiency < 0.25:
                yield self.env.timeout(self.engine.tick_hours)
                queue_wait_hr += self.engine.tick_hours
                boil_off_total_bbl += self.engine.apply_vessel_boil_off(vessel, self.engine.tick_hours)
                if queue_wait_hr > self.free_laytime_hours:
                    dem_mult = self.engine.market.demurrage_multipliers.get(vessel.vessel_class, 1.0)
                    hr_dem = v_spec.demurrage_rate_usd_hr * dem_mult * self.engine.tick_hours
                    demurrage_usd += hr_dem
                    vessel.accumulated_demurrage_usd += hr_dem
                    vessel.voyage_cost_usd += hr_dem
                    self.node.cumulative_demurrage_usd += hr_dem

            self.node.queued_vessels = max(0, self.node.queued_vessels - 1)
            self.node.occupied_berths += 1
            berth_acquired_hr = self.env.now

            vessel.state = VesselState.LOADING if operation == "LOAD" else VesselState.UNLOADING
            if operation == "LOAD":
                vessel.cargo_type = commodity

            mooring_hours = 2.0
            yield self.env.timeout(mooring_hours)
            service_hours = mooring_hours

            transferred_bbl = 0.0
            tank = self.node.storage.get(commodity)
            if tank is None:
                tank = StorageTank(commodity=commodity, capacity_bbl=5_000_000, current_level_bbl=0.0)
                self.node.storage[commodity] = tank

            stalled_hours = 0.0
            while transferred_bbl < target_volume_bbl - 1.0:
                if self.node.is_blocked or self.node.weather_efficiency < 0.20:
                    yield self.env.timeout(self.engine.tick_hours)
                    service_hours += self.engine.tick_hours
                    continue

                pump_capacity_bbl = (
                    self.node.max_loading_rate_bbl_hr * self.node.weather_efficiency * self.engine.tick_hours
                )
                remaining_needed = target_volume_bbl - transferred_bbl

                if operation == "LOAD":
                    vessel_free_space = max(0.0, v_spec.capacity_bbl - vessel.cargo_volume_bbl)
                    desired_step = min(pump_capacity_bbl, remaining_needed, vessel_free_space)
                    actual_step = tank.remove_volume(desired_step)
                    vessel.cargo_volume_bbl += actual_step
                    transferred_bbl += actual_step
                else:
                    desired_step = min(pump_capacity_bbl, remaining_needed, vessel.cargo_volume_bbl)
                    actual_step = tank.add_volume(desired_step)
                    vessel.cargo_volume_bbl = max(0.0, vessel.cargo_volume_bbl - actual_step)
                    transferred_bbl += actual_step

                yield self.env.timeout(self.engine.tick_hours)
                service_hours += self.engine.tick_hours
                boil_off_total_bbl += self.engine.apply_vessel_boil_off(vessel, self.engine.tick_hours)

                if actual_step < pump_capacity_bbl * 0.1:
                    stalled_hours += self.engine.tick_hours
                    if stalled_hours > 6.0:
                        dem_mult = self.engine.market.demurrage_multipliers.get(vessel.vessel_class, 1.0)
                        hr_dem = v_spec.demurrage_rate_usd_hr * dem_mult * self.engine.tick_hours
                        demurrage_usd += hr_dem
                        vessel.accumulated_demurrage_usd += hr_dem
                        vessel.voyage_cost_usd += hr_dem
                        self.node.cumulative_demurrage_usd += hr_dem
                    if stalled_hours >= 36.0 and transferred_bbl > 0.0:
                        self.engine.log_event(
                            category="PORT_SMO",
                            severity="WARNING",
                            subject_id=self.node.id,
                            message=(
                                f"Танкер {vessel.name} завершил операцию {operation} досрочно "
                                f"({transferred_bbl:,.0f} из {target_volume_bbl:,.0f} барр.)."
                            ),
                        )
                        break

                if operation == "UNLOAD" and vessel.cargo_volume_bbl <= 1.0:
                    vessel.cargo_volume_bbl = 0.0
                    vessel.cargo_type = None
                    break

            unmooring_hours = 1.0
            yield self.env.timeout(unmooring_hours)
            service_hours += unmooring_hours

            self.node.occupied_berths = max(0, self.node.occupied_berths - 1)
            self.node.total_vessels_served += 1

            port_fee_usd = 25_000.0 + (transferred_bbl * 0.04)
            vessel.voyage_cost_usd += port_fee_usd
            self.node.cumulative_port_fees_usd += port_fee_usd
            vessel.state = VesselState.IDLE

            receipt = PortServiceReceipt(
                vessel_id=vessel.id,
                vessel_name=vessel.name,
                vessel_class=vessel.vessel_class,
                node_id=self.node.id,
                node_name=self.node.name,
                operation=operation,
                commodity=commodity,
                arrival_hour=round(arrival_hr, 2),
                berth_acquired_hour=round(berth_acquired_hr, 2),
                departure_hour=round(self.env.now, 2),
                queue_wait_hours=round(queue_wait_hr, 2),
                service_hours=round(service_hours, 2),
                volume_transferred_bbl=round(transferred_bbl, 0),
                boil_off_loss_bbl=round(boil_off_total_bbl, 1),
                demurrage_incurred_usd=round(demurrage_usd, 0),
                port_or_canal_fee_usd=round(port_fee_usd, 0),
            )
            self.engine.port_receipts.append(receipt)

            if demurrage_usd > 0:
                self.engine.log_event(
                    category="PORT_SMO",
                    severity="WARNING",
                    subject_id=self.node.id,
                    message=(
                        f"{vessel.name} простоял на рейде {self.node.name} "
                        f"{queue_wait_hr:.1f} ч. Демередж: ${demurrage_usd:,.0f}."
                    ),
                    financial_impact_usd=demurrage_usd,
                )

            return receipt


class ChokepointCanalSMO:
    """СМО узкого пролива или канала."""

    def __init__(self, env: simpy.Environment, node: WorldNode, engine: "SimulationEngine") -> None:
        self.env = env
        self.node = node
        self.engine = engine
        simultaneous_slots = max(2, min(20, node.max_daily_vessels // 3))
        self.canal_slots = simpy.Resource(env, capacity=simultaneous_slots)

    def transit_chokepoint(
        self, vessel: TankerAgentModel, leg_canal_fee_usd: float = 0.0
    ) -> Generator[simpy.Event, None, PortServiceReceipt]:
        v_spec = VESSEL_SPECS[vessel.vessel_class]
        arrival_hr = self.env.now
        self.node.queued_vessels += 1
        wait_hr = 0.0
        demurrage_usd = 0.0
        boil_off_bbl = 0.0

        with self.canal_slots.request() as req:
            while not req.triggered or self.node.is_blocked or self.node.weather_efficiency < 0.25:
                yield self.env.timeout(self.engine.tick_hours)
                wait_hr += self.engine.tick_hours
                boil_off_bbl += self.engine.apply_vessel_boil_off(vessel, self.engine.tick_hours)
                if wait_hr > self.node.canal_Wait_hours + 6.0:
                    dem_mult = self.engine.market.demurrage_multipliers.get(vessel.vessel_class, 1.0)
                    hr_dem = v_spec.demurrage_rate_usd_hr * dem_mult * self.engine.tick_hours
                    demurrage_usd += hr_dem
                    vessel.accumulated_demurrage_usd += hr_dem
                    vessel.voyage_cost_usd += hr_dem
                    self.node.cumulative_demurrage_usd += hr_dem

            self.node.queued_vessels = max(0, self.node.queued_vessels - 1)
            self.node.occupied_berths += 1
            slot_acquired_hr = self.env.now

            transit_duration_hr = max(1.0, self.node.canal_Wait_hours / max(0.3, self.node.weather_efficiency))
            elapsed = 0.0
            while elapsed < transit_duration_hr:
                step = min(self.engine.tick_hours, transit_duration_hr - elapsed)
                yield self.env.timeout(step)
                elapsed += step
                boil_off_bbl += self.engine.apply_vessel_boil_off(vessel, step)

            self.node.occupied_berths = max(0, self.node.occupied_berths - 1)
            self.node.total_vessels_served += 1

            total_fee = max(self.node.canal_toll_usd, leg_canal_fee_usd)
            vessel.voyage_cost_usd += total_fee
            self.node.cumulative_port_fees_usd += total_fee

            receipt = PortServiceReceipt(
                vessel_id=vessel.id,
                vessel_name=vessel.name,
                vessel_class=vessel.vessel_class,
                node_id=self.node.id,
                node_name=self.node.name,
                operation="CANAL_TRANSIT",
                commodity=vessel.cargo_type,
                arrival_hour=round(arrival_hr, 2),
                berth_acquired_hour=round(slot_acquired_hr, 2),
                departure_hour=round(self.env.now, 2),
                queue_wait_hours=round(wait_hr, 2),
                service_hours=round(transit_duration_hr, 2),
                volume_transferred_bbl=vessel.cargo_volume_bbl,
                boil_off_loss_bbl=round(boil_off_bbl, 1),
                demurrage_incurred_usd=round(demurrage_usd, 0),
                port_or_canal_fee_usd=round(total_fee, 0),
            )
            self.engine.port_receipts.append(receipt)
            return receipt


class SimulationEngine:
    """Главное дискретно-событийное ядро цифрового двойника (Этапы 2–4)."""

    def __init__(
        self,
        world: Optional[WorldLogisticsGraph] = None,
        tick_hours: float = 1.0,
        auto_spawn_fleet: bool = False,
        enable_contract_dispatcher: bool = False,
        enable_random_chaos: bool = False,
        winter_arctic_freeze: bool = False,
        random_seed: int = 42,
    ) -> None:
        self.world = world or WorldLogisticsGraph()
        self.tick_hours = tick_hours
        self.winter_arctic_freeze = winter_arctic_freeze
        self.env = simpy.Environment()

        # Рыночный, маршрутный и кризисный контуры (Этапы 3–4)
        self.market = GlobalHydrocarbonMarketEngine(self.world, seed=random_seed)
        self.router = MaritimeRoutingEngine(self.world, market=self.market)
        self.chaos = ChaosAndGeopoliticsEngine(self, seed=random_seed + 7)

        self.port_smos: Dict[str, PortFacilitySMO] = {}
        self.chokepoint_smos: Dict[str, ChokepointCanalSMO] = {}
        self.vessels: Dict[str, TankerAgentModel] = {}

        self.contracts: Dict[str, CargoContract] = {}
        self._contract_seq: int = 0
        self.reserved_export_bbl: Dict[Tuple[str, CommodityType], float] = {}
        self.scheduled_import_bbl: Dict[Tuple[str, CommodityType], float] = {}

        self.event_logs: List[SimEventLog] = []
        self.port_receipts: List[PortServiceReceipt] = []
        self.hourly_snapshots: List[Dict] = []

        self._initialize_smos()
        self.env.process(self._continuous_physics_loop())

        if auto_spawn_fleet:
            self.spawn_default_global_fleet()
        if enable_contract_dispatcher:
            self.env.process(self._contract_exchange_loop())
        if enable_random_chaos:
            self.env.process(self.chaos.stochastic_chaos_loop())

    def _initialize_smos(self) -> None:
        for node_id, node in self.world.nodes.items():
            if node.node_type == NodeType.STS_ZONE and node.berth_count == 0:
                node.berth_count = 4
                node.max_loading_rate_bbl_hr = 45_000.0

            if node.node_type in (
                NodeType.EXPORT_TERMINAL,
                NodeType.IMPORT_TERMINAL,
                NodeType.STS_ZONE,
            ) and node.berth_count > 0:
                self.port_smos[node_id] = PortFacilitySMO(self.env, node, self)
            elif node.node_type == NodeType.CHOKEPOINT:
                self.chokepoint_smos[node_id] = ChokepointCanalSMO(self.env, node, self)

        houston = self.world.nodes.get("PORT_HOUSTON")
        if houston and houston.production_rate_bbl_hr == 0.0:
            houston.production_rate_bbl_hr = 40_000.0
            houston.primary_commodity = CommodityType.LNG

    def initialize_global_fleet_and_market(self, enable_chaos: bool = False) -> None:
        if not self.vessels:
            self.spawn_default_global_fleet()
        self.env.process(self._contract_exchange_loop())
        if enable_chaos:
            self.env.process(self.chaos.stochastic_chaos_loop())

    def register_vessel(self, vessel: TankerAgentModel) -> TankerAgentModel:
        if vessel.current_node_id and vessel.current_node_id in self.world.nodes:
            node = self.world.nodes[vessel.current_node_id]
            vessel.current_lat = node.lat
            vessel.current_lon = node.lon
        self.vessels[vessel.id] = vessel
        return vessel

    def spawn_default_global_fleet(self) -> List[TankerAgentModel]:
        fleet_blueprint = [
            ("VLCC_01", "Bahri Diriyah", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "SA", "PORT_RAS_TANURA"),
            ("VLCC_02", "Bahri Abha", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "SA", "PORT_YANBU"),
            ("VLCC_03", "Cosco Shipping Venus", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "CN", "PORT_NINGBO"),
            ("VLCC_04", "Cosco Wisdom", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "CN", "PORT_SINGAPORE"),
            ("VLCC_05", "Frontline Alta", VesselClass.VLCC, FleetAlignment.WESTERN_INSURED, "BM", "PORT_ROTTERDAM"),
            ("VLCC_06", "Euronav Oceania", VesselClass.VLCC, FleetAlignment.WESTERN_INSURED, "BE", "PORT_HOUSTON"),
            ("VLCC_07", "Petrobras Marlim", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "BR", "PORT_SANTOS_BUZIOS"),
            ("VLCC_08", "Mitsui Tsuruga", VesselClass.VLCC, FleetAlignment.WESTERN_INSURED, "JP", "PORT_YOKOHAMA"),
            ("VLCC_09", "Reliance Jamnagar Star", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, "IN", "PORT_JAMNAGAR"),
            ("VLCC_10", "Titan Shadow VLCC", VesselClass.VLCC, FleetAlignment.SHADOW_FLEET, "PA", "STS_KALAMATA"),
            ("SUEZ_01", "Sovcomflot Nevsky", VesselClass.SUEZMAX, FleetAlignment.SHADOW_FLEET, "GA", "PORT_NOVOROSSIYSK"),
            ("SUEZ_02", "Black Sea Spirit", VesselClass.SUEZMAX, FleetAlignment.SHADOW_FLEET, "CM", "PORT_NOVOROSSIYSK"),
            ("SUEZ_03", "Caspian Voyager", VesselClass.SUEZMAX, FleetAlignment.NEUTRAL_STATE, "AE", "PORT_NOVOROSSIYSK"),
            ("SUEZ_04", "Red Sea Falcon", VesselClass.SUEZMAX, FleetAlignment.NEUTRAL_STATE, "SA", "PORT_YANBU"),
            ("SUEZ_05", "Nordic Apollo", VesselClass.SUEZMAX, FleetAlignment.WESTERN_INSURED, "NO", "PORT_BERGEN_TROLL"),
            ("SUEZ_06", "Teekay Explorer", VesselClass.SUEZMAX, FleetAlignment.WESTERN_INSURED, "MH", "PORT_HOUSTON"),
            ("SUEZ_07", "Buzios Carioca", VesselClass.SUEZMAX, FleetAlignment.NEUTRAL_STATE, "BR", "PORT_SANTOS_BUZIOS"),
            ("SUEZ_08", "Mumbai Pride", VesselClass.SUEZMAX, FleetAlignment.NEUTRAL_STATE, "IN", "PORT_JAMNAGAR"),
            ("SUEZ_09", "Aegean Horizon", VesselClass.SUEZMAX, FleetAlignment.WESTERN_INSURED, "GR", "PORT_ROTTERDAM"),
            ("SUEZ_10", "Pacific Dragon", VesselClass.SUEZMAX, FleetAlignment.NEUTRAL_STATE, "HK", "PORT_KOZMINO"),
            ("AFRA_01", "Baltic Sunrise", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "PA", "PORT_PRIMORSK"),
            ("AFRA_02", "Ladoga Prospect", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "LR", "PORT_PRIMORSK"),
            ("AFRA_03", "Neva Express", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "CK", "PORT_PRIMORSK"),
            ("AFRA_04", "Primorye Pioneer", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "PA", "PORT_KOZMINO"),
            ("AFRA_05", "Vostok Breeze", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "GA", "PORT_KOZMINO"),
            ("AFRA_06", "Shandong Harmony", VesselClass.AFRAMAX, FleetAlignment.NEUTRAL_STATE, "CN", "PORT_KOZMINO"),
            ("AFRA_07", "Stavanger Fjord", VesselClass.AFRAMAX, FleetAlignment.WESTERN_INSURED, "NO", "PORT_BERGEN_TROLL"),
            ("AFRA_08", "North Sea Guardian", VesselClass.AFRAMAX, FleetAlignment.WESTERN_INSURED, "GB", "PORT_ROTTERDAM"),
            ("AFRA_09", "Texas Ranger", VesselClass.AFRAMAX, FleetAlignment.WESTERN_INSURED, "US", "PORT_HOUSTON"),
            ("AFRA_10", "Tengiz Chevron", VesselClass.AFRAMAX, FleetAlignment.NEUTRAL_STATE, "KZ", "PORT_NOVOROSSIYSK"),
            ("AFRA_11", "Malacca Trader", VesselClass.AFRAMAX, FleetAlignment.NEUTRAL_STATE, "SG", "PORT_SINGAPORE"),
            ("AFRA_12", "Laconikos Shuttle", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, "PA", "STS_KALAMATA"),
            ("QMAX_01", "Al Mayeda Q-Max", VesselClass.LNG_QMAX, FleetAlignment.NEUTRAL_STATE, "QA", "PORT_RAS_LAFFAN"),
            ("QMAX_02", "Mozah Q-Max", VesselClass.LNG_QMAX, FleetAlignment.NEUTRAL_STATE, "QA", "PORT_RAS_LAFFAN"),
            ("QMAX_03", "Rasheeda LNG", VesselClass.LNG_QMAX, FleetAlignment.NEUTRAL_STATE, "QA", "PORT_RAS_LAFFAN"),
            ("QMAX_04", "Sabine Pass Cryo", VesselClass.LNG_QMAX, FleetAlignment.WESTERN_INSURED, "US", "PORT_HOUSTON"),
            ("QMAX_05", "Freeport Americas", VesselClass.LNG_QMAX, FleetAlignment.WESTERN_INSURED, "MH", "PORT_ROTTERDAM"),
            ("QMAX_06", "Tokyo Gas Maru", VesselClass.LNG_QMAX, FleetAlignment.WESTERN_INSURED, "JP", "PORT_YOKOHAMA"),
            ("ARC7_01", "Christophe de Margerie", VesselClass.ARC7_LNG, FleetAlignment.SHADOW_FLEET, "RU", "PORT_YAMAL_SABETTA"),
            ("ARC7_02", "Boris Vilkitsky", VesselClass.ARC7_LNG, FleetAlignment.NEUTRAL_STATE, "CY", "PORT_YAMAL_SABETTA"),
            ("ARC7_03", "Fedor Litke", VesselClass.ARC7_LNG, FleetAlignment.NEUTRAL_STATE, "CY", "PORT_YAMAL_SABETTA"),
            ("ARC7_04", "Vladimir Rusanov", VesselClass.ARC7_LNG, FleetAlignment.NEUTRAL_STATE, "HK", "PORT_NINGBO"),
        ]

        spawned = []
        for vid, vname, vcls, align, flag, start_node in fleet_blueprint:
            node = self.world.nodes[start_node]
            vessel = self.register_vessel(
                TankerAgentModel(
                    id=vid,
                    name=vname,
                    vessel_class=vcls,
                    alignment=align,
                    flag_country=flag,
                    current_lat=node.lat,
                    current_lon=node.lon,
                    current_node_id=start_node,
                )
            )
            spawned.append(vessel)
        return spawned

    def log_event(
        self,
        category: str,
        severity: str,
        subject_id: str,
        message: str,
        financial_impact_usd: float = 0.0,
    ) -> None:
        self.event_logs.append(
            SimEventLog(
                sim_hour=round(self.env.now, 2),
                sim_day=int(self.env.now // 24) + 1,
                category=category,
                severity=severity,
                subject_id=subject_id,
                message=message,
                financial_impact_usd=round(financial_impact_usd, 0),
            )
        )

    def apply_vessel_boil_off(self, vessel: TankerAgentModel, hours: float) -> float:
        if vessel.cargo_volume_bbl <= 0.0 or vessel.cargo_type is None:
            return 0.0
        c_spec = COMMODITY_SPECS[vessel.cargo_type]
        if c_spec.boil_off_rate_daily <= 0.0:
            return 0.0
        hourly_rate = c_spec.boil_off_rate_daily / 24.0
        loss_bbl = vessel.cargo_volume_bbl * hourly_rate * hours
        vessel.cargo_volume_bbl = max(0.0, vessel.cargo_volume_bbl - loss_bbl)
        vessel.accumulated_boil_off_bbl += loss_bbl
        return loss_bbl

    # =========================================================================
    # КОНТУР 1: ДОБЫЧА (UPSTREAM) И КОНСЕРВАЦИЯ СКВАЖИН (TANK TOPS)
    # =========================================================================
    def _step_upstream_extraction(self, dt_hours: float) -> None:
        for node in self.world.nodes.values():
            if node.production_rate_bbl_hr <= 0.0 or not node.storage:
                continue

            if node.is_blocked:
                node.well_status = WellOperationalStatus.OFFLINE_DAMAGED
                node.current_production_bbl_hr = 0.0
                continue

            if node.shut_in_cooldown_hours > 0.0:
                node.shut_in_cooldown_hours = max(0.0, node.shut_in_cooldown_hours - dt_hours)
                node.well_status = WellOperationalStatus.SHUT_IN_TANK_TOP
                node.current_production_bbl_hr = 0.0
                if node.shut_in_cooldown_hours == 0.0:
                    node.well_status = WellOperationalStatus.ACTIVE
                    self.log_event(
                        category="UPSTREAM",
                        severity="INFO",
                        subject_id=node.id,
                        message=f"Промысел {node.name}: расконсервация завершена, добыча возобновлена.",
                    )
                continue

            if node.node_type == NodeType.EXTRACTION_FIELD:
                target_tanks = list(node.storage.values())
            elif node.primary_commodity and node.primary_commodity in node.storage:
                target_tanks = [node.storage[node.primary_commodity]]
            else:
                target_tanks = list(node.storage.values())

            total_cap = sum(t.capacity_bbl for t in target_tanks)
            total_cur = sum(t.current_level_bbl for t in target_tanks)
            avg_fill_ratio = total_cur / total_cap if total_cap > 0 else 1.0

            if avg_fill_ratio >= 0.998 or all(t.free_space_bbl < 100.0 for t in target_tanks):
                if node.well_status != WellOperationalStatus.SHUT_IN_TANK_TOP:
                    penalty_usd = 350_000.0
                    node.well_status = WellOperationalStatus.SHUT_IN_TANK_TOP
                    node.shut_in_cooldown_hours = 24.0
                    node.shut_in_penalty_usd += penalty_usd
                    node.current_production_bbl_hr = 0.0
                    self.log_event(
                        category="UPSTREAM",
                        severity="CRITICAL",
                        subject_id=node.id,
                        message=(
                            f"TANK TOPS на {node.name}! Заполнение {avg_fill_ratio*100:.1f}%. "
                            f"Консервация на 24 ч (штраф ${penalty_usd:,.0f})."
                        ),
                        financial_impact_usd=penalty_usd,
                    )
                continue

            if avg_fill_ratio >= 0.95:
                if node.well_status != WellOperationalStatus.THROTTLED:
                    node.well_status = WellOperationalStatus.THROTTLED
                    self.log_event(
                        category="UPSTREAM",
                        severity="WARNING",
                        subject_id=node.id,
                        message=f"Высокий уровень баков на {node.name} ({avg_fill_ratio*100:.1f}%). Дебит прижат до 35%.",
                    )
                throttle_factor = 0.35
            else:
                node.well_status = WellOperationalStatus.ACTIVE
                throttle_factor = 1.0

            nominal_step_bbl = node.production_rate_bbl_hr * node.weather_efficiency * throttle_factor * dt_hours
            actual_produced_bbl = 0.0
            for tank in target_tanks:
                share = tank.capacity_bbl / total_cap if total_cap > 0 else (1.0 / len(target_tanks))
                accepted = tank.add_volume(nominal_step_bbl * share)
                actual_produced_bbl += accepted

                if tank.fill_ratio >= 0.999 and node.well_status != WellOperationalStatus.SHUT_IN_TANK_TOP:
                    penalty_usd = 350_000.0
                    node.well_status = WellOperationalStatus.SHUT_IN_TANK_TOP
                    node.shut_in_cooldown_hours = 24.0
                    node.shut_in_penalty_usd += penalty_usd
                    self.log_event(
                        category="UPSTREAM",
                        severity="CRITICAL",
                        subject_id=node.id,
                        message=f"TANK TOPS по сорту {tank.commodity.value} на {node.name}. Консервация на 24 ч.",
                        financial_impact_usd=penalty_usd,
                    )

            node.current_production_bbl_hr = actual_produced_bbl / dt_hours
            node.cumulative_produced_bbl += actual_produced_bbl

    # =========================================================================
    # КОНТУР 2: МАГИСТРАЛЬНЫЕ ТРУБОПРОВОДЫ
    # =========================================================================
    def _step_pipeline_network(self, dt_hours: float) -> None:
        for u, v, attr in self.world.pipeline_graph.edges(data=True):
            edge: TransportEdge = attr["data"]
            src_node = self.world.nodes[u]
            dst_node = self.world.nodes[v]
            commodity = edge.allowed_commodity

            if not edge.is_active or edge.integrity_pct <= 0.0 or src_node.is_blocked or dst_node.is_blocked:
                edge.current_flow_bbl_hr = 0.0
                edge.bottleneck = PipelineBottleneck.SHUTDOWN
                continue

            if commodity is None or commodity not in src_node.storage or commodity not in dst_node.storage:
                edge.current_flow_bbl_hr = 0.0
                continue

            src_tank = src_node.storage[commodity]
            dst_tank = dst_node.storage[commodity]

            max_pipe_step_bbl = edge.max_flow_bbl_hr * (edge.integrity_pct / 100.0) * dt_hours
            available_src_bbl = src_tank.current_level_bbl
            free_dst_bbl = dst_tank.free_space_bbl
            actual_flow_bbl = min(max_pipe_step_bbl, available_src_bbl, free_dst_bbl)

            prev_bottleneck = edge.bottleneck
            if actual_flow_bbl < max_pipe_step_bbl - 1.0:
                if free_dst_bbl <= available_src_bbl and free_dst_bbl < max_pipe_step_bbl:
                    edge.bottleneck = PipelineBottleneck.TARGET_FULL_BACKPRESSURE
                    if prev_bottleneck != PipelineBottleneck.TARGET_FULL_BACKPRESSURE:
                        self.log_event(
                            category="PIPELINE",
                            severity="WARNING",
                            subject_id=edge.name,
                            message=f"Противодавление в «{edge.name}»: баки {dst_node.name} заполнены на {dst_tank.fill_ratio*100:.1f}%.",
                        )
                else:
                    edge.bottleneck = PipelineBottleneck.SOURCE_DEPLETED
            elif edge.integrity_pct < 99.0:
                edge.bottleneck = PipelineBottleneck.REDUCED_INTEGRITY
            else:
                edge.bottleneck = PipelineBottleneck.OK

            if actual_flow_bbl > 0.0:
                src_tank.remove_volume(actual_flow_bbl)
                dst_tank.add_volume(actual_flow_bbl)

            edge.current_flow_bbl_hr = actual_flow_bbl / dt_hours
            edge.cumulative_pumped_bbl += actual_flow_bbl
            edge.cumulative_tariff_usd += actual_flow_bbl * edge.tariff_usd_bbl

    # =========================================================================
    # КОНТУР 3: ПЕРЕРАБОТКА НПЗ
    # =========================================================================
    def _step_downstream_consumption(self, dt_hours: float) -> None:
        for node in self.world.nodes.values():
            if node.consumption_rate_bbl_hr <= 0.0 or not node.storage:
                continue
            if node.is_blocked:
                node.current_consumption_bbl_hr = 0.0
                continue

            required_bbl = node.consumption_rate_bbl_hr * dt_hours
            available_tanks = [t for t in node.storage.values() if t.current_level_bbl > 0.0]
            total_available = sum(t.current_level_bbl for t in available_tanks)

            if total_available <= 0.0:
                node.current_consumption_bbl_hr = 0.0
                node.unmet_demand_bbl += required_bbl
                self.log_event(
                    category="REFINERY",
                    severity="CRITICAL",
                    subject_id=node.id,
                    message=f"ОСУШЕНИЕ РЕЗЕРВУАРОВ (Dry-out) на {node.name}! Непокрытый спрос +{required_bbl:,.0f} барр.",
                )
                continue

            actual_consumed_bbl = 0.0
            for tank in available_tanks:
                weight = tank.current_level_bbl / total_available
                drawn = tank.remove_volume(required_bbl * weight)
                actual_consumed_bbl += drawn

            if actual_consumed_bbl < required_bbl - 1.0:
                node.unmet_demand_bbl += required_bbl - actual_consumed_bbl

            node.current_consumption_bbl_hr = actual_consumed_bbl / dt_hours
            node.cumulative_consumed_bbl += actual_consumed_bbl

    def _continuous_physics_loop(self) -> Generator[simpy.Event, None, None]:
        """Главный цикл непрерывной физики мира и котировок рынка (каждый час)"""
        while True:
            self._step_upstream_extraction(self.tick_hours)
            self._step_pipeline_network(self.tick_hours)
            self._step_downstream_consumption(self.tick_hours)
            self.market.update_market_step(self.env.now, self.vessels, self.tick_hours)

            if int(self.env.now) % 6 == 0:
                self.hourly_snapshots.append(self.get_telemetry_snapshot())

            yield self.env.timeout(self.tick_hours)

    # =========================================================================
    # КОНТУР 4: МОРСКАЯ НАВИГАЦИЯ С ПЕРЕСТРОЕНИЕМ МАРШРУТА В МОРЕ (REROUTING)
    # =========================================================================
    def sail_along_route(self, vessel: TankerAgentModel, route_node_ids: List[str]) -> Generator[simpy.Event, None, None]:
        """
        Процесс SimPy: движение танкера по рёбрам морского графа.
        Поддерживает ДИНАМИЧЕСКИЙ ОБХОД БЛОКАД (Mid-Voyage Rerouting).
        """
        if len(route_node_ids) < 2:
            return

        v_spec = VESSEL_SPECS[vessel.vessel_class]
        is_laden = vessel.cargo_volume_bbl > 100.0
        vessel.state = VesselState.LADEN_TRANSIT if is_laden else VesselState.BALLAST_TRANSIT

        active_route = list(route_node_ids)
        vessel.planned_route = list(active_route)
        final_dest_id = active_route[-1]
        vessel.destination_node_id = final_dest_id

        idx = 0
        while idx < len(active_route) - 1:
            u_id, v_id = active_route[idx], active_route[idx + 1]
            u_node, v_node = self.world.nodes[u_id], self.world.nodes[v_id]

            # Проверка: не заблокирован ли следующий пролив/узел
            edge_blocked = False
            if self.world.maritime_graph.has_edge(u_id, v_id):
                leg_edge: TransportEdge = self.world.maritime_graph[u_id][v_id]["data"]
                if not leg_edge.is_active or (
                    leg_edge.requires_ice_class and self.winter_arctic_freeze and not v_spec.ice_class
                ):
                    edge_blocked = True

            if (v_node.is_blocked or edge_blocked) and v_id != final_dest_id:
                alt_route = self.router.find_congestion_aware_route(
                    origin_id=u_id,
                    dest_id=final_dest_id,
                    vessel_class=vessel.vessel_class,
                    is_laden=(vessel.cargo_volume_bbl > 100.0),
                    alignment=vessel.alignment,
                    winter_arctic_freeze=self.winter_arctic_freeze,
                )
                if alt_route and len(alt_route["path_ids"]) >= 2:
                    active_route = alt_route["path_ids"]
                    vessel.planned_route = list(active_route)
                    vessel.rerouted_count += 1
                    idx = 0
                    self.log_event(
                        category="MARITIME",
                        severity="WARNING",
                        subject_id=vessel.id,
                        message=(
                            f"ЭКСТРЕННЫЙ ОБХОД В МОРЕ: {vessel.name} развернулся у {u_node.name} "
                            f"из-за блокады {v_node.name}! Новый маршрут: {' -> '.join(alt_route['path_names'])}."
                        ),
                    )
                    continue

            if self.world.maritime_graph.has_edge(u_id, v_id):
                edge: TransportEdge = self.world.maritime_graph[u_id][v_id]["data"]
                segment_dist_nm = edge.distance_nm
                leg_canal_fee = edge.canal_fee_usd
            else:
                segment_dist_nm = haversine_nm(u_node.lat, u_node.lon, v_node.lat, v_node.lon) * 1.08
                leg_canal_fee = 0.0

            vessel.current_node_id = u_id
            vessel.heading_deg = round(calculate_bearing_deg(u_node.lat, u_node.lon, v_node.lat, v_node.lon), 1)

            covered_nm = 0.0
            while covered_nm < segment_dist_nm - 1e-2:
                weather_factor = (
                    self.world.maritime_graph[u_id][v_id]["data"].weather_speed_factor
                    if self.world.maritime_graph.has_edge(u_id, v_id)
                    else 1.0
                )
                base_speed = v_spec.speed_laden_knots if vessel.cargo_volume_bbl > 100.0 else v_spec.speed_ballast_knots
                eff_speed_knots = max(3.0, base_speed * weather_factor * v_node.weather_efficiency)
                vessel.current_speed_knots = round(eff_speed_knots, 1)

                remaining_nm = segment_dist_nm - covered_nm
                step_hours = min(self.tick_hours, remaining_nm / eff_speed_knots)
                step_nm = eff_speed_knots * step_hours

                yield self.env.timeout(step_hours)
                covered_nm += step_nm
                vessel.total_distance_sailed_nm += step_nm
                vessel.miles_since_maintenance += step_nm

                progress = min(1.0, covered_nm / segment_dist_nm) if segment_dist_nm > 0 else 1.0
                vessel.current_lat = round(u_node.lat + (v_node.lat - u_node.lat) * progress, 4)
                vessel.current_lon = round(u_node.lon + (v_node.lon - u_node.lon) * progress, 4)

                step_days = step_hours / 24.0
                bunker_tons = v_spec.bunker_burn_tons_day * step_days
                vessel.bunker_consumed_tons += bunker_tons
                live_charter = self.market.charter_rates.get(vessel.vessel_class, v_spec.daily_charter_usd)
                vessel.voyage_cost_usd += (
                    bunker_tons * self.market.bunker_price_usd_ton + step_days * live_charter
                )

                self.apply_vessel_boil_off(vessel, step_hours)

            vessel.current_node_id = v_id
            vessel.current_lat = v_node.lat
            vessel.current_lon = v_node.lon

            if v_id in self.chokepoint_smos:
                chk_smo = self.chokepoint_smos[v_id]
                yield self.env.process(chk_smo.transit_chokepoint(vessel, leg_canal_fee_usd=leg_canal_fee))
                vessel.state = VesselState.LADEN_TRANSIT if vessel.cargo_volume_bbl > 100.0 else VesselState.BALLAST_TRANSIT

            idx += 1

        vessel.current_speed_knots = 0.0

    def execute_full_delivery_mission(
        self,
        vessel: TankerAgentModel,
        load_port_id: str,
        unload_port_id: str,
        commodity: CommodityType,
        volume_bbl: Optional[float] = None,
        winter_arctic_freeze: Optional[bool] = None,
        contract: Optional[CargoContract] = None,
    ) -> Generator[simpy.Event, None, Tuple[Optional[PortServiceReceipt], Optional[PortServiceReceipt]]]:
        arctic_freeze = self.winter_arctic_freeze if winter_arctic_freeze is None else winter_arctic_freeze
        voyage_start_cost = vessel.voyage_cost_usd

        if vessel.current_node_id and vessel.current_node_id != load_port_id:
            ballast_route = self.router.find_congestion_aware_route(
                origin_id=vessel.current_node_id,
                dest_id=load_port_id,
                vessel_class=vessel.vessel_class,
                is_laden=False,
                alignment=vessel.alignment,
                winter_arctic_freeze=arctic_freeze,
            )
            if ballast_route:
                yield self.env.process(self.sail_along_route(vessel, ballast_route["path_ids"]))

        load_receipt: Optional[PortServiceReceipt] = None
        if load_port_id in self.port_smos:
            load_smo = self.port_smos[load_port_id]
            load_receipt = yield self.env.process(
                load_smo.process_vessel_call(vessel=vessel, operation="LOAD", commodity=commodity, target_volume_bbl=volume_bbl)
            )

        if contract is not None:
            key_exp = (load_port_id, commodity)
            self.reserved_export_bbl[key_exp] = max(0.0, self.reserved_export_bbl.get(key_exp, 0.0) - contract.target_volume_bbl)
            contract.status = ContractStatus.IN_TRANSIT

        laden_route = self.router.find_congestion_aware_route(
            origin_id=load_port_id,
            dest_id=unload_port_id,
            vessel_class=vessel.vessel_class,
            is_laden=True,
            alignment=vessel.alignment,
            winter_arctic_freeze=arctic_freeze,
        )
        if not laden_route:
            self.log_event(
                category="MARITIME",
                severity="CRITICAL",
                subject_id=vessel.id,
                message=f"Маршрут для гружёного {vessel.name} ({load_port_id} -> {unload_port_id}) заблокирован!",
            )
            if contract is not None:
                contract.status = ContractStatus.CANCELLED
            vessel.active_contract_id = None
            vessel.state = VesselState.IDLE
            return load_receipt, None

        self.log_event(
            category="MARITIME",
            severity="INFO",
            subject_id=vessel.id,
            message=(
                f"{vessel.name} вышел с грузом {vessel.cargo_volume_bbl:,.0f} барр. ({commodity.value}) "
                f"по маршруту {' -> '.join(laden_route['path_names'])} ({laden_route['distance_nm']:,.0f} миль)."
            ),
        )
        yield self.env.process(self.sail_along_route(vessel, laden_route["path_ids"]))

        unload_receipt: Optional[PortServiceReceipt] = None
        if unload_port_id in self.port_smos:
            unload_smo = self.port_smos[unload_port_id]
            unload_receipt = yield self.env.process(
                unload_smo.process_vessel_call(vessel=vessel, operation="UNLOAD", commodity=commodity)
            )
            delivered_bbl = unload_receipt.volume_transferred_bbl
            vessel.completed_voyages += 1
            vessel.total_cargo_delivered_bbl += delivered_bbl

            voyage_logistics_cost = vessel.voyage_cost_usd - voyage_start_cost
            if contract is not None:
                key_imp = (unload_port_id, commodity)
                self.scheduled_import_bbl[key_imp] = max(0.0, self.scheduled_import_bbl.get(key_imp, 0.0) - contract.target_volume_bbl)
                loaded_bbl = load_receipt.volume_transferred_bbl if load_receipt else delivered_bbl
                gross_margin = (delivered_bbl * contract.cif_price_usd_bbl) - (loaded_bbl * contract.fob_price_usd_bbl)
                net_profit = gross_margin - voyage_logistics_cost

                contract.delivered_volume_bbl = round(delivered_bbl, 0)
                contract.realized_net_profit_usd = round(net_profit, 0)
                contract.status = ContractStatus.COMPLETED
                contract.completed_hour = round(self.env.now, 2)

                vessel.total_revenue_usd += delivered_bbl * contract.cif_price_usd_bbl
                vessel.total_net_profit_usd += net_profit
            else:
                spot_spread = 7.5
                net_profit = (delivered_bbl * spot_spread) - voyage_logistics_cost
                vessel.total_revenue_usd += delivered_bbl * self.market.spot_prices[commodity]
                vessel.total_net_profit_usd += net_profit

            self.log_event(
                category="MARITIME",
                severity="INFO",
                subject_id=vessel.id,
                message=f"{vessel.name} доставил {delivered_bbl:,.0f} барр. ({commodity.value}) в {self.world.nodes[unload_port_id].name}.",
            )

        if vessel.miles_since_maintenance >= 22_000.0:
            vessel.state = VesselState.MAINTENANCE
            maint_cost = 45_000.0
            vessel.voyage_cost_usd += maint_cost
            vessel.total_net_profit_usd -= maint_cost
            self.log_event(
                category="MARITIME",
                severity="INFO",
                subject_id=vessel.id,
                message=f"{vessel.name} встал на 24-часовое ТО после {vessel.miles_since_maintenance:,.0f} миль.",
            )
            yield self.env.timeout(24.0)
            vessel.miles_since_maintenance = 0.0

        vessel.active_contract_id = None
        vessel.state = VesselState.IDLE
        return load_receipt, unload_receipt

    def execute_sts_relay_mission(
        self,
        feeder_vessel: TankerAgentModel,
        mother_vessel: TankerAgentModel,
        export_port_id: str,
        sts_hub_id: str,
        import_port_id: str,
        commodity: CommodityType,
        transfer_volume_bbl: Optional[float] = None,
        contract: Optional[CargoContract] = None,
    ) -> Generator[simpy.Event, None, Dict]:
        f_spec = VESSEL_SPECS[feeder_vessel.vessel_class]
        m_spec = VESSEL_SPECS[mother_vessel.vessel_class]
        vol_to_move = transfer_volume_bbl or min(f_spec.capacity_bbl, m_spec.capacity_bbl - mother_vessel.cargo_volume_bbl)
        start_cost = feeder_vessel.voyage_cost_usd + mother_vessel.voyage_cost_usd

        def move_mother_to_sts():
            if mother_vessel.current_node_id and mother_vessel.current_node_id != sts_hub_id:
                r_mother_ballast = self.router.find_congestion_aware_route(
                    origin_id=mother_vessel.current_node_id,
                    dest_id=sts_hub_id,
                    vessel_class=mother_vessel.vessel_class,
                    is_laden=False,
                    alignment=mother_vessel.alignment,
                )
                if r_mother_ballast:
                    yield self.env.process(self.sail_along_route(mother_vessel, r_mother_ballast["path_ids"]))
            mother_vessel.state = VesselState.STS_TRANSFER

        mother_proc = self.env.process(move_mother_to_sts())

        if feeder_vessel.current_node_id and feeder_vessel.current_node_id != export_port_id:
            r_feeder_ballast = self.router.find_congestion_aware_route(
                origin_id=feeder_vessel.current_node_id,
                dest_id=export_port_id,
                vessel_class=feeder_vessel.vessel_class,
                is_laden=False,
                alignment=feeder_vessel.alignment,
            )
            if r_feeder_ballast:
                yield self.env.process(self.sail_along_route(feeder_vessel, r_feeder_ballast["path_ids"]))

        load_smo = self.port_smos[export_port_id]
        feeder_load_rec = yield self.env.process(
            load_smo.process_vessel_call(vessel=feeder_vessel, operation="LOAD", commodity=commodity, target_volume_bbl=vol_to_move)
        )

        if contract is not None:
            key_exp = (export_port_id, commodity)
            self.reserved_export_bbl[key_exp] = max(0.0, self.reserved_export_bbl.get(key_exp, 0.0) - contract.target_volume_bbl)
            contract.status = ContractStatus.STS_RENDEZVOUS

        r_feeder_sts = self.router.find_congestion_aware_route(
            origin_id=export_port_id,
            dest_id=sts_hub_id,
            vessel_class=feeder_vessel.vessel_class,
            is_laden=True,
            alignment=feeder_vessel.alignment,
        )
        if r_feeder_sts:
            yield self.env.process(self.sail_along_route(feeder_vessel, r_feeder_sts["path_ids"]))

        yield mother_proc

        sts_node = self.world.nodes[sts_hub_id]
        feeder_vessel.state = VesselState.STS_TRANSFER
        mother_vessel.state = VesselState.STS_TRANSFER
        mother_vessel.cargo_type = commodity

        sts_start_hr = self.env.now
        yield self.env.timeout(4.0)

        sts_pump_rate_bbl_hr = 42_000.0 * sts_node.weather_efficiency
        actual_sts_bbl = feeder_vessel.cargo_volume_bbl
        pump_hours = math.ceil(actual_sts_bbl / max(5_000.0, sts_pump_rate_bbl_hr))
        yield self.env.timeout(pump_hours)

        feeder_vessel.cargo_volume_bbl = 0.0
        feeder_vessel.cargo_type = None
        mother_vessel.cargo_volume_bbl += actual_sts_bbl

        sts_fee_usd = 85_000.0
        mother_vessel.voyage_cost_usd += sts_fee_usd
        sts_node.total_vessels_served += 2
        sts_node.cumulative_port_fees_usd += sts_fee_usd

        sts_receipt = PortServiceReceipt(
            vessel_id=f"{feeder_vessel.id}->{mother_vessel.id}",
            vessel_name=f"{feeder_vessel.name} -> {mother_vessel.name}",
            vessel_class=mother_vessel.vessel_class,
            node_id=sts_hub_id,
            node_name=sts_node.name,
            operation="STS_TRANSFER",
            commodity=commodity,
            arrival_hour=round(sts_start_hr, 2),
            berth_acquired_hour=round(sts_start_hr + 4.0, 2),
            departure_hour=round(self.env.now, 2),
            queue_wait_hours=0.0,
            service_hours=round(4.0 + pump_hours, 2),
            volume_transferred_bbl=round(actual_sts_bbl, 0),
            port_or_canal_fee_usd=sts_fee_usd,
        )
        self.port_receipts.append(sts_receipt)

        self.log_event(
            category="MARITIME",
            severity="INFO",
            subject_id=sts_hub_id,
            message=(
                f"STS в {sts_node.name}: {feeder_vessel.name} перекачал {actual_sts_bbl:,.0f} барр. "
                f"({commodity.value}) на {mother_vessel.name}."
            ),
        )

        feeder_vessel.completed_voyages += 1
        feeder_vessel.active_contract_id = None
        feeder_vessel.state = VesselState.IDLE

        if contract is not None:
            contract.status = ContractStatus.IN_TRANSIT

        r_mother_dest = self.router.find_congestion_aware_route(
            origin_id=sts_hub_id,
            dest_id=import_port_id,
            vessel_class=mother_vessel.vessel_class,
            is_laden=True,
            alignment=mother_vessel.alignment,
        )
        unload_rec: Optional[PortServiceReceipt] = None
        if r_mother_dest:
            yield self.env.process(self.sail_along_route(mother_vessel, r_mother_dest["path_ids"]))
            unload_smo = self.port_smos[import_port_id]
            unload_rec = yield self.env.process(
                unload_smo.process_vessel_call(vessel=mother_vessel, operation="UNLOAD", commodity=commodity)
            )
            delivered_bbl = unload_rec.volume_transferred_bbl
            mother_vessel.completed_voyages += 1
            mother_vessel.total_cargo_delivered_bbl += delivered_bbl

            if contract is not None:
                key_imp = (import_port_id, commodity)
                self.scheduled_import_bbl[key_imp] = max(0.0, self.scheduled_import_bbl.get(key_imp, 0.0) - contract.target_volume_bbl)
                total_cost = (feeder_vessel.voyage_cost_usd + mother_vessel.voyage_cost_usd) - start_cost
                gross_margin = (delivered_bbl * contract.cif_price_usd_bbl) - (actual_sts_bbl * contract.fob_price_usd_bbl)
                net_profit = gross_margin - total_cost

                contract.delivered_volume_bbl = round(delivered_bbl, 0)
                contract.realized_net_profit_usd = round(net_profit, 0)
                contract.status = ContractStatus.COMPLETED
                contract.completed_hour = round(self.env.now, 2)

                mother_vessel.total_revenue_usd += delivered_bbl * contract.cif_price_usd_bbl
                mother_vessel.total_net_profit_usd += net_profit * 0.65
                feeder_vessel.total_net_profit_usd += net_profit * 0.35

        mother_vessel.active_contract_id = None
        mother_vessel.state = VesselState.IDLE

        return {
            "feeder_load_receipt": feeder_load_rec,
            "sts_transfer_receipt": sts_receipt,
            "mother_unload_receipt": unload_rec,
        }

    # =========================================================================
    # КОНТУР 5: БИРЖА КОНТРАКТОВ И ДИСПЕТЧЕР ФЛОТА
    # =========================================================================
    def compute_export_fob_price(self, export_node: WorldNode, commodity: CommodityType) -> float:
        return self.market.get_export_fob_price(export_node, commodity)

    def compute_import_cif_price(self, import_node: WorldNode, commodity: CommodityType, eff_fill_ratio: float) -> float:
        return self.market.get_import_cif_price(import_node, commodity, eff_fill_ratio)

    def scan_and_publish_refinery_tenders(self) -> List[CargoContract]:
        new_contracts: List[CargoContract] = []
        export_ports = [
            n for n in self.world.nodes.values() if n.node_type == NodeType.EXPORT_TERMINAL and not n.is_blocked
        ]

        for imp_node in self.world.nodes.values():
            if imp_node.node_type != NodeType.IMPORT_TERMINAL or imp_node.berth_count == 0 or imp_node.is_blocked:
                continue

            for commodity, imp_tank in imp_node.storage.items():
                in_transit = self.scheduled_import_bbl.get((imp_node.id, commodity), 0.0)
                effective_level = imp_tank.current_level_bbl + in_transit
                eff_ratio = effective_level / imp_tank.capacity_bbl

                if eff_ratio >= 0.72:
                    continue

                already_open = any(
                    c.buyer_node_id == imp_node.id and c.commodity == commodity and c.status == ContractStatus.OPEN_TENDER
                    for c in self.contracts.values()
                )
                if already_open:
                    continue

                best_seller: Optional[WorldNode] = None
                best_avail_bbl = 0.0
                for exp_node in export_ports:
                    if commodity not in exp_node.storage:
                        continue
                    exp_tank = exp_node.storage[commodity]
                    reserved = self.reserved_export_bbl.get((exp_node.id, commodity), 0.0)
                    unreserved_bbl = exp_tank.current_level_bbl - reserved
                    if unreserved_bbl >= 680_000.0 and unreserved_bbl > best_avail_bbl:
                        best_seller = exp_node
                        best_avail_bbl = unreserved_bbl

                if best_seller is None:
                    continue

                self._contract_seq += 1
                cid = f"CNT_{self._contract_seq:04d}"
                desired_batch = min(2_000_000.0, imp_tank.capacity_bbl - effective_level, best_avail_bbl * 0.85)
                desired_batch = max(700_000.0, round(desired_batch, -4))

                fob = self.compute_export_fob_price(best_seller, commodity)
                cif = self.compute_import_cif_price(imp_node, commodity, eff_ratio)

                contract = CargoContract(
                    id=cid,
                    seller_node_id=best_seller.id,
                    buyer_node_id=imp_node.id,
                    commodity=commodity,
                    target_volume_bbl=desired_batch,
                    fob_price_usd_bbl=fob,
                    cif_price_usd_bbl=cif,
                    status=ContractStatus.OPEN_TENDER,
                    created_hour=round(self.env.now, 2),
                )
                self.contracts[cid] = contract
                new_contracts.append(contract)

        return new_contracts

    def dispatch_idle_vessels_to_contracts(self) -> List[Tuple[CargoContract, TankerAgentModel]]:
        assignments: List[Tuple[CargoContract, TankerAgentModel]] = []
        open_contracts = [c for c in self.contracts.values() if c.status == ContractStatus.OPEN_TENDER]
        open_contracts.sort(key=lambda c: c.cif_price_usd_bbl - c.fob_price_usd_bbl, reverse=True)

        idle_vessels = [v for v in self.vessels.values() if v.state == VesselState.IDLE and v.active_contract_id is None]

        for contract in open_contracts:
            seller_node = self.world.nodes[contract.seller_node_id]
            buyer_node = self.world.nodes[contract.buyer_node_id]
            exp_tank = seller_node.storage.get(contract.commodity)
            if not exp_tank:
                continue

            reserved = self.reserved_export_bbl.get((seller_node.id, contract.commodity), 0.0)
            avail_export_bbl = exp_tank.current_level_bbl - reserved
            if avail_export_bbl < 650_000.0:
                continue

            best_vessel: Optional[TankerAgentModel] = None
            best_netback_usd = -1e12
            best_logistics_usd = 0.0
            best_load_vol = 0.0

            for vessel in idle_vessels:
                if not self.router.is_vessel_compatible_with_commodity(vessel.vessel_class, contract.commodity):
                    continue
                if not self.router.check_terminal_draft_compatibility(vessel.vessel_class, seller_node, is_laden=True):
                    continue
                if not self.router.check_terminal_draft_compatibility(vessel.vessel_class, buyer_node, is_laden=True):
                    continue

                compliant, _ = self.router.check_sanctions_compliance(
                    alignment=vessel.alignment,
                    export_node=seller_node,
                    import_node=buyer_node,
                    commodity=contract.commodity,
                    fob_price_usd=contract.fob_price_usd_bbl,
                )
                if not compliant:
                    continue

                ballast_cost = 0.0
                if vessel.current_node_id and vessel.current_node_id != seller_node.id:
                    b_route = self.router.find_congestion_aware_route(
                        origin_id=vessel.current_node_id,
                        dest_id=seller_node.id,
                        vessel_class=vessel.vessel_class,
                        is_laden=False,
                        alignment=vessel.alignment,
                        winter_arctic_freeze=self.winter_arctic_freeze,
                    )
                    if not b_route:
                        continue
                    ballast_cost = b_route["estimated_cost_usd"]

                l_route = self.router.find_congestion_aware_route(
                    origin_id=seller_node.id,
                    dest_id=buyer_node.id,
                    vessel_class=vessel.vessel_class,
                    is_laden=True,
                    alignment=vessel.alignment,
                    winter_arctic_freeze=self.winter_arctic_freeze,
                )
                if not l_route:
                    continue

                v_spec = VESSEL_SPECS[vessel.vessel_class]
                ship_vol = min(v_spec.capacity_bbl, contract.target_volume_bbl, avail_export_bbl)
                if ship_vol < v_spec.capacity_bbl * 0.55:
                    continue

                port_fees = 50_000.0 + (ship_vol * 0.08)
                total_logistics = ballast_cost + l_route["estimated_cost_usd"] + port_fees

                c_spec = COMMODITY_SPECS[contract.commodity]
                bog_loss_ratio = c_spec.boil_off_rate_daily * l_route["transit_days"]
                delivered_est = ship_vol * (1.0 - bog_loss_ratio)

                gross_trade_margin = (delivered_est * contract.cif_price_usd_bbl) - (ship_vol * contract.fob_price_usd_bbl)
                netback_usd = gross_trade_margin - total_logistics

                if netback_usd > best_netback_usd:
                    best_netback_usd = netback_usd
                    best_vessel = vessel
                    best_logistics_usd = total_logistics
                    best_load_vol = ship_vol

            if best_vessel is not None:
                idle_vessels.remove(best_vessel)
                contract.target_volume_bbl = round(best_load_vol, 0)
                contract.estimated_logistics_cost_usd = round(best_logistics_usd, 0)
                contract.expected_netback_usd = round(best_netback_usd, 0)
                contract.assigned_vessel_id = best_vessel.id
                contract.assigned_hour = round(self.env.now, 2)
                contract.status = ContractStatus.ASSIGNED

                best_vessel.active_contract_id = contract.id

                key_exp = (seller_node.id, contract.commodity)
                key_imp = (buyer_node.id, contract.commodity)
                self.reserved_export_bbl[key_exp] = self.reserved_export_bbl.get(key_exp, 0.0) + best_load_vol
                self.scheduled_import_bbl[key_imp] = self.scheduled_import_bbl.get(key_imp, 0.0) + best_load_vol

                self.log_event(
                    category="MARKET",
                    severity="INFO",
                    subject_id=contract.id,
                    message=(
                        f"Контракт {contract.id}: {best_vessel.name} отправлен за {best_load_vol:,.0f} барр. "
                        f"{contract.commodity.value} ({seller_node.name} -> {buyer_node.name})."
                    ),
                    financial_impact_usd=best_netback_usd,
                )

                self.env.process(
                    self.execute_full_delivery_mission(
                        vessel=best_vessel,
                        load_port_id=seller_node.id,
                        unload_port_id=buyer_node.id,
                        commodity=contract.commodity,
                        volume_bbl=best_load_vol,
                        contract=contract,
                    )
                )
                assignments.append((contract, best_vessel))

        return assignments

    def _contract_exchange_loop(self) -> Generator[simpy.Event, None, None]:
        while True:
            self.scan_and_publish_refinery_tenders()
            self.dispatch_idle_vessels_to_contracts()
            yield self.env.timeout(6.0)

    def get_telemetry_snapshot(self) -> Dict:
        active_ships = sum(1 for v in self.vessels.values() if v.state != VesselState.IDLE)
        completed_cnt = sum(1 for c in self.contracts.values() if c.status == ContractStatus.COMPLETED)
        return {
            "sim_hour": round(self.env.now, 2),
            "sim_day": int(self.env.now // 24) + 1,
            "total_produced_mbbl": round(sum(n.cumulative_produced_bbl for n in self.world.nodes.values()) / 1e6, 3),
            "total_consumed_mbbl": round(sum(n.cumulative_consumed_bbl for n in self.world.nodes.values()) / 1e6, 3),
            "total_pumped_mbbl": round(
                sum(attr["data"].cumulative_pumped_bbl for _, _, attr in self.world.pipeline_graph.edges(data=True)) / 1e6, 3
            ),
            "total_demurrage_usd": round(sum(n.cumulative_demurrage_usd for n in self.world.nodes.values()), 0),
            "fleet_total": len(self.vessels),
            "fleet_active": active_ships,
            "contracts_completed": completed_cnt,
            "fleet_net_profit_musd": round(sum(v.total_net_profit_usd for v in self.vessels.values()) / 1e6, 2),
            "spot_brent_usd": self.market.spot_prices[CommodityType.BRENT],
            "spot_urals_usd": self.market.spot_prices[CommodityType.URAL],
            "spot_lng_usd": self.market.spot_prices[CommodityType.LNG],
            "vlcc_charter_usd_day": self.market.charter_rates[VesselClass.VLCC],
            "active_cataclysms": len(self.chaos.active_cataclysms),
        }

    def run(self, until_hours: float = 24.0) -> Dict:
        target_time = self.env.now + until_hours
        self.env.run(until=target_time)
        return self.get_telemetry_snapshot()
