# engines/routing.py
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import networkx as nx

from core.config import (
    BUNKER_FUEL_PRICE_USD_TON,
    COMMODITY_SPECS,
    VESSEL_SPECS,
    CommodityType,
    VesselClass,
)
from core.models import (
    FleetAlignment,
    NodeType,
    TankerAgentModel,
    TransportEdge,
    WorldNode,
)
from core.world_graph import WorldLogisticsGraph, haversine_nm

if TYPE_CHECKING:
    from engines.market import GlobalHydrocarbonMarketEngine


class MaritimeRoutingEngine:
    """
    Этапы 3–4: Интеллектуальный маршрутизатор и геополитический комплаенс-движок.
    Учитывает живые биржевые ставки фрахта и цены мазута из GlobalHydrocarbonMarketEngine.
    """

    WESTERN_STRICT_IMPORT_COUNTRIES = {"NL", "DE", "JP", "GB", "US"}
    G7_CRUDE_PRICE_CAP_USD = 60.0

    def __init__(
        self,
        world: WorldLogisticsGraph,
        market: Optional["GlobalHydrocarbonMarketEngine"] = None,
    ) -> None:
        self.world = world
        self.market = market

    def is_vessel_compatible_with_commodity(
        self, vessel_class: VesselClass, commodity: CommodityType
    ) -> bool:
        v_spec = VESSEL_SPECS[vessel_class]
        c_spec = COMMODITY_SPECS[commodity]
        if c_spec.is_cryogenic:
            return v_spec.can_carry_lng
        return not v_spec.can_carry_lng

    def check_sanctions_compliance(
        self,
        alignment: FleetAlignment,
        export_node: WorldNode,
        import_node: WorldNode,
        commodity: CommodityType,
        fob_price_usd: Optional[float] = None,
    ) -> Tuple[bool, str]:
        spot_fob = fob_price_usd or (
            self.market.spot_prices[commodity]
            if self.market
            else COMMODITY_SPECS[commodity].base_price_usd
        )
        price_cap = self.market.g7_price_cap_usd if self.market else self.G7_CRUDE_PRICE_CAP_USD

        if alignment == FleetAlignment.WESTERN_INSURED:
            if export_node.sanctioned_for_western_fleet:
                if commodity == CommodityType.LNG or spot_fob > price_cap:
                    return (
                        False,
                        f"Санкционный запрет P&I Club: {export_node.name} под санкциями, "
                        f"цена {commodity.value} (${spot_fob:.1f}) выше Price Cap (${price_cap:.0f}).",
                    )

        if alignment == FleetAlignment.SHADOW_FLEET:
            if import_node.country in self.WESTERN_STRICT_IMPORT_COUNTRIES:
                return (
                    False,
                    f"Портовый контроль ЕС/G7 ({import_node.name}, {import_node.country}): "
                    f"запрещён заход судов SHADOW_FLEET без западной P&I страховки.",
                )
            if not export_node.sanctioned_for_western_fleet and export_node.country in {"US", "NO"}:
                return (
                    False,
                    f"Экспортный терминал {export_node.name} ({export_node.country}) не отгружает сырьё на SHADOW_FLEET.",
                )

        if export_node.country == "RU" and import_node.country in {"NL", "DE", "JP"}:
            return (
                False,
                f"Эмбарго на прямую морскую поставку из РФ ({export_node.name}) в {import_node.name}.",
            )

        return True, "COMPLIANT"

    def check_terminal_draft_compatibility(
        self, vessel_class: VesselClass, node: WorldNode, is_laden: bool = True
    ) -> bool:
        v_spec = VESSEL_SPECS[vessel_class]
        draft = v_spec.draft_laden_m if is_laden else v_spec.draft_ballast_m
        return draft <= node.max_draft_m

    def find_congestion_aware_route(
        self,
        origin_id: str,
        dest_id: str,
        vessel_class: VesselClass,
        is_laden: bool = True,
        alignment: FleetAlignment = FleetAlignment.WESTERN_INSURED,
        winter_arctic_freeze: bool = False,
        include_queue_penalties: bool = True,
    ) -> Optional[Dict]:
        if origin_id == dest_id:
            return {
                "path_ids": [origin_id],
                "path_names": [self.world.nodes[origin_id].name],
                "distance_nm": 0.0,
                "transit_days": 0.0,
                "estimated_queue_hours": 0.0,
                "estimated_cost_usd": 0.0,
                "vessel_draft_m": VESSEL_SPECS[vessel_class].draft_laden_m
                if is_laden
                else VESSEL_SPECS[vessel_class].draft_ballast_m,
            }

        v_spec = VESSEL_SPECS[vessel_class]
        current_draft = v_spec.draft_laden_m if is_laden else v_spec.draft_ballast_m
        base_speed = v_spec.speed_laden_knots if is_laden else v_spec.speed_ballast_knots

        daily_charter = self.market.charter_rates[vessel_class] if self.market else v_spec.daily_charter_usd
        bunker_price = self.market.bunker_price_usd_ton if self.market else BUNKER_FUEL_PRICE_USD_TON
        demurrage_mult = self.market.demurrage_multipliers[vessel_class] if self.market else 1.0

        def estimate_node_congestion_hours(node: WorldNode) -> float:
            if not include_queue_penalties or node.queued_vessels <= 0:
                return 0.0
            if node.node_type == NodeType.CHOKEPOINT:
                slots = max(2, min(20, node.max_daily_vessels // 3))
                avg_service = max(2.0, node.canal_Wait_hours)
                return (node.queued_vessels / slots) * avg_service
            if node.berth_count > 0:
                return (node.queued_vessels / node.berth_count) * 18.0
            return 0.0

        def edge_cost(u: str, v: str, edge_attr: dict) -> Optional[float]:
            edge: TransportEdge = edge_attr["data"]
            target_node: WorldNode = self.world.nodes[v]

            if not edge.is_active or target_node.is_blocked:
                return None
            if current_draft > edge.max_draft_m or current_draft > target_node.max_draft_m:
                return None
            if edge.requires_ice_class and winter_arctic_freeze and not v_spec.ice_class:
                return None

            eff_speed = max(3.0, base_speed * edge.weather_speed_factor * target_node.weather_efficiency)
            sailing_hours = edge.distance_nm / eff_speed
            congestion_hours = estimate_node_congestion_hours(target_node)
            total_hours = sailing_hours + target_node.canal_Wait_hours + congestion_hours
            total_days = total_hours / 24.0

            charter_cost = total_days * daily_charter
            fuel_cost = (sailing_hours / 24.0) * v_spec.bunker_burn_tons_day * bunker_price
            canal_cost = max(edge.canal_fee_usd, target_node.canal_toll_usd)
            expected_demurrage = (
                max(0.0, congestion_hours - 4.0) * v_spec.demurrage_rate_usd_hr * demurrage_mult
            )

            if alignment == FleetAlignment.WESTERN_INSURED:
                risk_mult = 2.5
            elif alignment == FleetAlignment.NEUTRAL_STATE:
                risk_mult = 1.2
            else:
                risk_mult = 0.5
            war_risk_premium = edge.piracy_or_war_risk * risk_mult * 450_000.0

            return charter_cost + fuel_cost + canal_cost + expected_demurrage + war_risk_premium

        def heuristic(u: str, v: str) -> float:
            n1, n2 = self.world.nodes[u], self.world.nodes[v]
            dist = haversine_nm(n1.lat, n1.lon, n2.lat, n2.lon)
            return ((dist / base_speed) / 24.0) * daily_charter

        try:
            path = nx.astar_path(
                self.world.maritime_graph,
                origin_id,
                dest_id,
                heuristic=heuristic,
                weight=edge_cost,
            )
        except nx.NetworkXNoPath:
            return None

        total_nm = 0.0
        total_hours = 0.0
        total_queue_hours = 0.0
        total_cost_usd = 0.0

        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            edge: TransportEdge = self.world.maritime_graph[u][v]["data"]
            target_node = self.world.nodes[v]
            eff_speed = max(3.0, base_speed * edge.weather_speed_factor * target_node.weather_efficiency)
            q_hrs = estimate_node_congestion_hours(target_node)
            seg_hours = (edge.distance_nm / eff_speed) + target_node.canal_Wait_hours + q_hrs

            total_nm += edge.distance_nm
            total_hours += seg_hours
            total_queue_hours += q_hrs
            total_cost_usd += edge_cost(u, v, self.world.maritime_graph[u][v]) or 0.0

        return {
            "path_ids": path,
            "path_names": [self.world.nodes[nid].name for nid in path],
            "distance_nm": round(total_nm, 1),
            "transit_days": round(total_hours / 24.0, 2),
            "estimated_queue_hours": round(total_queue_hours, 1),
            "estimated_cost_usd": round(total_cost_usd, 0),
            "vessel_draft_m": current_draft,
        }

    def plan_sts_relay(
        self,
        export_port_id: str,
        import_port_id: str,
        feeder_class: VesselClass,
        mother_class: VesselClass,
        feeder_alignment: FleetAlignment = FleetAlignment.SHADOW_FLEET,
        mother_alignment: FleetAlignment = FleetAlignment.NEUTRAL_STATE,
        winter_arctic_freeze: bool = False,
    ) -> Optional[Dict]:
        sts_candidates = [
            nid for nid, n in self.world.nodes.items() if n.node_type == NodeType.STS_ZONE and not n.is_blocked
        ]
        best_plan: Optional[Dict] = None

        for sts_id in sts_candidates:
            leg1 = self.find_congestion_aware_route(
                origin_id=export_port_id,
                dest_id=sts_id,
                vessel_class=feeder_class,
                is_laden=True,
                alignment=feeder_alignment,
                winter_arctic_freeze=winter_arctic_freeze,
            )
            if not leg1:
                continue

            leg2 = self.find_congestion_aware_route(
                origin_id=sts_id,
                dest_id=import_port_id,
                vessel_class=mother_class,
                is_laden=True,
                alignment=mother_alignment,
                winter_arctic_freeze=winter_arctic_freeze,
            )
            if not leg2:
                continue

            sts_fee_usd = 85_000.0
            combined_cost = leg1["estimated_cost_usd"] + leg2["estimated_cost_usd"] + sts_fee_usd

            if best_plan is None or combined_cost < best_plan["combined_cost_usd"]:
                best_plan = {
                    "sts_hub_id": sts_id,
                    "sts_hub_name": self.world.nodes[sts_id].name,
                    "feeder_leg": leg1,
                    "mother_leg": leg2,
                    "sts_fee_usd": sts_fee_usd,
                    "combined_distance_nm": round(leg1["distance_nm"] + leg2["distance_nm"], 1),
                    "combined_transit_days": round(leg1["transit_days"] + leg2["transit_days"] + 1.2, 2),
                    "combined_cost_usd": round(combined_cost, 0),
                }

        return best_plan
