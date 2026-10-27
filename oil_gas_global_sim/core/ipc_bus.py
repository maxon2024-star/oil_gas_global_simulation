# core/ipc_bus.py
import json
import multiprocessing as mp
from multiprocessing import shared_memory
from multiprocessing.connection import Connection
import struct
import time
from typing import Any, Dict, Optional

from core.config import COMMODITY_SPECS, VESSEL_SPECS, CommodityType, VesselClass
from core.models import ContractStatus, NodeType, TankerAgentModel, VesselState
from core.simulation import SimulationEngine


SHM_BLOCK_SIZE = 524_288  # 512 KB сегмент в оперативной памяти ОС
SHM_HEADER_FORMAT = "<QQ"  # uint64 frame_seq, uint64 payload_length_bytes
SHM_HEADER_SIZE = struct.calcsize(SHM_HEADER_FORMAT)


def serialize_full_world_state(sim: SimulationEngine) -> Dict[str, Any]:
    """
    Упаковка полного 3D-состояния мира (узлы, резервуары, трубы, 42 танкера,
    полигоны катаклизмов, котировки и журнал событий) для записи в Shared Memory.
    """
    nodes_payload = []
    for n in sim.world.nodes.values():
        total_cap = sum(t.capacity_bbl for t in n.storage.values())
        total_cur = sum(t.current_level_bbl for t in n.storage.values())
        fill_pct = round((total_cur / total_cap) * 100.0, 1) if total_cap > 0 else 0.0

        nodes_payload.append({
            "id": n.id,
            "name": n.name,
            "country": n.country,
            "type": n.node_type.value,
            "lat": n.lat,
            "lon": n.lon,
            "is_blocked": n.is_blocked,
            "weather": n.weather_efficiency,
            "well_status": n.well_status.value,
            "prod_rate": round(n.current_production_bbl_hr, 0),
            "cons_rate": round(n.current_consumption_bbl_hr, 0),
            "fill_pct": fill_pct,
            "total_cur_mbbl": round(total_cur / 1e6, 2),
            "total_cap_mbbl": round(total_cap / 1e6, 2),
            "berth_count": n.berth_count,
            "occupied_berths": n.occupied_berths,
            "queued_vessels": n.queued_vessels,
            "demurrage_kusd": round(n.cumulative_demurrage_usd / 1e3, 1),
        })

    pipelines_payload = []
    for u, v, attr in sim.world.pipeline_graph.edges(data=True):
        edge = attr["data"]
        n1, n2 = sim.world.nodes[u], sim.world.nodes[v]
        pipelines_payload.append({
            "u": u,
            "v": v,
            "name": edge.name,
            "commodity": edge.allowed_commodity.value if edge.allowed_commodity else "CRUDE",
            "start_lat": n1.lat,
            "start_lon": n1.lon,
            "end_lat": n2.lat,
            "end_lon": n2.lon,
            "flow_bbl_hr": round(edge.current_flow_bbl_hr, 0),
            "max_flow_bbl_hr": round(edge.max_flow_bbl_hr, 0),
            "integrity_pct": round(edge.integrity_pct, 1),
            "bottleneck": edge.bottleneck.value,
            "pumped_mbbl": round(edge.cumulative_pumped_bbl / 1e6, 2),
        })

    maritime_edges_payload = []
    for u, v, attr in sim.world.maritime_graph.edges(data=True):
        if u < v:
            edge = attr["data"]
            n1, n2 = sim.world.nodes[u], sim.world.nodes[v]
            maritime_edges_payload.append({
                "start_lat": n1.lat,
                "start_lon": n1.lon,
                "end_lat": n2.lat,
                "end_lon": n2.lon,
                "war_risk": edge.piracy_or_war_risk,
                "ice": edge.requires_ice_class,
            })

    vessels_payload = []
    for v in sim.vessels.values():
        v_spec = VESSEL_SPECS[v.vessel_class]
        route_coords = [
            [sim.world.nodes[nid].lat, sim.world.nodes[nid].lon]
            for nid in v.planned_route
            if nid in sim.world.nodes
        ]
        dest_name = (
            sim.world.nodes[v.destination_node_id].name
            if v.destination_node_id and v.destination_node_id in sim.world.nodes
            else "—"
        )
        vessels_payload.append({
            "id": v.id,
            "name": v.name,
            "vessel_class": v.vessel_class.value,
            "alignment": v.alignment.value,
            "flag": v.flag_country,
            "lat": v.current_lat,
            "lon": v.current_lon,
            "heading": v.heading_deg,
            "speed": v.current_speed_knots,
            "state": v.state.value,
            "cargo": v.cargo_type.value if v.cargo_type else "BALLAST",
            "cargo_mbbl": round(v.cargo_volume_bbl / 1e6, 2),
            "fill_pct": round((v.cargo_volume_bbl / v_spec.capacity_bbl) * 100.0, 0),
            "dest_name": dest_name,
            "voyages": v.completed_voyages,
            "reroutes": v.rerouted_count,
            "demurrage_kusd": round(v.accumulated_demurrage_usd / 1e3, 1),
            "net_profit_musd": round(v.total_net_profit_usd / 1e6, 2),
            "route_coords": route_coords,
        })

    cataclysms_payload = []
    for c_ev in sim.chaos.active_cataclysms.values():
        cataclysms_payload.append({
            "id": c_ev.id,
            "name": c_ev.name,
            "type": c_ev.cataclysm_type.value,
            "lat": c_ev.center_lat,
            "lon": c_ev.center_lon,
            "radius_nm": c_ev.radius_nm,
            "severity": c_ev.severity,
            "remaining_hr": c_ev.remaining_hours,
            "polygon": [[lat, lon] for lat, lon in c_ev.polygon_coords],
        })

    contracts_payload = []
    for cnt in list(sim.contracts.values())[-25:]:
        seller_name = sim.world.nodes[cnt.seller_node_id].name
        buyer_name = sim.world.nodes[cnt.buyer_node_id].name
        ship_name = sim.vessels[cnt.assigned_vessel_id].name if cnt.assigned_vessel_id in sim.vessels else "—"
        pnl = cnt.realized_net_profit_usd if cnt.status == ContractStatus.COMPLETED else cnt.expected_netback_usd
        contracts_payload.append({
            "id": cnt.id,
            "route": f"{seller_name} -> {buyer_name}",
            "commodity": cnt.commodity.value,
            "volume_mbbl": round(cnt.target_volume_bbl / 1e6, 2),
            "fob": cnt.fob_price_usd_bbl,
            "cif": cnt.cif_price_usd_bbl,
            "vessel": ship_name,
            "status": cnt.status.value,
            "netback_kusd": round(pnl / 1e3, 0),
        })

    logs_payload = [
        {
            "hour": ev.sim_hour,
            "day": ev.sim_day,
            "cat": ev.category,
            "sev": ev.severity,
            "msg": ev.message,
        }
        for ev in sim.event_logs[-30:]
    ]

    kpi = sim.get_telemetry_snapshot()
    market_payload = {
        "prices": {k.value: v for k, v in sim.market.spot_prices.items()},
        "charter": {k.value: v for k, v in sim.market.charter_rates.items()},
        "bunker_vlsfo": sim.market.bunker_price_usd_ton,
        "g7_cap": sim.market.g7_price_cap_usd,
        "war_risk": sim.market.global_war_risk_index,
        "disruption_pct": sim.market.supply_disruption_pct,
    }

    return {
        "kpi": kpi,
        "market": market_payload,
        "nodes": nodes_payload,
        "pipelines": pipelines_payload,
        "maritime_grid": maritime_edges_payload,
        "vessels": vessels_payload,
        "cataclysms": cataclysms_payload,
        "contracts": contracts_payload,
        "logs": logs_payload,
    }


