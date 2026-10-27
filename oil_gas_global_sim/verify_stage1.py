# verify_stage1.py
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from core.config import VesselClass
from core.models import FleetAlignment, NodeType
from core.world_graph import WorldLogisticsGraph


def run_stage1_diagnostics() -> None:
    console = Console()
    world = WorldLogisticsGraph()

    console.print(Panel.fit(
        "[bold cyan]GLOBAL OIL & GAS DIGITAL TWIN — ЭТАП 1: ГЕОПРОСТРАНСТВЕННОЕ ЯДРО[/bold cyan]\n"
        f"Узлов в мире: [bold yellow]{len(world.nodes)}[/bold yellow] | "
        f"Морских коридоров: [bold green]{world.maritime_graph.number_of_edges()}[/bold green] | "
        f"Магистральных трубопроводов: [bold magenta]{world.pipeline_graph.number_of_edges()}[/bold magenta]",
        border_style="cyan"
    ))

    # 1. Вывод таблицы магистральных трубопроводов
    pipe_table = Table(title="Сеть магистральных нефте- и газопроводов", show_lines=True)
    pipe_table.add_column("Название магистрали", style="cyan")
    pipe_table.add_column("Маршрут", style="white")
    pipe_table.add_column("Сорт сырья", style="yellow")
    pipe_table.add_column("Протяженность (NM)", justify="right")
    pipe_table.add_column("Мощность (барр/сутки)", justify="right", style="green")

    for u, v, attr in world.pipeline_graph.edges(data=True):
        edge = attr["data"]
        pipe_table.add_row(
            edge.name,
            f"{world.nodes[u].name} -> {world.nodes[v].name}",
            edge.allowed_commodity.value,
            f"{edge.distance_nm:,.0f}",
            f"{edge.max_flow_bbl_hr * 24:,.0f}",
        )
    console.print(pipe_table)

    # 2. ТЕСТ 1: Физика осадки судов (Suezmax vs груженый супертанкер VLCC)
    console.print("\n[bold yellow]ТЕСТ 1: Влияние осадки судна на проход через Суэцкий канал (Ras Tanura -> Rotterdam)[/bold yellow]")
    route_suezmax = world.find_optimal_maritime_route("PORT_RAS_TANURA", "PORT_ROTTERDAM", VesselClass.SUEZMAX, is_laden=True)
    route_vlcc_laden = world.find_optimal_maritime_route("PORT_RAS_TANURA", "PORT_ROTTERDAM", VesselClass.VLCC, is_laden=True)
    route_vlcc_ballast = world.find_optimal_maritime_route("PORT_ROTTERDAM", "PORT_RAS_TANURA", VesselClass.VLCC, is_laden=False)

    t1 = Table(show_lines=True)
    t1.add_column("Класс судна и режим")
    t1.add_column("Осадка (м)")
    t1.add_column("Выбранный маршрут (узлы графа)", style="cyan")
    t1.add_column("Дистанция (миль)", justify="right")
    t1.add_column("Время в пути", justify="right")
    t1.add_column("Бюджет рейса ($)", justify="right", style="green")

    for label, r in [
        ("SUEZMAX (С грузом 1 млн барр.)", route_suezmax),
        ("VLCC (С грузом 2 млн барр. — не лезет в Суэц!)", route_vlcc_laden),
        ("VLCC (Порожняком в балласте — проходит Суэц!)", route_vlcc_ballast),
    ]:
        t1.add_row(
            label,
            f"{r['vessel_draft_m']} м",
            " -> ".join(r["path_names"]),
            f"{r['distance_nm']:,.0f} NM",
            f"{r['transit_days']} сут.",
            f"${r['estimated_cost_usd']:,.0f}",
        )
    console.print(t1)

    # 3. ТЕСТ 2: Геополитический кризис — Блокада Баб-эль-Мандебского пролива (Красное море)
    console.print("\n[bold red]ТЕСТ 2: Военная эскалация в Красном море (Блокировка Баб-эль-Мандебского пролива)[/bold red]")
    world.nodes["CHK_BAB_EL_MANDEB"].is_blocked = True
    route_crisis = world.find_optimal_maritime_route("PORT_RAS_TANURA", "PORT_ROTTERDAM", VesselClass.SUEZMAX, is_laden=True)
    world.nodes["CHK_BAB_EL_MANDEB"].is_blocked = False  # Снимаем блокировку после теста

    console.print(
        f"• Маршрут Suezmax [bold green]ДО кризиса[/bold green]:  {route_suezmax['distance_nm']:,.0f} миль | "
        f"{route_suezmax['transit_days']} суток | ${route_suezmax['estimated_cost_usd']:,.0f}\n"
        f"• Маршрут Suezmax [bold red]ПРИ БЛОКАДЕ[/bold red]: {route_crisis['distance_nm']:,.0f} миль | "
        f"{route_crisis['transit_days']} суток | ${route_crisis['estimated_cost_usd']:,.0f}\n"
        f"  [italic]Трасса обхода:[/italic] {' -> '.join(route_crisis['path_names'])}"
    )

    # 4. ТЕСТ 3: Арктическая навигация по Северному морскому пути (Сабетта -> Нинбо, Китай)
    console.print("\n[bold cyan]ТЕСТ 3: Зимний ледостав на СМП (Сабетта Ямал -> Нинбо, Китай)[/bold cyan]")
    r_arc7 = world.find_optimal_maritime_route("PORT_YAMAL_SABETTA", "PORT_NINGBO", VesselClass.ARC7_LNG, winter_arctic_freeze=True)
    r_qmax_winter = world.find_optimal_maritime_route("PORT_YAMAL_SABETTA", "PORT_NINGBO", VesselClass.LNG_QMAX, winter_arctic_freeze=True)
    r_qmax_summer = world.find_optimal_maritime_route("PORT_YAMAL_SABETTA", "PORT_NINGBO", VesselClass.LNG_QMAX, winter_arctic_freeze=False)

    console.print(
        f"• Газовоз ледового класса [bold green]ARC7_LNG (зимой)[/bold green]: идет напрямую через Берингов пролив за [bold]{r_arc7['transit_days']} сут.[/bold] ({r_arc7['distance_nm']:,.0f} NM)\n"
        f"• Обычный газовоз [bold yellow]LNG_QMAX (летом)[/bold yellow]: проходит за [bold]{r_qmax_summer['transit_days']} сут.[/bold]\n"
        f"• Обычный газовоз [bold red]LNG_QMAX (зимой без ледокола)[/bold red]: СМП закрыт льдами (маршрут: {r_qmax_winter})"
    )


if __name__ == "__main__":
    run_stage1_diagnostics()
