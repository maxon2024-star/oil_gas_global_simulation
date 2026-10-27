# verify_stage2.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import COMMODITY_SPECS, CommodityType, VesselClass
from core.models import FleetAlignment, NodeType, TankerAgentModel
from core.simulation import SimulationEngine


def test_1_global_mass_balance(console: Console) -> None:
    console.print("\n[bold yellow]ТЕСТ 1: Глобальная гидродинамика — Добыча, Трубопроводы и НПЗ (72 часа SimPy)[/bold yellow]")
    sim = SimulationEngine(tick_hours=1.0)
    summary = sim.run(until_hours=72.0)

    # Таблица состояния промыслов и импортных НПЗ
    node_table = Table(title="Баланс ключевых месторождений и НПЗ после 72 часов (3 суток)", show_lines=True)
    node_table.add_column("Узел", style="cyan")
    node_table.add_column("Тип", style="white")
    node_table.add_column("Статус скважин", style="green")
    node_table.add_column("Добыто / Переработано (барр.)", justify="right")
    node_table.add_column("Заполнение резервуаров", justify="right", style="yellow")

    for nid in [
        "FLD_WEST_SIBERIA",
        "FLD_GHAWAR",
        "FLD_PERMIAN",
        "FLD_KASHAGAN",
        "PORT_ROTTERDAM",
        "PORT_SCHWEDT",
        "PORT_NINGBO",
    ]:
        n = sim.world.nodes[nid]
        vol = n.cumulative_produced_bbl if n.production_rate_bbl_hr > 0 else n.cumulative_consumed_bbl
        action_label = f"+{vol:,.0f} (добыча)" if n.production_rate_bbl_hr > 0 else f"-{vol:,.0f} (НПЗ)"
        tanks_str = " | ".join(
            f"{t.commodity.value}: {t.fill_ratio*100:.1f}% ({t.current_level_bbl/1e6:.2f}M)"
            for t in n.storage.values()
        )
        node_table.add_row(
            n.name,
            n.node_type.value,
            n.well_status.value,
            action_label,
            tanks_str,
        )
    console.print(node_table)

    # Таблица работы магистральных трубопроводов
    pipe_table = Table(title="Телеметрия магистральных трубопроводов за 72 часа", show_lines=True)
    pipe_table.add_column("Магистраль", style="cyan")
    pipe_table.add_column("Сорт", style="yellow")
    pipe_table.add_column("Текущий поток (барр/ч)", justify="right")
    pipe_table.add_column("Прокачано за 72 ч (барр.)", justify="right", style="green")
    pipe_table.add_column("Выручка по тарифу ($)", justify="right", style="magenta")
    pipe_table.add_column("Диагностика (Bottleneck)")

    for u, v, attr in sim.world.pipeline_graph.edges(data=True):
        edge = attr["data"]
        pipe_table.add_row(
            edge.name,
            edge.allowed_commodity.value,
            f"{edge.current_flow_bbl_hr:,.0f}",
            f"{edge.cumulative_pumped_bbl:,.0f}",
            f"${edge.cumulative_tariff_usd:,.0f}",
            edge.bottleneck.value,
        )
    console.print(pipe_table)
    console.print(
        f"[bold green]Итог 72 ч:[/bold green] Добыто в мире: [bold]{summary['total_produced_mbbl']} млн барр.[/bold] | "
        f"Прокачано по трубам: [bold]{summary['total_pumped_mbbl']} млн барр.[/bold] | "
        f"Переработано НПЗ: [bold]{summary['total_consumed_mbbl']} млн барр.[/bold]"
    )


