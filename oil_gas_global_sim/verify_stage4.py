# verify_stage4.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import CommodityType, VesselClass
from core.models import FleetAlignment, TankerAgentModel
from core.simulation import SimulationEngine


def test_1_hurricane_and_arctic_ice_lifecycle(console: Console) -> None:
    console.print(
        "\n[bold yellow]ТЕСТ 1: Ураган 5-й категории в Мексиканском заливе и ледостав на СМП "
        "(Гео-полигоны для 3D-Земли, падение дебита и автоматическое восстановление)[/bold yellow]"
    )
    sim = SimulationEngine(tick_hours=1.0)
    houston = sim.world.nodes["PORT_HOUSTON"]
    permian = sim.world.nodes["FLD_PERMIAN"]

    ev_hurr = sim.chaos.trigger_scenario("GULF_HURRICANE_CAT5", custom_duration_hours=48.0)
    ev_ice = sim.chaos.trigger_scenario("ARCTIC_WINTER_POLAR_LOCK", custom_duration_hours=72.0)

    console.print(
        f"• Сформирован 3D-полигон урагана «{ev_hurr.name}»: [bold cyan]{len(ev_hurr.polygon_coords)} гео-вершин[/bold cyan] "
        f"вокруг ({ev_hurr.center_lat}°, {ev_hurr.center_lon}°), радиус {ev_hurr.radius_nm:.0f} миль.\n"
        f"• Погодная эффективность во время удара стихии: Хьюстон = [bold red]{houston.weather_efficiency*100:.0f}%[/bold red], "
        f"Пермский бассейн = [bold yellow]{permian.weather_efficiency*100:.0f}%[/bold yellow] | "
        f"Зимний режим СМП: [bold cyan]{sim.winter_arctic_freeze}[/bold cyan]\n"
        f"• Мгновенная реакция спотовых цен: WTI = [bold green]${sim.market.spot_prices[CommodityType.WTI]:.2f}[/bold green], "
        f"LNG = [bold green]${sim.market.spot_prices[CommodityType.LNG]:.2f}[/bold green]"
    )

    sim.run(until_hours=50.0)
    console.print(
        f"• Статус на 50-м часу (после ухода урагана): Хьюстон восстановлен до "
        f"[bold green]{houston.weather_efficiency*100:.0f}%[/bold green] | "
        f"Активных катаклизмов осталось: [bold yellow]{len(sim.chaos.active_cataclysms)}[/bold yellow] ({ev_ice.name})"
    )


def test_2_strait_blockade_and_mid_voyage_rerouting(console: Console) -> None:
    console.print(
        "\n[bold red]ТЕСТ 2: Военная блокада Красного моря и экстренный разворот танкера прямо в океане "
        "(Mid-Voyage Dynamic Rerouting вокруг мыса Доброй Надежды)[/bold red]"
    )
    sim = SimulationEngine(tick_hours=1.0)

    suez_tanker = sim.register_vessel(
        TankerAgentModel(
            id="REROUTE_SUEZ_01",
            name="Arabian Navigator Suezmax",
            vessel_class=VesselClass.SUEZMAX,
            alignment=FleetAlignment.WESTERN_INSURED,
            flag_country="NO",
            current_lat=26.64,
            current_lon=50.16,
            current_node_id="PORT_RAS_TANURA",
            cargo_type=CommodityType.ARAB_LIGHT,
            cargo_volume_bbl=1_000_000.0,
        )
    )

    initial_route = sim.router.find_congestion_aware_route(
        "PORT_RAS_TANURA", "PORT_ROTTERDAM", VesselClass.SUEZMAX, is_laden=True
    )
    console.print(
        f"• Первоначальный план перехода {suez_tanker.name}: [cyan]{' -> '.join(initial_route['path_names'])}[/cyan] "
        f"({initial_route['distance_nm']:,.0f} миль)"
    )

    sim.env.process(sim.sail_along_route(suez_tanker, initial_route["path_ids"]))
    sim.run(until_hours=15.0)
    sim.chaos.trigger_scenario("RED_SEA_HOUTHI_CRISIS", custom_duration_hours=600.0)
    sim.run(until_hours=850.0)

    console.print(
        f"• Счётчик экстренных перестроений маршрута в море: [bold yellow]{suez_tanker.rerouted_count}[/bold yellow]\n"
        f"• Фактически пройденный маршрут после разворота: [bold green]{' -> '.join(sim.world.nodes[nid].name for nid in suez_tanker.planned_route)}[/bold green]\n"
        f"• Итоговая пройденная дистанция: [bold]{suez_tanker.total_distance_sailed_nm:,.1f} NM[/bold] | "
        f"Текущий узел судна: [bold cyan]{sim.world.nodes[suez_tanker.current_node_id].name}[/bold cyan]"
    )

    for ev in sim.event_logs:
        if ev.subject_id == suez_tanker.id or ev.category == "CHAOS":
            console.print(f"  [dim][Час {ev.sim_hour:05.1f} | {ev.category}][/dim] {ev.message}")


