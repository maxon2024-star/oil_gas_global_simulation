# verify_stage3.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import CommodityType, VesselClass
from core.models import ContractStatus, FleetAlignment, TankerAgentModel
from core.simulation import SimulationEngine
from core.world_graph import WorldLogisticsGraph
from engines.routing import MaritimeRoutingEngine


def test_1_sanctions_and_congestion_routing(console: Console) -> None:
    console.print(
        "\n[bold yellow]ТЕСТ 1: Матрица санкционного комплаенса (P&I Price Cap vs Shadow Fleet) "
        "и динамический учет очередей в проливах[/bold yellow]"
    )
    world = WorldLogisticsGraph()
    router = MaritimeRoutingEngine(world)

    comp_table = Table(title="Проверка геополитического комплаенса рейсов (MaritimeRoutingEngine)", show_lines=True)
    comp_table.add_column("Маршрут (Экспорт -> Импорт)", style="cyan")
    comp_table.add_column("Сорт и цена FOB", style="yellow")
    comp_table.add_column("Коалиция судна")
    comp_table.add_column("Вердикт комплаенса", style="bold")
    comp_table.add_column("Причина / Статус")

    cases = [
        ("PORT_PRIMORSK", "PORT_JAMNAGAR", CommodityType.URAL, 66.0, FleetAlignment.WESTERN_INSURED),
        ("PORT_PRIMORSK", "PORT_JAMNAGAR", CommodityType.URAL, 66.0, FleetAlignment.SHADOW_FLEET),
        ("PORT_KOZMINO", "PORT_NINGBO", CommodityType.ESPO, 74.0, FleetAlignment.NEUTRAL_STATE),
        ("PORT_PRIMORSK", "PORT_ROTTERDAM", CommodityType.URAL, 55.0, FleetAlignment.SHADOW_FLEET),
        ("PORT_HOUSTON", "PORT_ROTTERDAM", CommodityType.WTI, 78.5, FleetAlignment.WESTERN_INSURED),
    ]

    for exp_id, imp_id, comm, fob, align in cases:
        exp_node, imp_node = world.nodes[exp_id], world.nodes[imp_id]
        ok, reason = router.check_sanctions_compliance(align, exp_node, imp_node, comm, fob_price_usd=fob)
        verdict = "[green]РАЗРЕШЕНО[/green]" if ok else "[red]ЗАБЛОКИРОВАНО[/red]"
        comp_table.add_row(
            f"{exp_node.name} -> {imp_node.name}",
            f"{comm.value} (${fob:.1f}/bbl)",
            align.value,
            verdict,
            reason,
        )
    console.print(comp_table)

    # Проверка реакции на пробку в проливе Босфор
    r_free = router.find_congestion_aware_route(
        "PORT_NOVOROSSIYSK", "PORT_JAMNAGAR", VesselClass.SUEZMAX, is_laden=True, alignment=FleetAlignment.SHADOW_FLEET
    )
    world.nodes["CHK_BOSPORUS"].queued_vessels = 24  # Искусственная пробка из 24 танкеров у входа в Босфор
    world.nodes["CHK_SUEZ"].queued_vessels = 30      # Пробка в Суэцком канале
    r_congested = router.find_congestion_aware_route(
        "PORT_NOVOROSSIYSK", "PORT_JAMNAGAR", VesselClass.SUEZMAX, is_laden=True, alignment=FleetAlignment.SHADOW_FLEET
    )
    world.nodes["CHK_BOSPORUS"].queued_vessels = 0
    world.nodes["CHK_SUEZ"].queued_vessels = 0

    console.print(
        f"• Рейс Новороссийск -> Индия [bold green]без пробок в проливах[/bold green]: "
        f"{r_free['transit_days']} сут. | Очереди: {r_free['estimated_queue_hours']} ч | "
        f"Бюджет: [bold]${r_free['estimated_cost_usd']:,.0f}[/bold]\n"
        f"• Тот же рейс [bold red]при скоплении судов в Босфоре (24) и Суэце (30)[/bold red]: "
        f"{r_congested['transit_days']} сут. | Очереди: [bold yellow]{r_congested['estimated_queue_hours']} ч[/bold yellow] | "
        f"Бюджет с демереджем: [bold red]${r_congested['estimated_cost_usd']:,.0f}[/bold red]"
    )


