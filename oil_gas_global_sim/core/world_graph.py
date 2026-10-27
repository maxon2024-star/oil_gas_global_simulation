# core/world_graph.py
import math
from typing import Dict, List, Optional, Tuple
import networkx as nx

from core.config import (
    BUNKER_FUEL_PRICE_USD_TON,
    COMMODITY_SPECS,
    EARTH_RADIUS_NM,
    VESSEL_SPECS,
    CommodityType,
    VesselClass,
)
from core.models import (
    EdgeType,
    FleetAlignment,
    NodeType,
    StorageTank,
    TransportEdge,
    WorldNode,
)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Точный расчет ортодромии (расстояния по сфере Земли) в морских милях"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_NM * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class WorldLogisticsGraph:
    """
    Двухконтурный граф мира:
    1. maritime_graph (nx.DiGraph) — океанские маршруты, проливы, каналы и порты.
    2. pipeline_graph (nx.DiGraph) — сухопутные нефте- и газопроводы.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, WorldNode] = {}
        self.maritime_graph = nx.DiGraph()
        self.pipeline_graph = nx.DiGraph()
        self._build_default_world()

    def add_node(self, node: WorldNode) -> None:
        self.nodes[node.id] = node
        self.maritime_graph.add_node(node.id, data=node)
        self.pipeline_graph.add_node(node.id, data=node)

    def add_maritime_leg(
        self,
        u: str,
        v: str,
        name: str = "",
        max_draft_m: float = 99.0,
        canal_fee_usd: float = 0.0,
        requires_ice_class: bool = False,
        piracy_or_war_risk: float = 0.0,
        bidirectional: bool = True,
        detour_multiplier: float = 1.08,  # Коэффициент огибания береговой линии
    ) -> None:
        n1, n2 = self.nodes[u], self.nodes[v]
        dist_nm = haversine_nm(n1.lat, n1.lon, n2.lat, n2.lon) * detour_multiplier

        edge_fwd = TransportEdge(
            source_id=u,
            target_id=v,
            edge_type=EdgeType.MARITIME,
            name=name or f"{n1.name} -> {n2.name}",
            distance_nm=round(dist_nm, 1),
            max_draft_m=min(max_draft_m, n1.max_draft_m, n2.max_draft_m),
            canal_fee_usd=canal_fee_usd,
            requires_ice_class=requires_ice_class,
            piracy_or_war_risk=piracy_or_war_risk,
        )
        self.maritime_graph.add_edge(u, v, data=edge_fwd)

        if bidirectional:
            edge_rev = edge_fwd.model_copy(
                update={
                    "source_id": v,
                    "target_id": u,
                    "name": name or f"{n2.name} -> {n1.name}",
                }
            )
            self.maritime_graph.add_edge(v, u, data=edge_rev)

    def add_pipeline_segment(
        self,
        u: str,
        v: str,
        name: str,
        commodity: CommodityType,
        max_flow_bbl_hr: float,
        tariff_usd_bbl: float = 1.2,
    ) -> None:
        n1, n2 = self.nodes[u], self.nodes[v]
        dist_nm = haversine_nm(n1.lat, n1.lon, n2.lat, n2.lon) * 1.15
        edge = TransportEdge(
            source_id=u,
            target_id=v,
            edge_type=EdgeType.PIPELINE,
            name=name,
            distance_nm=round(dist_nm, 1),
            allowed_commodity=commodity,
            max_flow_bbl_hr=max_flow_bbl_hr,
            tariff_usd_bbl=tariff_usd_bbl,
        )
        self.pipeline_graph.add_edge(u, v, data=edge)

    def _build_default_world(self) -> None:
        """Инициализация реальных мировых месторождений, терминалов, проливов и труб"""

        # =====================================================================
        # 1. МЕСТОРОЖДЕНИЯ (EXTRACTION FIELDS)
        # =====================================================================
        fields = [
            WorldNode(
                id="FLD_WEST_SIBERIA", name="Самотлор / Западная Сибирь", country="RU",
                node_type=NodeType.EXTRACTION_FIELD, lat=61.15, lon=76.70,
                primary_commodity=CommodityType.URAL, production_rate_bbl_hr=140_000,
                extraction_cost_usd_bbl=15.0,
                storage={
                    CommodityType.URAL: StorageTank(commodity=CommodityType.URAL, capacity_bbl=12_000_000, current_level_bbl=5_000_000),
                    CommodityType.ESPO: StorageTank(commodity=CommodityType.ESPO, capacity_bbl=8_000_000, current_level_bbl=3_500_000),
                },
            ),
            WorldNode(
                id="FLD_GHAWAR", name="Ghawar Super-Field", country="SA",
                node_type=NodeType.EXTRACTION_FIELD, lat=25.43, lon=49.62,
                primary_commodity=CommodityType.ARAB_LIGHT, production_rate_bbl_hr=160_000,
                extraction_cost_usd_bbl=8.5,
                storage={
                    CommodityType.ARAB_LIGHT: StorageTank(commodity=CommodityType.ARAB_LIGHT, capacity_bbl=15_000_000, current_level_bbl=6_000_000)
                },
            ),
            WorldNode(
                id="FLD_PERMIAN", name="Permian Basin (Texas)", country="US",
                node_type=NodeType.EXTRACTION_FIELD, lat=31.99, lon=102.07 * -1,
                primary_commodity=CommodityType.WTI, production_rate_bbl_hr=150_000,
                extraction_cost_usd_bbl=34.0,
                storage={
                    CommodityType.WTI: StorageTank(commodity=CommodityType.WTI, capacity_bbl=14_000_000, current_level_bbl=6_500_000)
                },
            ),
            WorldNode(
                id="FLD_KASHAGAN", name="Кашаган / Тенгиз", country="KZ",
                node_type=NodeType.EXTRACTION_FIELD, lat=46.16, lon=51.59,
                primary_commodity=CommodityType.KASHAGAN_CPC, production_rate_bbl_hr=65_000,
                extraction_cost_usd_bbl=22.0,
                storage={
                    CommodityType.KASHAGAN_CPC: StorageTank(commodity=CommodityType.KASHAGAN_CPC, capacity_bbl=6_000_000, current_level_bbl=2_500_000)
                },
            ),
            # Экспортные терминалы со встроенной добычей / заводами СПГ на побережье
            WorldNode(
                id="PORT_YAMAL_SABETTA", name="Сабетта (Ямал СПГ)", country="RU",
                node_type=NodeType.EXPORT_TERMINAL, lat=71.27, lon=72.07,
                primary_commodity=CommodityType.LNG, production_rate_bbl_hr=45_000,
                berth_count=3, max_loading_rate_bbl_hr=42_000, max_draft_m=15.0,
                supports_lng=True, sanctioned_for_western_fleet=True,
                storage={
                    CommodityType.LNG: StorageTank(commodity=CommodityType.LNG, capacity_bbl=6_000_000, current_level_bbl=3_200_000)
                },
            ),
            WorldNode(
                id="PORT_RAS_LAFFAN", name="Ras Laffan LNG Hub", country="QA",
                node_type=NodeType.EXPORT_TERMINAL, lat=25.93, lon=51.53,
                primary_commodity=CommodityType.LNG, production_rate_bbl_hr=95_000,
                berth_count=6, max_loading_rate_bbl_hr=55_000, max_draft_m=15.5,
                supports_lng=True,
                storage={
                    CommodityType.LNG: StorageTank(commodity=CommodityType.LNG, capacity_bbl=12_000_000, current_level_bbl=6_000_000)
                },
            ),
            WorldNode(
                id="PORT_BERGEN_TROLL", name="Mongstad / North Sea", country="NO",
                node_type=NodeType.EXPORT_TERMINAL, lat=60.81, lon=5.03,
                primary_commodity=CommodityType.BRENT, production_rate_bbl_hr=75_000,
                berth_count=4, max_loading_rate_bbl_hr=50_000, max_draft_m=22.0,
                storage={
                    CommodityType.BRENT: StorageTank(commodity=CommodityType.BRENT, capacity_bbl=9_000_000, current_level_bbl=4_000_000)
                },
            ),
            WorldNode(
                id="PORT_SANTOS_BUZIOS", name="Santos Basin Offshore", country="BR",
                node_type=NodeType.EXPORT_TERMINAL, lat=-24.00, lon=-46.33,
                primary_commodity=CommodityType.BRENT, production_rate_bbl_hr=80_000,
                berth_count=4, max_loading_rate_bbl_hr=48_000, max_draft_m=23.0,
                storage={
                    CommodityType.BRENT: StorageTank(commodity=CommodityType.BRENT, capacity_bbl=8_500_000, current_level_bbl=3_800_000)
                },
            ),
        ]

        # =====================================================================
        # 2. ЭКСПОРТНЫЕ МОРСКИЕ ТЕРМИНАЛЫ (EXPORT TERMINALS)
        # =====================================================================
        export_ports = [
            WorldNode(
                id="PORT_PRIMORSK", name="Приморск (Балтика)", country="RU",
                node_type=NodeType.EXPORT_TERMINAL, lat=60.35, lon=28.62,
                primary_commodity=CommodityType.URAL, berth_count=4,
                max_loading_rate_bbl_hr=45_000, max_draft_m=15.0,  # Ограничение Балтики (Aframax/Suezmax с недогрузом)
                sanctioned_for_western_fleet=True,
                storage={
                    CommodityType.URAL: StorageTank(commodity=CommodityType.URAL, capacity_bbl=10_000_000, current_level_bbl=5_500_000)
                },
            ),
            WorldNode(
                id="PORT_NOVOROSSIYSK", name="Новороссийск / КТК", country="RU",
                node_type=NodeType.EXPORT_TERMINAL, lat=44.72, lon=37.77,
                primary_commodity=CommodityType.URAL, berth_count=5,
                max_loading_rate_bbl_hr=50_000, max_draft_m=19.0,
                sanctioned_for_western_fleet=True,
                storage={
                    CommodityType.URAL: StorageTank(commodity=CommodityType.URAL, capacity_bbl=8_000_000, current_level_bbl=4_000_000),
                    CommodityType.KASHAGAN_CPC: StorageTank(commodity=CommodityType.KASHAGAN_CPC, capacity_bbl=7_000_000, current_level_bbl=3_500_000),
                },
            ),
            WorldNode(
                id="PORT_KOZMINO", name="Козьмино (ВСТО / Находка)", country="RU",
                node_type=NodeType.EXPORT_TERMINAL, lat=42.73, lon=133.00,
                primary_commodity=CommodityType.ESPO, berth_count=3,
                max_loading_rate_bbl_hr=48_000, max_draft_m=21.0,
                sanctioned_for_western_fleet=True,
                storage={
                    CommodityType.ESPO: StorageTank(commodity=CommodityType.ESPO, capacity_bbl=9_000_000, current_level_bbl=4_800_000)
                },
            ),
            WorldNode(
                id="PORT_RAS_TANURA", name="Ras Tanura (Persian Gulf)", country="SA",
                node_type=NodeType.EXPORT_TERMINAL, lat=26.64, lon=50.16,
                primary_commodity=CommodityType.ARAB_LIGHT, berth_count=8,
                max_loading_rate_bbl_hr=80_000, max_draft_m=24.0,  # Принимает любые VLCC
                storage={
                    CommodityType.ARAB_LIGHT: StorageTank(commodity=CommodityType.ARAB_LIGHT, capacity_bbl=22_000_000, current_level_bbl=12_000_000)
                },
            ),
            WorldNode(
                id="PORT_YANBU", name="Yanbu (Red Sea Terminal)", country="SA",
                node_type=NodeType.EXPORT_TERMINAL, lat=24.08, lon=38.06,
                primary_commodity=CommodityType.ARAB_LIGHT, berth_count=4,
                max_loading_rate_bbl_hr=60_000, max_draft_m=23.0,
                storage={
                    CommodityType.ARAB_LIGHT: StorageTank(commodity=CommodityType.ARAB_LIGHT, capacity_bbl=11_000_000, current_level_bbl=4_000_000)
                },
            ),
            WorldNode(
                id="PORT_HOUSTON", name="Houston / Corpus Christi", country="US",
                node_type=NodeType.EXPORT_TERMINAL, lat=29.73, lon=-95.02,
                primary_commodity=CommodityType.WTI, berth_count=7,
                max_loading_rate_bbl_hr=65_000, max_draft_m=22.0, supports_lng=True,
                storage={
                    CommodityType.WTI: StorageTank(commodity=CommodityType.WTI, capacity_bbl=18_000_000, current_level_bbl=9_500_000),
                    CommodityType.LNG: StorageTank(commodity=CommodityType.LNG, capacity_bbl=8_000_000, current_level_bbl=4_000_000),
                },
            ),
        ]

        # =====================================================================
        # 3. ИМПОРТНЫЕ ХАБЫ И НПЗ (IMPORT TERMINALS / REFINERIES)
        # =====================================================================
        import_ports = [
            WorldNode(
                id="PORT_ROTTERDAM", name="Rotterdam Europort (EU Hub)", country="NL",
                node_type=NodeType.IMPORT_TERMINAL, lat=51.95, lon=4.14,
                consumption_rate_bbl_hr=135_000, berth_count=8,
                max_loading_rate_bbl_hr=75_000, max_draft_m=24.0, supports_lng=True,
                storage={
                    c: StorageTank(commodity=c, capacity_bbl=16_000_000, current_level_bbl=6_000_000)
                    for c in [CommodityType.BRENT, CommodityType.WTI, CommodityType.ARAB_LIGHT, CommodityType.LNG, CommodityType.KASHAGAN_CPC]
                },
            ),
            WorldNode(
                id="PORT_SCHWEDT", name="НПЗ Шведт / Центральная Европа", country="DE",
                node_type=NodeType.IMPORT_TERMINAL, lat=53.06, lon=14.28,
                consumption_rate_bbl_hr=38_000, berth_count=0,
                storage={
                    CommodityType.URAL: StorageTank(commodity=CommodityType.URAL, capacity_bbl=5_000_000, current_level_bbl=2_000_000),
                    CommodityType.BRENT: StorageTank(commodity=CommodityType.BRENT, capacity_bbl=5_000_000, current_level_bbl=1_500_000),
                },
            ),
            WorldNode(
                id="PORT_JAMNAGAR", name="Jamnagar / Sikka (Reliance Hub)", country="IN",
                node_type=NodeType.IMPORT_TERMINAL, lat=22.47, lon=69.85,
                consumption_rate_bbl_hr=125_000, berth_count=6,
                max_loading_rate_bbl_hr=70_000, max_draft_m=24.0, supports_lng=True,
                storage={
                    c: StorageTank(commodity=c, capacity_bbl=15_000_000, current_level_bbl=5_000_000)
                    for c in [CommodityType.URAL, CommodityType.ARAB_LIGHT, CommodityType.ESPO, CommodityType.LNG]
                },
            ),
            WorldNode(
                id="PORT_NINGBO", name="Ningbo-Zhoushan (China Mega-Hub)", country="CN",
                node_type=NodeType.IMPORT_TERMINAL, lat=29.87, lon=121.85,
                consumption_rate_bbl_hr=175_000, berth_count=9,
                max_loading_rate_bbl_hr=85_000, max_draft_m=24.5, supports_lng=True,
                storage={
                    c: StorageTank(commodity=c, capacity_bbl=22_000_000, current_level_bbl=8_000_000)
                    for c in [CommodityType.ESPO, CommodityType.ARAB_LIGHT, CommodityType.URAL, CommodityType.BRENT, CommodityType.LNG]
                },
            ),
            WorldNode(
                id="PORT_SINGAPORE", name="Jurong / Singapore Hub", country="SG",
                node_type=NodeType.IMPORT_TERMINAL, lat=1.26, lon=103.82,
                consumption_rate_bbl_hr=95_000, berth_count=7,
                max_loading_rate_bbl_hr=65_000, max_draft_m=22.5, supports_lng=True,
                storage={
                    c: StorageTank(commodity=c, capacity_bbl=14_000_000, current_level_bbl=5_500_000)
                    for c in [CommodityType.ARAB_LIGHT, CommodityType.ESPO, CommodityType.BRENT, CommodityType.LNG]
                },
            ),
            WorldNode(
                id="PORT_YOKOHAMA", name="Tokyo Bay / Yokohama", country="JP",
                node_type=NodeType.IMPORT_TERMINAL, lat=35.44, lon=139.65,
                consumption_rate_bbl_hr=85_000, berth_count=5,
                max_loading_rate_bbl_hr=60_000, max_draft_m=22.0, supports_lng=True,
                storage={
                    c: StorageTank(commodity=c, capacity_bbl=12_000_000, current_level_bbl=4_800_000)
                    for c in [CommodityType.ARAB_LIGHT, CommodityType.WTI, CommodityType.LNG]
                },
            ),
        ]

        # =====================================================================
        # 4. УЗКИЕ ПРОЛИВЫ, КАНАЛЫ И ЗОНЫ STS (CHOKEPOINTS & WAYPOINTS)
        # =====================================================================
        waypoints = [
            # Ключевые проливы и каналы (с реальными ограничениями осадки!)
            WorldNode(
                id="CHK_DANISH_STRAITS", name="Датские проливы (Большой Бельт)", country="DK",
                node_type=NodeType.CHOKEPOINT, lat=55.34, lon=10.97,
                max_draft_m=15.4, canal_Wait_hours=4.0,
            ),
            WorldNode(
                id="CHK_BOSPORUS", name="Пролив Босфор и Дарданеллы", country="TR",
                node_type=NodeType.CHOKEPOINT, lat=41.12, lon=29.08,
                max_draft_m=18.5, canal_Wait_hours=14.0, canal_toll_usd=45_000,
                max_daily_vessels=18,
            ),
            WorldNode(
                id="CHK_SUEZ", name="Суэцкий канал", country="EG",
                node_type=NodeType.CHOKEPOINT, lat=30.58, lon=32.34,
                max_draft_m=20.1,  # Груженый VLCC (21.5 м) НЕ ПРОХОДИТ!
                canal_Wait_hours=18.0, canal_toll_usd=420_000, max_daily_vessels=35,
            ),
            WorldNode(
                id="CHK_BAB_EL_MANDEB", name="Баб-эль-Мандебский пролив (Красное море)", country="YE",
                node_type=NodeType.CHOKEPOINT, lat=12.58, lon=43.33,
                max_draft_m=99.0, canal_Wait_hours=2.0,
            ),
            WorldNode(
                id="CHK_HORMUZ", name="Ормузский пролив", country="OM",
                node_type=NodeType.CHOKEPOINT, lat=26.56, lon=56.25,
                max_draft_m=99.0, canal_Wait_hours=2.0,
            ),
            WorldNode(
                id="CHK_MALACCA", name="Малаккский пролив", country="MY",
                node_type=NodeType.CHOKEPOINT, lat=2.50, lon=101.80,
                max_draft_m=20.5, canal_Wait_hours=3.0,
            ),
            # Океанские обходные точки и рейдовые хабы STS
            WorldNode(
                id="STS_KALAMATA", name="Зона STS Лаконикос (Греция)", country="GR",
                node_type=NodeType.STS_ZONE, lat=36.55, lon=22.75,
            ),
            WorldNode(
                id="STS_CEUTA_GIBRALTAR", name="Гибралтар / Зона STS Сеута", country="ES",
                node_type=NodeType.STS_ZONE, lat=35.95, lon=-5.35,
            ),
            WorldNode(
                id="WP_ENGLISH_CHANNEL", name="Ла-Манш (Па-де-Кале)", country="GB",
                node_type=NodeType.WAYPOINT, lat=50.85, lon=1.35,
            ),
            WorldNode(
                id="WP_CAPE_GOOD_HOPE", name="Мыс Доброй Надежды (ЮАР)", country="ZA",
                node_type=NodeType.WAYPOINT, lat=-34.83, lon=19.98,
            ),
            WorldNode(
                id="WP_ARABIAN_SEA", name="Аравийское море (Центр)", country="INT",
                node_type=NodeType.WAYPOINT, lat=16.50, lon=61.50,
            ),
            WorldNode(
                id="WP_SOUTH_CHINA_SEA", name="Южно-Китайское море", country="INT",
                node_type=NodeType.WAYPOINT, lat=14.50, lon=114.50,
            ),
            WorldNode(
                id="WP_MURMANSK_BARENTS", name="Баренцево море (Мурманск STS)", country="RU",
                node_type=NodeType.STS_ZONE, lat=69.50, lon=33.50,
            ),
            WorldNode(
                id="WP_BERING_STRAIT", name="Берингов пролив (Восточный сектор СМП)", country="RU",
                node_type=NodeType.CHOKEPOINT, lat=65.75, lon=-168.95,
            ),
        ]

        for n in fields + export_ports + import_ports + waypoints:
            self.add_node(n)

        # =====================================================================
        # 5. МАГИСТРАЛЬНЫЕ ТРУБОПРОВОДЫ (PIPELINE NETWORK)
        # =====================================================================
        # 1. БТС-2 (Западная Сибирь -> Приморск)
        self.add_pipeline_segment("FLD_WEST_SIBERIA", "PORT_PRIMORSK", "Балтийская трубопроводная система (БТС)", CommodityType.URAL, 55_000, 1.8)
        # 2. ВСТО / ESPO (Западная Сибирь -> Козьмино)
        self.add_pipeline_segment("FLD_WEST_SIBERIA", "PORT_KOZMINO", "Магистральный нефтепровод ВСТО (ESPO)", CommodityType.ESPO, 52_000, 2.4)
        # 3. Нефтепровод «Дружба» (Западная Сибирь -> Центральная Европа / Шведт)
        self.add_pipeline_segment("FLD_WEST_SIBERIA", "PORT_SCHWEDT", "Нефтепровод «Дружба» (Северная ветка)", CommodityType.URAL, 35_000, 1.5)
        # 4. Самара — Тихорецк — Новороссийск
        self.add_pipeline_segment("FLD_WEST_SIBERIA", "PORT_NOVOROSSIYSK", "Южный нефтепровод до Новороссийска", CommodityType.URAL, 32_000, 1.6)
        # 5. КТК (Каспийский трубопроводный консорциум: Кашаган/Тенгиз -> Новороссийск)
        self.add_pipeline_segment("FLD_KASHAGAN", "PORT_NOVOROSSIYSK", "Трубопровод КТК (Тенгиз — Новороссийск)", CommodityType.KASHAGAN_CPC, 62_000, 2.1)
        # 6. Саудовская Аравия: Ghawar -> Ras Tanura и стратегический East-West Petroline (в обход Ормуза до Янбу)
        self.add_pipeline_segment("FLD_GHAWAR", "PORT_RAS_TANURA", "Abqaiq — Ras Tanura Trunkline", CommodityType.ARAB_LIGHT, 120_000, 0.5)
        self.add_pipeline_segment("FLD_GHAWAR", "PORT_YANBU", "East-West Pipeline (Petroline)", CommodityType.ARAB_LIGHT, 65_000, 1.4)
        # 7. США: Permian Basin -> Houston
        self.add_pipeline_segment("FLD_PERMIAN", "PORT_HOUSTON", "Permian Highway / Wink-to-Webster", CommodityType.WTI, 130_000, 1.1)

        # =====================================================================
        # 6. МОРСКИЕ НАВИГАЦИОННЫЕ МАРШРУТЫ (MARITIME NETWORK)
        # =====================================================================
        # Северная Европа и Балтика
        self.add_maritime_leg("PORT_PRIMORSK", "CHK_DANISH_STRAITS", max_draft_m=15.4)
        self.add_maritime_leg("CHK_DANISH_STRAITS", "PORT_ROTTERDAM", max_draft_m=15.4)
        self.add_maritime_leg("PORT_BERGEN_TROLL", "PORT_ROTTERDAM")
        self.add_maritime_leg("PORT_ROTTERDAM", "WP_ENGLISH_CHANNEL")
        self.add_maritime_leg("WP_ENGLISH_CHANNEL", "STS_CEUTA_GIBRALTAR")

        # Арктика и Северный морской путь (СМП)
        self.add_maritime_leg("PORT_YAMAL_SABETTA", "WP_MURMANSK_BARENTS", requires_ice_class=True)
        self.add_maritime_leg("WP_MURMANSK_BARENTS", "PORT_BERGEN_TROLL")
        self.add_maritime_leg("PORT_YAMAL_SABETTA", "WP_BERING_STRAIT", name="Северный морской путь (Восток)", requires_ice_class=True, detour_multiplier=1.18)
        self.add_maritime_leg("WP_BERING_STRAIT", "PORT_KOZMINO")
        self.add_maritime_leg("WP_BERING_STRAIT", "PORT_YOKOHAMA")
        self.add_maritime_leg("WP_BERING_STRAIT", "PORT_NINGBO")

        # Черное и Средиземное моря
        self.add_maritime_leg("PORT_NOVOROSSIYSK", "CHK_BOSPORUS", max_draft_m=18.5, canal_fee_usd=45_000)
        self.add_maritime_leg("CHK_BOSPORUS", "STS_KALAMATA", max_draft_m=18.5)
        self.add_maritime_leg("STS_KALAMATA", "STS_CEUTA_GIBRALTAR")
        self.add_maritime_leg("STS_KALAMATA", "CHK_SUEZ", max_draft_m=20.1, canal_fee_usd=420_000)

        # Атлантика (Америка, Бразилия и обход Африки)
        self.add_maritime_leg("PORT_HOUSTON", "WP_ENGLISH_CHANNEL")
        self.add_maritime_leg("PORT_HOUSTON", "STS_CEUTA_GIBRALTAR")
        self.add_maritime_leg("PORT_HOUSTON", "WP_CAPE_GOOD_HOPE")
        self.add_maritime_leg("PORT_SANTOS_BUZIOS", "STS_CEUTA_GIBRALTAR")
        self.add_maritime_leg("PORT_SANTOS_BUZIOS", "WP_CAPE_GOOD_HOPE")
        self.add_maritime_leg("STS_CEUTA_GIBRALTAR", "WP_CAPE_GOOD_HOPE", detour_multiplier=1.12)

        # Красное море, Персидский залив и Индийский океан
        self.add_maritime_leg("CHK_SUEZ", "PORT_YANBU", max_draft_m=20.1)
        self.add_maritime_leg("PORT_YANBU", "CHK_BAB_EL_MANDEB", piracy_or_war_risk=0.15)
        self.add_maritime_leg("CHK_SUEZ", "CHK_BAB_EL_MANDEB", max_draft_m=20.1, piracy_or_war_risk=0.15)
        self.add_maritime_leg("CHK_BAB_EL_MANDEB", "WP_ARABIAN_SEA", piracy_or_war_risk=0.15)

        self.add_maritime_leg("PORT_RAS_TANURA", "CHK_HORMUZ")
        self.add_maritime_leg("PORT_RAS_LAFFAN", "CHK_HORMUZ")
        self.add_maritime_leg("CHK_HORMUZ", "WP_ARABIAN_SEA", piracy_or_war_risk=0.05)

        self.add_maritime_leg("WP_ARABIAN_SEA", "PORT_JAMNAGAR")
        self.add_maritime_leg("WP_ARABIAN_SEA", "WP_CAPE_GOOD_HOPE", detour_multiplier=1.10)
        self.add_maritime_leg("WP_ARABIAN_SEA", "CHK_MALACCA", max_draft_m=20.5)
        # Глубоководный обход для груженых VLCC (Ломбокский пролив -> Южно-Китайское море напрямую в обход лимита Малакки 20.5 м)
        self.add_maritime_leg("WP_ARABIAN_SEA", "WP_SOUTH_CHINA_SEA", name="Глубоководный маршрут через Зондский/Ломбокский пролив (VLCC)", max_draft_m=25.0, detour_multiplier=1.22)
        self.add_maritime_leg("WP_CAPE_GOOD_HOPE", "CHK_MALACCA", max_draft_m=20.5)
        self.add_maritime_leg("WP_CAPE_GOOD_HOPE", "WP_SOUTH_CHINA_SEA", name="Индийский океан -> Ломбокский пролив (VLCC)", max_draft_m=25.0, detour_multiplier=1.15)

        # Азия и Дальний Восток
        self.add_maritime_leg("CHK_MALACCA", "PORT_SINGAPORE", max_draft_m=20.5)
        self.add_maritime_leg("PORT_SINGAPORE", "WP_SOUTH_CHINA_SEA")
        self.add_maritime_leg("WP_SOUTH_CHINA_SEA", "PORT_NINGBO")
        self.add_maritime_leg("WP_SOUTH_CHINA_SEA", "PORT_YOKOHAMA")
        self.add_maritime_leg("PORT_KOZMINO", "PORT_NINGBO")
        self.add_maritime_leg("PORT_KOZMINO", "PORT_YOKOHAMA")

    def find_optimal_maritime_route(
        self,
        origin_id: str,
        dest_id: str,
        vessel_class: VesselClass,
        is_laden: bool = True,
        alignment: FleetAlignment = FleetAlignment.WESTERN_INSURED,
        winter_arctic_freeze: bool = False,
    ) -> Optional[Dict]:
        """
        Интеллектуальный поиск маршрута A* с комплексной целевой функцией стоимости рейса ($):
        Учитывает:
        - Осадку судна (груженое или балластное) против глубины каналов/проливов;
        - Блокировку проливов (is_blocked);
        - Требование ледового класса Arc7 на СМП;
        - Стоимость бункерного топлива + суточную ставку фрахта + сборы каналов;
        - Военную премию за риск (Red Sea / Hormuz) и штормовые замедления.
        """
        v_spec = VESSEL_SPECS[vessel_class]
        current_draft = v_spec.draft_laden_m if is_laden else v_spec.draft_ballast_m
        base_speed = v_spec.speed_laden_knots if is_laden else v_spec.speed_ballast_knots

        def edge_cost(u: str, v: str, edge_attr: dict) -> Optional[float]:
            edge: TransportEdge = edge_attr["data"]
            target_node: WorldNode = self.nodes[v]

            # 1. Проверка блокировки пролива/узла или самого маршрута
            if not edge.is_active or target_node.is_blocked:
                return None

            # 2. Физическая проверка осадки судна
            if current_draft > edge.max_draft_m or current_draft > target_node.max_draft_m:
                return None

            # 3. Проверка ледовой обстановки (для СМП)
            if edge.requires_ice_class and winter_arctic_freeze and not v_spec.ice_class:
                return None

            # 4. Расчет времени хода с учетом погоды и очередей в каналах
            effective_speed = max(3.0, base_speed * edge.weather_speed_factor * target_node.weather_efficiency)
            sailing_hours = edge.distance_nm / effective_speed
            total_hours = sailing_hours + target_node.canal_Wait_hours
            total_days = total_hours / 24.0

            # 5. Экономика участка: фрахт + топливо + канальный сбор + военная страховка
            charter_cost = total_days * v_spec.daily_charter_usd
            fuel_cost = (sailing_hours / 24.0) * v_spec.bunker_burn_tons_day * BUNKER_FUEL_PRICE_USD_TON
            canal_cost = edge.canal_fee_usd + target_node.canal_toll_usd

            # Военный риск (западные страховщики берут огромный штраф за вход в зону конфликта)
            risk_multiplier = 2.5 if alignment == FleetAlignment.WESTERN_INSURED else 0.6
            war_risk_premium = edge.piracy_or_war_risk * risk_multiplier * 450_000.0

            return charter_cost + fuel_cost + canal_cost + war_risk_premium

        def heuristic(u: str, v: str) -> float:
            n1, n2 = self.nodes[u], self.nodes[v]
            dist = haversine_nm(n1.lat, n1.lon, n2.lat, n2.lon)
            min_days = (dist / base_speed) / 24.0
            return min_days * v_spec.daily_charter_usd

        try:
            path = nx.astar_path(
                self.maritime_graph,
                origin_id,
                dest_id,
                heuristic=heuristic,
                weight=edge_cost,
            )
        except nx.NetworkXNoPath:
            return None

        # Собираем полную телеметрию по найденному маршруту
        total_nm = 0.0
        total_hours = 0.0
        total_cost_usd = 0.0
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            edge: TransportEdge = self.maritime_graph[u][v]["data"]
            target_node = self.nodes[v]
            eff_speed = max(3.0, base_speed * edge.weather_speed_factor * target_node.weather_efficiency)
            seg_hours = (edge.distance_nm / eff_speed) + target_node.canal_Wait_hours

            total_nm += edge.distance_nm
            total_hours += seg_hours
            total_cost_usd += edge_cost(u, v, self.maritime_graph[u][v]) or 0.0

        return {
            "path_ids": path,
            "path_names": [self.nodes[nid].name for nid in path],
            "distance_nm": round(total_nm, 1),
            "transit_days": round(total_hours / 24.0, 2),
            "estimated_cost_usd": round(total_cost_usd, 0),
            "vessel_draft_m": current_draft,
        }