class SharedMemoryTelemetryBus:
    """
    Высокоскоростная шина разделяемой памяти ОС (multiprocessing.shared_memory).
    Позволяет процессу SimPy записывать состояние мира, а процессу Qt6/3D-рендера
    читать его напрямую из ОЗУ без сокетов, HTTP и сериализации по сети.
    """

    def __init__(self, name: str = "oil_gas_twin_shm", create: bool = True) -> None:
        self.name = name
        self.is_creator = create
        if create:
            try:
                old = shared_memory.SharedMemory(name=name, create=False)
                old.close()
                old.unlink()
            except FileNotFoundError:
                pass
            self.shm = shared_memory.SharedMemory(name=name, create=True, size=SHM_BLOCK_SIZE)
            self._write_header(0, 0)
        else:
            self.shm = shared_memory.SharedMemory(name=name, create=False, size=SHM_BLOCK_SIZE)

        self._frame_seq = 0

    def _write_header(self, seq: int, length: int) -> None:
        struct.pack_into(SHM_HEADER_FORMAT, self.shm.buf, 0, seq, length)

    def write_state(self, state_dict: Dict[str, Any]) -> int:
        raw_bytes = json.dumps(state_dict, ensure_ascii=False).encode("utf-8")
        length = len(raw_bytes)
        if length + SHM_HEADER_SIZE > SHM_BLOCK_SIZE:
            raise BufferError(f"Размер телеметрии ({length} байт) превысил буфер SharedMemory!")

        self._frame_seq += 1
        self.shm.buf[SHM_HEADER_SIZE : SHM_HEADER_SIZE + length] = raw_bytes
        self._write_header(self._frame_seq, length)
        return length

    def read_state(self, last_seen_seq: int = -1) -> Optional[tuple]:
        seq, length = struct.unpack_from(SHM_HEADER_FORMAT, self.shm.buf, 0)
        if seq == 0 or length == 0 or seq == last_seen_seq:
            return None
        raw_bytes = bytes(self.shm.buf[SHM_HEADER_SIZE : SHM_HEADER_SIZE + length])
        return seq, json.loads(raw_bytes.decode("utf-8"))

    def close(self) -> None:
        try:
            self.shm.close()
            if self.is_creator:
                self.shm.unlink()
        except Exception:
            pass