def test_2_tank_tops_and_backpressure(console: Console) -> None:
    console.print(
        "\n[bold red]ТЕСТ 2: Эффект домино «Tank Tops» (Затоваривание Приморска -> "
        "Гидравлическое противодавление БТС -> Аварийное глушение скважин Западной Сибири)[/bold red]"
    )
    sim = SimulationEngine(tick_hours=1.0)

    # Заполняем баки Приморска и Западной Сибири до 92%, а трубу «Дружба» и Южную ветку перекрываем,
    # чтобы проверить ударную волну затоваривания по Балтийской трубопроводной системе (БТС)
    primorsk_tank = sim.world.nodes["PORT_PRIMORSK"].storage[CommodityType.URAL]
    primorsk_tank.current_level_bbl = 9_100_000  # из 10_000_000

    siberia_ural = sim.world.nodes["FLD_WEST_SIBERIA"].storage[CommodityType.URAL]
    siberia_ural.current_level_bbl = 10_900_000  # из 12_000_000

    sim.world.pipeline_graph["FLD_WEST_SIBERIA"]["PORT_SCHWEDT"]["data"].is_active = False
    sim.world.pipeline_graph["FLD_WEST_SIBERIA"]["PORT_NOVOROSSIYSK"]["data"].is_active = False

    sim.run(until_hours=36.0)

    bts_edge = sim.world.pipeline_graph["FLD_WEST_SIBERIA"]["PORT_PRIMORSK"]["data"]
    siberia_node = sim.world.nodes["FLD_WEST_SIBERIA"]

    console.print(
        f"• Резервуар Приморска (URALS): [bold yellow]{primorsk_tank.fill_ratio*100:.1f}%[/bold yellow] "
        f"({primorsk_tank.current_level_bbl:,.0f} / {primorsk_tank.capacity_bbl:,.0f} барр.)\n"
        f"• Статус трубы «{bts_edge.name}»: [bold red]{bts_edge.bottleneck.value}[/bold red] "
        f"(текущий поток упал до {bts_edge.current_flow_bbl_hr:,.0f} барр/ч)\n"
        f"• Статус промысла «{siberia_node.name}»: [bold red]{siberia_node.well_status.value}[/bold red] | "
        f"Штраф за консервацию: [bold magenta]${siberia_node.shut_in_penalty_usd:,.0f}[/bold magenta] | "
        f"Таймер расконсервации: {siberia_node.shut_in_cooldown_hours:.0f} ч."
    )

    for ev in sim.event_logs:
        if ev.subject_id in ("FLD_WEST_SIBERIA", bts_edge.name):
            console.print(f"  [dim][Час {ev.sim_hour:04.1f} | {ev.severity}][/dim] {ev.message}")


def test_3_port_smo_queue_and_demurrage(console: Console) -> None:
    console.print(
        "\n[bold cyan]ТЕСТ 3: СМО Порта Козьмино — Одновременный приход 6 танкеров в шторм, "
        "очередь на рейде и лавина демереджа[/bold cyan]"
    )
    sim = SimulationEngine(tick_hours=1.0)
    kozmino = sim.world.nodes["PORT_KOZMINO"]
    # В Козьмино 3 причала; ухудшаем погоду до 65% (штормовой накат в заливе Находка)
    # и обеспечиваем полный резервуарный парк ESPO для непрерывного налива всех 6 судов
    kozmino.weather_efficiency = 0.65
    kozmino.storage[CommodityType.ESPO].current_level_bbl = 8_800_000

    fleet_wave = [
        ("TNK_01", "SCF Primorye", VesselClass.AFRAMAX, FleetAlignment.SHADOW_FLEET, 0.0),
        ("TNK_02", "Cosco Dragon VLCC", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, 1.0),
        ("TNK_03", "Eastern Star", VesselClass.SUEZMAX, FleetAlignment.SHADOW_FLEET, 2.0),
        ("TNK_04", "Pacific Harmony", VesselClass.AFRAMAX, FleetAlignment.NEUTRAL_STATE, 2.0),
        ("TNK_05", "Jade Emperor VLCC", VesselClass.VLCC, FleetAlignment.NEUTRAL_STATE, 3.0),
        ("TNK_06", "Amur Bay", VesselClass.SUEZMAX, FleetAlignment.SHADOW_FLEET, 4.0),
    ]

    def spawn_vessel_with_delay(v_model: TankerAgentModel, delay_hr: float):
        if delay_hr > 0:
            yield sim.env.timeout(delay_hr)
        smo = sim.port_smos["PORT_KOZMINO"]
        yield sim.env.process(
            smo.process_vessel_call(vessel=v_model, operation="LOAD", commodity=CommodityType.ESPO)
        )

    for vid, vname, vcls, align, arrival_delay in fleet_wave:
        vessel = sim.register_vessel(
            TankerAgentModel(
                id=vid,
                name=vname,
                vessel_class=vcls,
                alignment=align,
                flag_country="PA",
                current_lat=kozmino.lat,
                current_lon=kozmino.lon,
                current_node_id="PORT_KOZMINO",
            )
        )
        sim.env.process(spawn_vessel_with_delay(vessel, arrival_delay))

    sim.run(until_hours=140.0)

    smo_table = Table(title="Квитанции СМО терминала Козьмино (3 причала, штормовой коэффициент 0.65)", show_lines=True)
    smo_table.add_column("Судно", style="cyan")
    smo_table.add_column("Класс")
    smo_table.add_column("Приход на рейд", justify="right")
    smo_table.add_column("Швартовка к причалу", justify="right")
    smo_table.add_column("Ожидание в очереди", justify="right", style="yellow")
    smo_table.add_column("Время у причала", justify="right")
    smo_table.add_column("Залито ESPO (барр.)", justify="right", style="green")
    smo_table.add_column("Демередж ($)", justify="right", style="bold red")

    for r in sim.port_receipts:
        smo_table.add_row(
            r.vessel_name,
            r.vessel_class.value,
            f"{r.arrival_hour:.0f} ч",
            f"{r.berth_acquired_hour:.0f} ч",
            f"{r.queue_wait_hours:.1f} ч",
            f"{r.service_hours:.1f} ч",
            f"{r.volume_transferred_bbl:,.0f}",
            f"${r.demurrage_incurred_usd:,.0f}",
        )
    console.print(smo_table)
    console.print(
        f"Суммарный демередж по порту Козьмино: [bold red]${kozmino.cumulative_demurrage_usd:,.0f}[/bold red] | "
        f"Обслужено танкеров: [bold green]{kozmino.total_vessels_served}[/bold green]"
    )