def test_2_sts_offshore_relay_mission(console: Console) -> None:
    console.print(
        "\n[bold cyan]ТЕСТ 2: Двухзвенная рейдовая перегрузка борт-в-борт (STS Transfer) в открытом море "
        "(Новороссийск -> Aframax -> Зона STS Лаконикос Греция -> VLCC -> Джамнагар Индия)[/bold cyan]"
    )
    sim = SimulationEngine(tick_hours=1.0)

    # Планируем оптимальную точку STS между Новороссийском и Индией
    sts_plan = sim.router.plan_sts_relay(
        export_port_id="PORT_NOVOROSSIYSK",
        import_port_id="PORT_JAMNAGAR",
        feeder_class=VesselClass.AFRAMAX,
        mother_class=VesselClass.VLCC,
    )
    console.print(
        f"• Планировщик STS выбрал хаб: [bold yellow]{sts_plan['sts_hub_name']}[/bold yellow] | "
        f"Суммарная дистанция: {sts_plan['combined_distance_nm']:,.0f} миль | "
        f"Расчетное время эстафеты: {sts_plan['combined_transit_days']} сут."
    )

    feeder = sim.register_vessel(
        TankerAgentModel(
            id="STS_FEEDER_01",
            name="Kuban Shuttle Aframax",
            vessel_class=VesselClass.AFRAMAX,
            alignment=FleetAlignment.SHADOW_FLEET,
            flag_country="PA",
            current_lat=44.72,
            current_lon=37.77,
            current_node_id="PORT_NOVOROSSIYSK",
        )
    )
    mother_vlcc = sim.register_vessel(
        TankerAgentModel(
            id="STS_MOTHER_01",
            name="Ispahan Giant VLCC",
            vessel_class=VesselClass.VLCC,
            alignment=FleetAlignment.NEUTRAL_STATE,
            flag_country="IN",
            current_lat=36.55,
            current_lon=22.75,
            current_node_id="STS_KALAMATA",
        )
    )

    sim.env.process(
        sim.execute_sts_relay_mission(
            feeder_vessel=feeder,
            mother_vessel=mother_vlcc,
            export_port_id="PORT_NOVOROSSIYSK",
            sts_hub_id=sts_plan["sts_hub_id"],
            import_port_id="PORT_JAMNAGAR",
            commodity=CommodityType.URAL,
            transfer_volume_bbl=700_000.0,
        )
    )

    sim.run(until_hours=1000.0)

    sts_rec = next((r for r in sim.port_receipts if r.operation == "STS_TRANSFER"), None)
    unload_rec = next((r for r in sim.port_receipts if r.operation == "UNLOAD"), None)

    if sts_rec is None:
        console.print("[bold red]ОШИБКА: STS-перегрузка не состоялась.[/bold red]")
        return

    console.print(
        f"• [bold green]Этап 1 (Челнок):[/bold green] {feeder.name} вывез 700,000 барр. URALS из Новороссийска через Босфор.\n"
        f"• [bold yellow]Этап 2 (Рандеву в море):[/bold yellow] В узле «{sts_rec.node_name}» с {sts_rec.arrival_hour:.0f}-го по {sts_rec.departure_hour:.0f}-й час "
        f"перекачано борт-в-борт [bold]{sts_rec.volume_transferred_bbl:,.0f} барр.[/bold] (сбор STS: ${sts_rec.port_or_canal_fee_usd:,.0f})."
    )

    if unload_rec is not None:
        console.print(
            f"• [bold magenta]Этап 3 (Океанское плечо):[/bold magenta] Супертанкер {mother_vlcc.name} доставил и выгрузил "
            f"[bold green]{unload_rec.volume_transferred_bbl:,.0f} барр.[/bold green] на НПЗ Джамнагар (Индия) на {unload_rec.departure_hour:.0f}-м часу."
        )
    else:
        console.print(
            f"• [bold red]Этап 3 (Океанское плечо):[/bold red] Супертанкер {mother_vlcc.name} [bold red]НЕ СМОГ[/bold red] достичь порта Джамнагар или выгрузить груз. "
            f"(Возможно, маршрут для груженого VLCC вокруг Африки заблокирован или не найден A*). "
            f"Последнее состояние: {mother_vlcc.state} в узле {mother_vlcc.current_node_id}."
        )


