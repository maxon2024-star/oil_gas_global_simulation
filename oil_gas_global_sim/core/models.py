# core/models.py
from enum import Enum
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field
from core.config import CommodityType, VesselClass


class NodeType(str, Enum):
    EXTRACTION_FIELD = "EXTRACTION_FIELD"
    EXPORT_TERMINAL = "EXPORT_TERMINAL"
    IMPORT_TERMINAL = "IMPORT_TERMINAL"
    PIPELINE_HUB = "PIPELINE_HUB"
    CHOKEPOINT = "CHOKEPOINT"
    WAYPOINT = "WAYPOINT"
    STS_ZONE = "STS_ZONE"


class EdgeType(str, Enum):
    MARITIME = "MARITIME"
    PIPELINE = "PIPELINE"


class VesselState(str, Enum):
    IDLE = "IDLE"
    BALLAST_TRANSIT = "BALLAST_TRANSIT"
    QUEUED_FOR_LOAD = "QUEUED_FOR_LOAD"
    LOADING = "LOADING"
    LADEN_TRANSIT = "LADEN_TRANSIT"
    QUEUED_FOR_UNLOAD = "QUEUED_FOR_UNLOAD"
    UNLOADING = "UNLOADING"
    STS_TRANSFER = "STS_TRANSFER"
    MAINTENANCE = "MAINTENANCE"
    ADRIFT_DAMAGED = "ADRIFT_DAMAGED"


class FleetAlignment(str, Enum):
    WESTERN_INSURED = "WESTERN_INSURED"
    SHADOW_FLEET = "SHADOW_FLEET"
    NEUTRAL_STATE = "NEUTRAL_STATE"


class WellOperationalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    SHUT_IN_TANK_TOP = "SHUT_IN_TANK_TOP"
    OFFLINE_DAMAGED = "OFFLINE_DAMAGED"


class PipelineBottleneck(str, Enum):
    OK = "OK"
    SOURCE_DEPLETED = "SOURCE_DEPLETED"
    TARGET_FULL_BACKPRESSURE = "TARGET_FULL_BACKPRESSURE"
    REDUCED_INTEGRITY = "REDUCED_INTEGRITY"
    SHUTDOWN = "SHUTDOWN"


class ContractStatus(str, Enum):
    OPEN_TENDER = "OPEN_TENDER"
    ASSIGNED = "ASSIGNED"
    IN_TRANSIT = "IN_TRANSIT"
    STS_RENDEZVOUS = "STS_RENDEZVOUS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CataclysmType(str, Enum):
    HURRICANE_STORM = "HURRICANE_STORM"
    ARCTIC_ICE_LOCK = "ARCTIC_ICE_LOCK"
    STRAIT_MILITARY_BLOCKADE = "STRAIT_BLOCKADE"
    PIPELINE_SABOTAGE = "PIPELINE_SABOTAGE"
    TERMINAL_FIRE_OUTAGE = "TERMINAL_OUTAGE"
    SANCTIONS_PRICE_CAP_SHOCK = "SANCTIONS_SHOCK"


class StorageTank(BaseModel):
    """Резервуарный парк в узле для конкретного сорта сырья"""
    commodity: CommodityType
    capacity_bbl: float = Field(gt=0)
    current_level_bbl: float = Field(ge=0)

    total_inflow_bbl: float = 0.0
    total_outflow_bbl: float = 0.0
    overflow_blocked_events: int = 0
    dryout_events: int = 0

    @property
    def fill_ratio(self) -> float:
        return self.current_level_bbl / self.capacity_bbl

    @property
    def free_space_bbl(self) -> float:
        return max(0.0, self.capacity_bbl - self.current_level_bbl)

    def add_volume(self, requested_bbl: float) -> float:
        if requested_bbl <= 0.0:
            return 0.0
        accepted = min(requested_bbl, self.free_space_bbl)
        self.current_level_bbl += accepted
        self.total_inflow_bbl += accepted
        if accepted < requested_bbl - 1e-3:
            self.overflow_blocked_events += 1
        return accepted

    def remove_volume(self, requested_bbl: float) -> float:
        if requested_bbl <= 0.0:
            return 0.0
        drawn = min(requested_bbl, self.current_level_bbl)
        self.current_level_bbl -= drawn
        self.total_outflow_bbl += drawn
        if drawn < requested_bbl - 1e-3:
            self.dryout_events += 1
        return drawn


class WorldNode(BaseModel):
    """Универсальный гео-узел на 3D-глобусе"""
    id: str
    name: str
    country: str
    node_type: NodeType
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)

    primary_commodity: Optional[CommodityType] = None
    production_rate_bbl_hr: float = 0.0
    consumption_rate_bbl_hr: float = 0.0
    extraction_cost_usd_bbl: float = 0.0

    berth_count: int = 0
    max_loading_rate_bbl_hr: float = 0.0
    max_draft_m: float = 99.0
    supports_lng: bool = False

    canal_Wait_hours: float = 0.0
    canal_toll_usd: float = 0.0
    max_daily_vessels: int = 999

    is_blocked: bool = False
    sanctioned_for_western_fleet: bool = False
    weather_efficiency: float = 1.0
    storage: Dict[CommodityType, StorageTank] = Field(default_factory=dict)

    well_status: WellOperationalStatus = WellOperationalStatus.ACTIVE
    shut_in_cooldown_hours: float = 0.0
    current_production_bbl_hr: float = 0.0
    current_consumption_bbl_hr: float = 0.0
    cumulative_produced_bbl: float = 0.0
    cumulative_consumed_bbl: float = 0.0
    unmet_demand_bbl: float = 0.0
    shut_in_penalty_usd: float = 0.0

    occupied_berths: int = 0
    queued_vessels: int = 0
    total_vessels_served: int = 0
    cumulative_demurrage_usd: float = 0.0
    cumulative_port_fees_usd: float = 0.0