def _simulation_worker_loop(shm_name: str, cmd_conn: Connection, random_seed: int) -> None:
    """
    Изолированный рабочий процесс ОС (multiprocessing.Process), в котором
    непрерывно выполняется дискретно-событийное ядро SimPy.
    """
    bus = SharedMemoryTelemetryBus(name=shm_name, create=False)
    sim = SimulationEngine(
        tick_hours=1.0,
        auto_spawn_fleet=True,
        enable_contract_dispatcher=True,
        enable_random_chaos=True,
        random_seed=random_seed,
    )

    paused = False
    ticks_per_second = 4.0
    running = True

    bus.write_state(serialize_full_world_state(sim))

    while running:
        loop_start = time.perf_counter()

        while cmd_conn.poll(0.0):
            cmd_msg = cmd_conn.recv()
            action = cmd_msg.get("action")

            if action == "STOP":
                running = False
                break
            elif action == "SET_PAUSED":
                paused = bool(cmd_msg.get("value", False))
            elif action == "SET_SPEED":
                ticks_per_second = float(max(0.5, min(48.0, cmd_msg.get("value", 4.0))))
            elif action == "STEP_ONCE":
                sim.run(until_hours=1.0)
                bus.write_state(serialize_full_world_state(sim))
            elif action == "TRIGGER_CRISIS":
                scenario_key = cmd_msg.get("scenario")
                if scenario_key in sim.chaos.SCENARIO_PRESETS:
                    sim.chaos.trigger_scenario(scenario_key)
                    bus.write_state(serialize_full_world_state(sim))
            elif action == "CLEAR_ALL_CRISES":
                for ev in list(sim.chaos.active_cataclysms.values()):
                    preset = sim.chaos.SCENARIO_PRESETS.get(ev.id.split("_", 2)[-1], {})
                    sim.chaos._revert_cataclysm_effects(ev, preset)
                for n in sim.world.nodes.values():
                    n.is_blocked = False
                    n.weather_efficiency = 1.0
                for _, _, attr in sim.world.pipeline_graph.edges(data=True):
                    attr["data"].is_active = True
                    attr["data"].integrity_pct = 100.0
                sim.log_event("CHAOS", "INFO", "GOD_MODE", "Все активные катаклизмы и блокады сняты оператором.")
                bus.write_state(serialize_full_world_state(sim))
            elif action == "SPAWN_STS_DEMO":
                feeder = next(
                    (v for v in sim.vessels.values() if v.vessel_class == VesselClass.AFRAMAX and v.state == VesselState.IDLE),
                    None,
                )
                mother = next(
                    (v for v in sim.vessels.values() if v.vessel_class == VesselClass.SUEZMAX and v.state == VesselState.IDLE),
                    None,
                )
                if feeder and mother:
                    feeder.active_contract_id = "STS_MANUAL"
                    mother.active_contract_id = "STS_MANUAL"
                    sim.env.process(
                        sim.execute_sts_relay_mission(
                            feeder_vessel=feeder,
                            mother_vessel=mother,
                            export_port_id="PORT_NOVOROSSIYSK",
                            sts_hub_id="STS_KALAMATA",
                            import_port_id="PORT_JAMNAGAR",
                            commodity=CommodityType.URAL,
                            transfer_volume_bbl=700_000.0,
                        )
                    )
                    sim.log_event(
                        "MARITIME",
                        "INFO",
                        "STS_KALAMATA",
                        f"Оператор запустил STS-эстафету: {feeder.name} + {mother.name} через Лаконикос (Греция)!",
                    )
                    bus.write_state(serialize_full_world_state(sim))

        if not running:
            break

        if not paused:
            sim.run(until_hours=1.0)
            bus.write_state(serialize_full_world_state(sim))

        elapsed = time.perf_counter() - loop_start
        target_delay = (1.0 / ticks_per_second) if not paused else 0.08
        sleep_time = max(0.005, target_delay - elapsed)
        time.sleep(sleep_time)

    bus.close()
    cmd_conn.close()


class SimulationIPCController:
    """
    Контроллер управления дочерним процессом симуляции (для GUI и тестов Этапа 5).
    """

    def __init__(self, shm_name: str = "oil_gas_twin_shm", random_seed: int = 77) -> None:
        self.shm_name = shm_name
        self.bus = SharedMemoryTelemetryBus(name=shm_name, create=True)
        self.parent_conn, child_conn = mp.Pipe(duplex=True)
        self.process = mp.Process(
            target=_simulation_worker_loop,
            args=(shm_name, child_conn, random_seed),
            daemon=True,
        )
        self.last_seq: int = -1

    def start(self) -> None:
        self.process.start()

    def send_command(self, action: str, **kwargs: Any) -> None:
        if self.process.is_alive():
            self.parent_conn.send({"action": action, **kwargs})

    def poll_latest_state(self) -> Optional[Dict[str, Any]]:
        res = self.bus.read_state(self.last_seq)
        if res is None:
            return None
        seq, state = res
        self.last_seq = seq
        return state

    def stop(self) -> None:
        try:
            if self.process.is_alive():
                self.parent_conn.send({"action": "STOP"})
                self.process.join(timeout=2.0)
                if self.process.is_alive():
                    self.process.terminate()
        finally:
            self.bus.close()