def test_4_full_lng_voyage_and_boil_off(console: Console) -> None:
    console.print(
        "\n[bold magenta]ТЕСТ 4: Сквозной морской рейс криогенного газовоза LNG_QMAX "
        "(Ras Laffan Катар -> Ормуз -> Малакка -> Нинбо Китай) с потерями Boil-Off Gas[/bold magenta]"
    )
    sim = SimulationEngine(tick_hours=1.0)
    qmax = sim.register_vessel(
        TankerAgentModel(
            id="LNG_01",
            name="Al Mafyar Q-Max",
            vessel_class=VesselClass.LNG_QMAX,
            alignment=FleetAlignment.NEUTRAL_STATE,
            flag_country="QA",
            current_lat=25.93,
            current_lon=51.53,
            current_node_id="PORT_RAS_LAFFAN",
        )
    )

    sim.env.process(
        sim.execute_full_delivery_mission(
            vessel=qmax,
            load_port_id="PORT_RAS_LAFFAN",
            unload_port_id="PORT_NINGBO",
            commodity=CommodityType.LNG,
        )
    )

    # Прогоняем 420 часов (~17.5 суток) до завершения погрузки, перехода и выгрузки в Китае
    sim.run(until_hours=420.0)

    load_rec = next((r for r in sim.port_receipts if r.operation == "LOAD"), None)
    unload_rec = next((r for r in sim.port_receipts if r.operation == "UNLOAD"), None)
    chokepoints_passed = [r for r in sim.port_receipts if r.operation == "CANAL_TRANSIT"]
    bog_value_usd = qmax.accumulated_boil_off_bbl * COMMODITY_SPECS[CommodityType.LNG].base_price_usd

    console.print(
        f"• Загружено в Рас-Лаффане (Катар): [bold green]{load_rec.volume_transferred_bbl:,.0f} б.н.э. СПГ[/bold green] "
        f"(выход в море на {load_rec.departure_hour:.0f}-м часу)\n"
        f"• Пройдено узких проливов (СМО): [bold cyan]{', '.join(r.node_name for r in chokepoints_passed)}[/bold cyan]\n"
        f"• Пройдена дистанция: [bold]{qmax.total_distance_sailed_nm:,.1f} морских миль[/bold] | "
        f"Сожжено бункера VLSFO: [bold]{qmax.bunker_consumed_tons:,.1f} тонн[/bold]\n"
        f"• Испарилось СПГ в пути (Boil-Off Gas 0.12%/сут.): [bold yellow]{qmax.accumulated_boil_off_bbl:,.0f} б.н.э.[/bold yellow] "
        f"(рыночная стоимость потерь: [bold red]${bog_value_usd:,.0f}[/bold red])\n"
        f"• Фактически сдано в резервуары Нинбо (Китай): [bold green]{unload_rec.volume_transferred_bbl:,.0f} б.н.э. СПГ[/bold green] "
        f"(разгрузка завершена на {unload_rec.departure_hour:.0f}-м часу / {unload_rec.departure_hour/24:.1f} сут.)\n"
        f"• Полная себестоимость рейса (фрахт + мазут + портовые сборы): [bold magenta]${qmax.voyage_cost_usd:,.0f}[/bold magenta]"
    )


def run_stage2_diagnostics() -> None:
    console = Console()
    console.print(
        Panel.fit(
            "[bold cyan]GLOBAL OIL & GAS DIGITAL TWIN — ЭТАП 2: ДИСКРЕТНО-СОБЫТИЙНОЕ ЯДРО (SimPy)[/bold cyan]\n"
            "Проверка: Баланс масс | Консервация скважин (Tank Tops) | Противодавление труб | СМО портов и Демередж | Криогенный Boil-Off",
            border_style="cyan",
        )
    )
    test_1_global_mass_balance(console)
    test_2_tank_tops_and_backpressure(console)
    test_3_port_smo_queue_and_demurrage(console)
    test_4_full_lng_voyage_and_boil_off(console)


if __name__ == "__main__":
    run_stage2_diagnostics()