def test_3_pipeline_sabotage_and_sanctions_cap(console: Console) -> None:
    console.print(
        "\n[bold magenta]ТЕСТ 3: Диверсия на магистральных нефтепроводах БТС/«Дружба» "
        "и ужесточение потолка цен G7 Price Cap до $45/барр.[/bold magenta]"
    )
    sim = SimulationEngine(tick_hours=1.0)
    brent_before = sim.market.spot_prices[CommodityType.BRENT]
    urals_before = sim.market.spot_prices[CommodityType.URAL]

    sim.chaos.trigger_scenario("DRUZHBA_BTS_SABOTAGE", custom_duration_hours=96.0)
    sim.chaos.trigger_scenario("G7_SANCTIONS_CAP_45", custom_duration_hours=120.0)
    sim.run(until_hours=24.0)

    bts_edge = sim.world.pipeline_graph["FLD_WEST_SIBERIA"]["PORT_PRIMORSK"]["data"]
    druzhba_edge = sim.world.pipeline_graph["FLD_WEST_SIBERIA"]["PORT_SCHWEDT"]["data"]

    console.print(
        f"• Статус трубы «{bts_edge.name}»: [bold red]{bts_edge.bottleneck.value}[/bold red] "
        f"(целостность {bts_edge.integrity_pct:.0f}%, прокачка {bts_edge.current_flow_bbl_hr:,.0f} барр/ч)\n"
        f"• Статус трубы «{druzhba_edge.name}»: [bold red]{druzhba_edge.bottleneck.value}[/bold red] "
        f"(целостность {druzhba_edge.integrity_pct:.0f}%, прокачка {druzhba_edge.current_flow_bbl_hr:,.0f} барр/ч)\n"
        f"• Доля остановленных мощностей мира (Supply Disruption): [bold yellow]{sim.market.supply_disruption_pct:.1f}%[/bold yellow]\n"
        f"• Реакция цен: BRENT [green]${brent_before:.2f}[/green] -> [bold red]${sim.market.spot_prices[CommodityType.BRENT]:.2f}[/bold red] | "
        f"URALS [green]${urals_before:.2f}[/green] -> [bold yellow]${sim.market.spot_prices[CommodityType.URAL]:.2f}[/bold yellow] "
        f"(дисконт расширился до [bold]${sim.market.spot_prices[CommodityType.BRENT] - sim.market.spot_prices[CommodityType.URAL]:.2f}/барр.[/bold])"
    )