class TransportEdge(BaseModel):
    """Ребро графа: либо морской коридор, либо магистральный трубопровод"""
    source_id: str
    target_id: str
    edge_type: EdgeType
    name: str = ""
    distance_nm: float = 0.0

    max_draft_m: float = 99.0
    canal_fee_usd: float = 0.0
    requires_ice_class: bool = False
    piracy_or_war_risk: float = 0.0
    weather_speed_factor: float = 1.0

    allowed_commodity: Optional[CommodityType] = None
    max_flow_bbl_hr: float = 0.0
    current_flow_bbl_hr: float = 0.0
    tariff_usd_bbl: float = 0.0
    integrity_pct: float = 100.0
    is_active: bool = True

    bottleneck: PipelineBottleneck = PipelineBottleneck.OK
    cumulative_pumped_bbl: float = 0.0
    cumulative_tariff_usd: float = 0.0


class TankerAgentModel(BaseModel):
    """Модель состояния отдельного танкера для телеметрии и 3D-рендера"""
    id: str
    name: str
    vessel_class: VesselClass
    alignment: FleetAlignment
    flag_country: str

    current_lat: float
    current_lon: float
    heading_deg: float = 0.0
    current_speed_knots: float = 0.0
    current_node_id: Optional[str] = None
    destination_node_id: Optional[str] = None
    planned_route: List[str] = Field(default_factory=list)

    state: VesselState = VesselState.IDLE
    cargo_type: Optional[CommodityType] = None
    cargo_volume_bbl: float = 0.0
    accumulated_demurrage_usd: float = 0.0
    accumulated_boil_off_bbl: float = 0.0
    bunker_consumed_tons: float = 0.0
    voyage_cost_usd: float = 0.0
    total_distance_sailed_nm: float = 0.0

    active_contract_id: Optional[str] = None
    completed_voyages: int = 0
    total_cargo_delivered_bbl: float = 0.0
    total_revenue_usd: float = 0.0
    total_net_profit_usd: float = 0.0
    miles_since_maintenance: float = 0.0
    rerouted_count: int = 0  # Счётчик экстренных перестроений маршрута в море (Этап 4)


class CargoContract(BaseModel):
    """Фрахтовый и торговый контракт на доставку партии углеводородов (Этап 3)"""
    id: str
    seller_node_id: str
    buyer_node_id: str
    sts_hub_id: Optional[str] = None
    commodity: CommodityType
    target_volume_bbl: float
    delivered_volume_bbl: float = 0.0

    fob_price_usd_bbl: float
    cif_price_usd_bbl: float
    estimated_logistics_cost_usd: float = 0.0
    expected_netback_usd: float = 0.0
    realized_net_profit_usd: float = 0.0

    status: ContractStatus = ContractStatus.OPEN_TENDER
    assigned_vessel_id: Optional[str] = None
    feeder_vessel_id: Optional[str] = None
    created_hour: float = 0.0
    assigned_hour: Optional[float] = None
    completed_hour: Optional[float] = None


class CataclysmEvent(BaseModel):
    """Активный природный или геополитический катаклизм с полигоном для 3D-Земли (Этап 4)"""
    id: str
    name: str
    cataclysm_type: CataclysmType
    severity: float = Field(ge=0.0, le=1.0)
    center_lat: float
    center_lon: float
    radius_nm: float
    start_hour: float
    duration_hours: float
    remaining_hours: float
    is_active: bool = True

    affected_node_ids: List[str] = Field(default_factory=list)
    affected_pipelines: List[Tuple[str, str]] = Field(default_factory=list)
    polygon_coords: List[Tuple[float, float]] = Field(default_factory=list)
    description: str = ""


class MarketQuoteSnapshot(BaseModel):
    """Слепок мировых спотовых котировок сырья, дисконтов и ставок фрахта (Этап 4)"""
    sim_hour: float
    sim_day: int
    spot_prices_usd: Dict[CommodityType, float]
    urals_espo_discount_usd: float
    g7_price_cap_usd: float
    bunker_vlsfo_usd_ton: float
    charter_rates_usd_day: Dict[VesselClass, float]
    global_war_risk_index: float
    supply_disruption_pct: float


class PortServiceReceipt(BaseModel):
    """Квитанция обслуживания судна в порту или проливе (СМО)"""
    vessel_id: str
    vessel_name: str
    vessel_class: VesselClass
    node_id: str
    node_name: str
    operation: str  # LOAD, UNLOAD, CANAL_TRANSIT, STS_TRANSFER
    commodity: Optional[CommodityType] = None
    arrival_hour: float
    berth_acquired_hour: float
    departure_hour: float
    queue_wait_hours: float
    service_hours: float
    volume_transferred_bbl: float = 0.0
    boil_off_loss_bbl: float = 0.0
    demurrage_incurred_usd: float = 0.0
    port_or_canal_fee_usd: float = 0.0


class SimEventLog(BaseModel):
    """Запись в оперативном журнале ситуационного центра"""
    sim_hour: float
    sim_day: int
    category: str  # UPSTREAM, PIPELINE, PORT_SMO, MARITIME, REFINERY, MARKET, CHAOS
    severity: str  # INFO, WARNING, CRITICAL
    subject_id: str
    message: str
    financial_impact_usd: float = 0.0