def test_3_and_4_autonomous_global_fleet_simulation(console: Console) -> None:
    console.print(
        "\n[bold green]ТЕСТ 3 и 4: Полномасштабная автономная симуляция (42 танкера, биржа контрактов НПЗ, "
        "оптимизация Netback, 360 часов / 15 суток)[/bold green]"
    )
    sim = SimulationEngine(
        tick_hours=1.0,
        auto_spawn_fleet=True,
        enable_contract_dispatcher=True,
    )

    # Прогоняем 360 часов (15 полных суток работы мирового нефтегазового рынка)
    summary = sim.run(until_hours=360.0)

    # 1. Вывод таблицы топ-12 контрактов биржи НПЗ
    cnt_table = Table(title="Образцы контрактов, автоматически заключенных биржей НПЗ (Top-12)", show_lines=True)
    cnt_table.add_column("ID", style="cyan")
    cnt_table.add_column("Экспорт -> Импорт (НПЗ)")
    cnt_table.add_column("Сорт", style="yellow")
    cnt_table.add_column("Объем (барр.)", justify="right")
    cnt_table.add_column("FOB -> CIF ($/б)", justify="right")
    cnt_table.add_column("Назначенное судно", style="green")
    cnt_table.add_column("Статус")
    cnt_table.add_column("Ожид. / Факт. Netback ($)", justify="right", style="magenta")

    for c in list(sim.contracts.values())[:12]:
        seller = sim.world.nodes[c.seller_node_id].name
        buyer = sim.world.nodes[c.buyer_node_id].name
        v_name = sim.vessels[c.assigned_vessel_id].name if c.assigned_vessel_id else "—"
        pnl_val = c.realized_net_profit_usd if c.status == ContractStatus.COMPLETED else c.expected_netback_usd
        cnt_table.add_row(
            c.id,
            f"{seller} -> {buyer}",
            c.commodity.value,
            f"{c.target_volume_bbl:,.0f}",
            f"${c.fob_price_usd_bbl:.1f} -> ${c.cif_price_usd_bbl:.1f}",
            v_name,
            c.status.value,
            f"${pnl_val:,.0f}",
        )
    console.print(cnt_table)

    # 2. Сводная статистика по классам флота (42 судна)
    class_table = Table(title="Операционная и финансовая сводка мирового флота по классам судов (за 15 суток)", show_lines=True)
    class_table.add_column("Класс танкеров", style="cyan")
    class_table.add_column("Судов в строю", justify="right")
    class_table.add_column("Завершено рейсов", justify="right", style="green")
    class_table.add_column("Пройдено (морских миль)", justify="right")
    class_table.add_column("Доставлено сырья (барр.)", justify="right", style="yellow")
    class_table.add_column("Сожжено мазута (тонн)", justify="right")
    class_table.add_column("Чистая прибыль Netback ($)", justify="right", style="bold green")

    for vcls in VesselClass:
        ships = [v for v in sim.vessels.values() if v.vessel_class == vcls]
        class_table.add_row(
            vcls.value,
            str(len(ships)),
            str(sum(v.completed_voyages for v in ships)),
            f"{sum(v.total_distance_sailed_nm for v in ships):,.0f} NM",
            f"{sum(v.total_cargo_delivered_bbl for v in ships):,.0f}",
            f"{sum(v.bunker_consumed_tons for v in ships):,.1f} т",
            f"${sum(v.total_net_profit_usd for v in ships):,.0f}",
        )
    console.print(class_table)

    console.print(
        f"[bold cyan]Глобальный итог 15 суток (360 ч):[/bold cyan]\n"
        f"• Всего судов во флоте: [bold]{summary['fleet_total']}[/bold] | "
        f"Активных в море/портах: [bold yellow]{summary['fleet_active']}[/bold yellow]\n"
        f"• Всего сгенерировано контрактов НПЗ: [bold]{len(sim.contracts)}[/bold] | "
        f"Полностью выгружено в баки покупателей: [bold green]{summary['contracts_completed']}[/bold green]\n"
        f"• Добыто сырья: [bold]{summary['total_produced_mbbl']} млн барр.[/bold] | "
        f"Прокачано по трубам: [bold]{summary['total_pumped_mbbl']} млн барр.[/bold] | "
        f"Переработано НПЗ: [bold]{summary['total_consumed_mbbl']} млн барр.[/bold]\n"
        f"• Суммарная чистая прибыль мирового флота: [bold green]${summary['fleet_net_profit_musd']} млн[/bold green]"
    )


def run_stage3_diagnostics() -> None:
    console = Console()
    console.print(
        Panel.fit(
            "[bold cyan]GLOBAL OIL & GAS DIGITAL TWIN — ЭТАП 3: ИНТЕЛЛЕКТУАЛЬНЫЕ АГЕНТЫ И БИРЖА КОНТРАКТОВ[/bold cyan]\n"
            "Проверка: Санкционный комплаенс | Пробки в проливах | Рейдовая перегрузка STS в море | Автономный флот (42 судна) и Netback-биржа",
            border_style="cyan",
        )
    )
    test_1_sanctions_and_congestion_routing(console)
    test_2_sts_offshore_relay_mission(console)
    test_3_and_4_autonomous_global_fleet_simulation(console)


if __name__ == "__main__":
    run_stage3_diagnostics()