def test_4_full_stochastic_chaos_and_market_simulation(console: Console) -> None:
    console.print(
        "\n[bold green]ТЕСТ 4: 20-суточная (480 ч) глобальная симуляция (42 танкера + Биржа НПЗ + "
        "Стохастический генератор катаклизмов + Живые котировки и ставки фрахта)[/bold green]"
    )
    sim = SimulationEngine(
        tick_hours=1.0,
        auto_spawn_fleet=True,
        enable_contract_dispatcher=True,
        enable_random_chaos=True,
        random_seed=77,
    )

    sim.run(until_hours=24.0)
    sim.chaos.trigger_scenario("HORMUZ_STRAIT_BLOCKADE", custom_duration_hours=96.0)
    summary = sim.run(until_hours=456.0)  # Всего 480 часов (20 суток)

    mkt_table = Table(title="Динамика мировых спотовых котировок и ставок фрахта (MarketEngine)", show_lines=True)
    mkt_table.add_column("Сутки (Час)", style="cyan")
    mkt_table.add_column("BRENT ($/б)", justify="right", style="bold green")
    mkt_table.add_column("WTI ($/б)", justify="right")
    mkt_table.add_column("URALS ($/б)", justify="right", style="yellow")
    mkt_table.add_column("LNG ($/б.н.э.)", justify="right", style="magenta")
    mkt_table.add_column("Мазут VLSFO ($/т)", justify="right")
    mkt_table.add_column("Фрахт VLCC ($/сут)", justify="right", style="bold cyan")
    mkt_table.add_column("Индекс воен. риска", justify="right", style="red")

    sampled_quotes = [q for q in sim.market.price_history if int(q.sim_hour) % 48 == 0]
    for q in sampled_quotes:
        mkt_table.add_row(
            f"День {q.sim_day:02d} ({q.sim_hour:03.0f} ч)",
            f"${q.spot_prices_usd[CommodityType.BRENT]:.2f}",
            f"${q.spot_prices_usd[CommodityType.WTI]:.2f}",
            f"${q.spot_prices_usd[CommodityType.URAL]:.2f}",
            f"${q.spot_prices_usd[CommodityType.LNG]:.2f}",
            f"${q.bunker_vlsfo_usd_ton:.0f}",
            f"${q.charter_rates_usd_day[VesselClass.VLCC]:,.0f}",
            f"{q.global_war_risk_index:.2f}",
        )
    console.print(mkt_table)

    cat_table = Table(title="Хроника катаклизмов и геополитических кризисов за 20 суток", show_lines=True)
    cat_table.add_column("ID", style="cyan")
    cat_table.add_column("Название катаклизма / кризиса", style="bold white")
    cat_table.add_column("Тип", style="yellow")
    cat_table.add_column("Старт (час)", justify="right")
    cat_table.add_column("Длительность", justify="right")
    cat_table.add_column("Затронуто узлов / труб", justify="right")
    cat_table.add_column("Статус")

    for c_ev in sim.chaos.cataclysm_history:
        status_str = "[bold red]АКТИВЕН[/bold red]" if c_ev.is_active else "[green]ВОССТАНОВЛЕН[/green]"
        cat_table.add_row(
            c_ev.id,
            c_ev.name,
            c_ev.cataclysm_type.value,
            f"{c_ev.start_hour:.0f} ч",
            f"{c_ev.duration_hours:.0f} ч",
            f"{len(c_ev.affected_node_ids)} узл. / {len(c_ev.affected_pipelines)} труб",
            status_str,
        )
    console.print(cat_table)

    console.print(
        f"[bold cyan]Итог 20 суток турбулентного мира (480 ч):[/bold cyan]\n"
        f"• Пережито глобальных катаклизмов: [bold red]{len(sim.chaos.cataclysm_history)}[/bold red] | "
        f"Экстренных разворотов судов в море: [bold yellow]{sum(v.rerouted_count for v in sim.vessels.values())}[/bold yellow]\n"
        f"• Выполнено контрактов НПЗ: [bold green]{summary['contracts_completed']}[/bold green] из {len(sim.contracts)} | "
        f"Чистая прибыль мирового флота: [bold green]${summary['fleet_net_profit_musd']} млн[/bold green]\n"
        f"• Текущие котировки: BRENT = [bold]${summary['spot_brent_usd']}[/bold] | "
        f"URALS = [bold]${summary['spot_urals_usd']}[/bold] | "
        f"LNG = [bold]${summary['spot_lng_usd']}[/bold] | "
        f"Фрахт VLCC = [bold]${summary['vlcc_charter_usd_day']:,.0f}/сут.[/bold]"
    )


def run_stage4_diagnostics() -> None:
    console = Console()
    console.print(
        Panel.fit(
            "[bold cyan]GLOBAL OIL & GAS DIGITAL TWIN — ЭТАП 4: ДВИЖОК КАТАКЛИЗМОВ, ГЕОПОЛИТИКИ И РЫНКА[/bold cyan]\n"
            "Проверка: Ураганы и 3D-полигоны | Блокады проливов и разворот судов в море | Диверсии на трубах | Живые котировки и фрахт",
            border_style="cyan",
        )
    )
    test_1_hurricane_and_arctic_ice_lifecycle(console)
    test_2_strait_blockade_and_mid_voyage_rerouting(console)
    test_3_pipeline_sabotage_and_sanctions_cap(console)
    test_4_full_stochastic_chaos_and_market_simulation(console)


if __name__ == "__main__":
    run_stage4_diagnostics()
