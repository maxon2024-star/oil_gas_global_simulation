# engines/market.py
import random
from typing import Dict, List, Optional

import numpy as np

from core.config import (
    BUNKER_FUEL_PRICE_USD_TON,
    COMMODITY_SPECS,
    VESSEL_SPECS,
    CommodityType,
    VesselClass,
)
from core.models import (
    MarketQuoteSnapshot,
    NodeType,
    PipelineBottleneck,
    TankerAgentModel,
    VesselState,
    WellOperationalStatus,
    WorldNode,
)
from core.world_graph import WorldLogisticsGraph


class GlobalHydrocarbonMarketEngine:
    """
    Этап 4: Рыночный и макроэкономический контур цифрового двойника.
    Отвечает за:
    - Динамические спотовые цены (Brent, WTI, Urals, ESPO, Arab Light, CPC, LNG)
      по модели Mean-Reverting Ornstein-Uhlenbeck + Jump-Diffusion;
    - Динамический санкционный дисконт Urals/ESPO и контроль потолка цен G7 Price Cap;
    - Ставки суточного фрахта (Worldscale Charter Rates) по каждому классу судов;
    - Котировки бункерного топлива VLSFO и расчёт маржинальности FOB/CIF.
    """

    def __init__(self, world: WorldLogisticsGraph, seed: int = 42) -> None:
        self.world = world
        self.rng = np.random.default_rng(seed)

        self.spot_prices: Dict[CommodityType, float] = {
            c: spec.base_price_usd for c, spec in COMMODITY_SPECS.items()
        }
        self.charter_rates: Dict[VesselClass, float] = {
            vcls: spec.daily_charter_usd for vcls, spec in VESSEL_SPECS.items()
        }
        self.demurrage_multipliers: Dict[VesselClass, float] = {
            vcls: 1.0 for vcls in VesselClass
        }
        self.bunker_price_usd_ton: float = BUNKER_FUEL_PRICE_USD_TON
        self.g7_price_cap_usd: float = 60.0
        self.sanctions_discount_urals_usd: float = 12.5
        self.sanctions_discount_espo_usd: float = 6.5
        self.global_war_risk_index: float = 0.05
        self.supply_disruption_pct: float = 0.0
        self.price_history: List[MarketQuoteSnapshot] = []

    def apply_instant_price_shock(self, commodity: CommodityType, delta_usd: float) -> float:
        """Мгновенный ценовой геп (Jump) при возникновении геополитического или техногенного шока"""
        base = COMMODITY_SPECS[commodity].base_price_usd
        self.spot_prices[commodity] = round(
            float(np.clip(self.spot_prices[commodity] + delta_usd, base * 0.45, base * 2.8)),
            2,
        )
        return self.spot_prices[commodity]

    def update_market_step(
        self,
        sim_hour: float,
        vessels: Dict[str, TankerAgentModel],
        dt_hours: float = 1.0,
    ) -> MarketQuoteSnapshot:
        """
        Почасовой пересчёт мировых товарных и фрахтовых рынков на основе
        физического состояния промыслов, трубопроводов, запасов НПЗ и очередей в проливах.
        """
        dt_days = dt_hours / 24.0

        # 1. Оценка глобального выбытия добычи и трубопроводов (Supply Disruption)
        total_nom_prod = 0.0
        lost_prod = 0.0
        for node in self.world.nodes.values():
            if node.production_rate_bbl_hr > 0:
                total_nom_prod += node.production_rate_bbl_hr
                if node.is_blocked or node.well_status in (
                    WellOperationalStatus.SHUT_IN_TANK_TOP,
                    WellOperationalStatus.OFFLINE_DAMAGED,
                ):
                    lost_prod += node.production_rate_bbl_hr
                elif node.well_status == WellOperationalStatus.THROTTLED:
                    lost_prod += node.production_rate_bbl_hr * 0.65
                else:
                    lost_prod += node.production_rate_bbl_hr * (1.0 - node.weather_efficiency)

        total_pipe_cap = 0.0
        lost_pipe_cap = 0.0
        for _, _, attr in self.world.pipeline_graph.edges(data=True):
            edge = attr["data"]
            total_pipe_cap += edge.max_flow_bbl_hr
            if not edge.is_active or edge.bottleneck == PipelineBottleneck.SHUTDOWN:
                lost_pipe_cap += edge.max_flow_bbl_hr
            else:
                lost_pipe_cap += edge.max_flow_bbl_hr * (1.0 - edge.integrity_pct / 100.0)

        prod_disruption_ratio = lost_prod / max(1.0, total_nom_prod)
        pipe_disruption_ratio = lost_pipe_cap / max(1.0, total_pipe_cap)
        self.supply_disruption_pct = round(
            (0.65 * prod_disruption_ratio + 0.35 * pipe_disruption_ratio) * 100.0, 2
        )

        # 2. Оценка военной напряжённости и блокад в ключевых проливах (Chokepoints)
        chokepoint_stress = 0.0
        critical_straits = ["CHK_HORMUZ", "CHK_BAB_EL_MANDEB", "CHK_SUEZ", "CHK_BOSPORUS", "CHK_MALACCA"]
        for cid in critical_straits:
            cnode = self.world.nodes.get(cid)
            if cnode:
                if cnode.is_blocked:
                    chokepoint_stress += 0.32
                chokepoint_stress += min(0.20, cnode.queued_vessels / 40.0)

        max_edge_war_risk = max(
            (attr["data"].piracy_or_war_risk for _, _, attr in self.world.maritime_graph.edges(data=True)),
            default=0.0,
        )
        self.global_war_risk_index = round(
            float(np.clip(chokepoint_stress + max_edge_war_risk * 0.5, 0.0, 1.0)), 3
        )

        # 3. Оценка средних запасов в импортных хабах (НПЗ) по каждому сорту
        import_fill_by_comm: Dict[CommodityType, List[float]] = {c: [] for c in CommodityType}
        total_unmet_demand_mbbl = sum(n.unmet_demand_bbl for n in self.world.nodes.values()) / 1e6
        for node in self.world.nodes.values():
            if node.node_type == NodeType.IMPORT_TERMINAL:
                for comm, tank in node.storage.items():
                    import_fill_by_comm[comm].append(tank.fill_ratio)

        # 4. Стохастический пересчёт спотовых цен по процессу Орнштейна-Уленбека
        kappa = 0.18
        sigma = 0.014

        # Сначала обновляем мировой бенчмарк BRENT
        brent_base = COMMODITY_SPECS[CommodityType.BRENT].base_price_usd
        brent_fills = import_fill_by_comm[CommodityType.BRENT]
        avg_brent_fill = float(np.mean(brent_fills)) if brent_fills else 0.45
        scarcity_factor = max(0.0, 0.52 - avg_brent_fill) * 0.85
        brent_target = brent_base * (
            1.0
            + 0.75 * (self.supply_disruption_pct / 100.0)
            + scarcity_factor
            + 0.42 * self.global_war_risk_index
            + min(0.25, total_unmet_demand_mbbl * 0.02)
        )
        noise_brent = float(self.rng.normal(0.0, 1.0))
        new_brent = (
            self.spot_prices[CommodityType.BRENT]
            + kappa * (brent_target - self.spot_prices[CommodityType.BRENT]) * dt_days
            + sigma * self.spot_prices[CommodityType.BRENT] * np.sqrt(dt_days) * noise_brent
        )
        self.spot_prices[CommodityType.BRENT] = round(float(np.clip(new_brent, 48.0, 185.0)), 2)
        brent_now = self.spot_prices[CommodityType.BRENT]

        # Обновляем остальные сорта с привязкой к спредам и локальным шокам
        for comm, spec in COMMODITY_SPECS.items():
            if comm == CommodityType.BRENT:
                continue

            fills = import_fill_by_comm.get(comm, [])
            avg_fill = float(np.mean(fills)) if fills else 0.45
            local_scarcity = max(0.0, 0.50 - avg_fill) * 0.70

            if comm == CommodityType.URAL:
                target = max(32.0, brent_now - self.sanctions_discount_urals_usd + local_scarcity * 15.0)
            elif comm == CommodityType.ESPO:
                target = max(40.0, brent_now - self.sanctions_discount_espo_usd + local_scarcity * 18.0)
            elif comm == CommodityType.ARAB_LIGHT:
                hormuz_blocked = 1.0 if self.world.nodes["CHK_HORMUZ"].is_blocked else 0.0
                target = brent_now + 0.8 + hormuz_blocked * 14.0 + local_scarcity * 20.0
            elif comm == CommodityType.WTI:
                houston_factor = 1.0 - self.world.nodes["PORT_HOUSTON"].weather_efficiency
                target = brent_now - 3.8 + houston_factor * 11.0 + local_scarcity * 16.0
            elif comm == CommodityType.LNG:
                qatar_blocked = 1.0 if self.world.nodes["CHK_HORMUZ"].is_blocked else 0.0
                target = spec.base_price_usd * (
                    1.0
                    + 0.55 * self.global_war_risk_index
                    + 0.30 * qatar_blocked
                    + local_scarcity
                )
            else:
                target = spec.base_price_usd * (brent_now / brent_base) * (1.0 + local_scarcity * 0.4)

            noise = float(self.rng.normal(0.0, 1.0))
            updated = (
                self.spot_prices[comm]
                + (kappa * 1.2) * (target - self.spot_prices[comm]) * dt_days
                + sigma * self.spot_prices[comm] * np.sqrt(dt_days) * noise
            )
            self.spot_prices[comm] = round(
                float(np.clip(updated, spec.base_price_usd * 0.4, spec.base_price_usd * 2.6)), 2
            )

        # 5. Пересчёт стоимости бункерного мазута VLSFO и суточных ставок фрахта (Worldscale)
        self.bunker_price_usd_ton = round(
            BUNKER_FUEL_PRICE_USD_TON * (0.35 + 0.65 * (brent_now / brent_base)), 1
        )
        total_queued_ships = sum(n.queued_vessels for n in self.world.nodes.values())

        for vcls, vspec in VESSEL_SPECS.items():
            cls_ships = [v for v in vessels.values() if v.vessel_class == vcls]
            if cls_ships:
                active_ratio = sum(1 for v in cls_ships if v.state != VesselState.IDLE) / len(cls_ships)
            else:
                active_ratio = 0.5

            utilization_premium = 1.6 * (max(0.0, active_ratio - 0.45) ** 2)
            congestion_premium = min(0.85, total_queued_ships * 0.025)
            war_premium = self.global_war_risk_index * 0.75
            multiplier = 1.0 + utilization_premium + congestion_premium + war_premium

            self.charter_rates[vcls] = round(vspec.daily_charter_usd * multiplier, 0)
            self.demurrage_multipliers[vcls] = round(1.0 + congestion_premium * 0.8 + war_premium * 0.5, 2)

        snapshot = MarketQuoteSnapshot(
            sim_hour=round(sim_hour, 2),
            sim_day=int(sim_hour // 24) + 1,
            spot_prices_usd=dict(self.spot_prices),
            urals_espo_discount_usd=round(
                self.spot_prices[CommodityType.BRENT] - self.spot_prices[CommodityType.URAL], 2
            ),
            g7_price_cap_usd=self.g7_price_cap_usd,
            bunker_vlsfo_usd_ton=self.bunker_price_usd_ton,
            charter_rates_usd_day=dict(self.charter_rates),
            global_war_risk_index=self.global_war_risk_index,
            supply_disruption_pct=self.supply_disruption_pct,
        )
        if int(sim_hour) % 6 == 0 or not self.price_history:
            self.price_history.append(snapshot)
        return snapshot

    def get_export_fob_price(self, export_node: WorldNode, commodity: CommodityType) -> float:
        """Расчёт отпускной цены FOB в порту отгрузки на основе живых спотовых котировок"""
        spot = self.spot_prices.get(commodity, COMMODITY_SPECS[commodity].base_price_usd)
        tank = export_node.storage.get(commodity)
        inventory_pressure = (tank.fill_ratio - 0.55) * 5.0 if tank else 0.0
        sanction_extra = (
            2.5 if export_node.sanctioned_for_western_fleet and commodity != CommodityType.URAL else 0.0
        )
        return round(max(22.0, spot - inventory_pressure - sanction_extra), 2)

    def get_import_cif_price(
        self, import_node: WorldNode, commodity: CommodityType, eff_fill_ratio: float
    ) -> float:
        """Расчёт цены покупки CIF на НПЗ импортёра с учётом живого рынка и дефицита в резервуарах"""
        spot = self.spot_prices.get(commodity, COMMODITY_SPECS[commodity].base_price_usd)

        if commodity in (CommodityType.URAL, CommodityType.ESPO):
            benchmark = self.spot_prices[CommodityType.BRENT]
            cif_base = benchmark - (3.5 if commodity == CommodityType.URAL else 1.0)
        else:
            regional_premium = 8.5 if import_node.country in {"JP", "CN", "NL", "DE"} else 6.5
            cif_base = spot + regional_premium

        urgency_premium = max(0.0, (0.82 - eff_fill_ratio) * 26.0)
        return round(cif_base + urgency_premium, 2)
