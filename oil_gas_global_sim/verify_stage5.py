# verify_stage5.py
import time
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.ipc_bus import SimulationIPCController


def run_stage5_diagnostics(launch_gui: bool = False) -> None:
    console = Console()
    console.print(
        Panel.fit(
            "[bold cyan]GLOBAL OIL & GAS DIGITAL TWIN — ЭТАП 5: МНОГОПРОЦЕССНАЯ ШИНА IPC (SharedMemory + Pipe) & 3D EARTH GUI[/bold cyan]\n"
            "Технологии: multiprocessing.shared_memory (Win32/POSIX RAM) | multiprocessing.Pipe | PySide6 QtWebChannel + Globe.gl (Zero-WebSockets)",
            border_style="cyan",
        )
    )

    controller = SimulationIPCController(shm_name="oil_gas_twin_verify_shm", random_seed=77)
    controller.start()

    try:
        state = None
        for _ in range(50):
            state = controller.poll_latest_state()
            if state is not None:
                break
            time.sleep(0.05)

        if state is None:
            console.print("[bold red][ERROR] Не удалось получить первый кадр из дочернего процесса симуляции.[/bold red]")
            return

        controller.send_command("SET_SPEED", value=40.0)
        controller.send_command("TRIGGER_CRISIS", scenario="RED_SEA_HOUTHI_CRISIS")
        controller.send_command("SPAWN_STS_DEMO")

        read_latencies_us = []
        frames_received = 0
        t_start = time.perf_counter()

        while frames_received < 12 and (time.perf_counter() - t_start) < 6.0:
            t0 = time.perf_counter()
            frame = controller.poll_latest_state()
            dt_us = (time.perf_counter() - t0) * 1e6
            if frame is not None:
                state = frame
                frames_received += 1
                read_latencies_us.append(dt_us)
            time.sleep(0.08)

        avg_lat_us = sum(read_latencies_us) / max(1, len(read_latencies_us))

        ipc_table = Table(title="Телеметрия межпроцессной шины SharedMemory + Pipe (Zero-Socket IPC)", show_lines=True)
        ipc_table.add_column("Метрика IPC и 3D-пакета", style="cyan")
        ipc_table.add_column("Значение", justify="right", style="bold green")

        ipc_table.add_row("Транспорт ядра симуляции", "OS SharedMemory (512 KB) + Duplex Pipe")
        ipc_table.add_row("Транспорт 3D-движка Земли", "Qt6 QWebChannel (Chromium Mojo IPC)")
        ipc_table.add_row("Получено кадров из дочернего процесса ОС", str(frames_received))
        ipc_table.add_row("Средняя задержка чтения из SharedMemory", f"{avg_lat_us:.1f} мкс ({avg_lat_us/1000:.3f} мс)")
        ipc_table.add_row("Синхронизировано 3D-узлов / трубопроводов", f"{len(state['nodes'])} / {len(state['pipelines'])}")
        ipc_table.add_row("Синхронизировано 3D-танкеров в океане", str(len(state["vessels"])))
        ipc_table.add_row("Активных 3D-полигонов катаклизмов", str(len(state["cataclysms"])))
        ipc_table.add_row("Текущая котировка BRENT в SharedMemory", f"${state['market']['prices']['BRENT']:.2f}/барр.")

        console.print(ipc_table)
    finally:
        controller.stop()

    if launch_gui:
        console.print("\n[bold green]Запуск нативного 3D Ситуационного Центра (PySide6 + QWebChannel)...[/bold green]")
        from core.desktop_app import launch_3d_situational_center
        launch_3d_situational_center()
    else:
        console.print(
            "\n[bold cyan]Для открытия 3D-глобуса Земли и Ситуационного Центра выполните:[/bold cyan] "
            "[bold yellow]python main.py[/bold yellow]"
        )


if __name__ == "__main__":
    run_stage5_diagnostics(launch_gui=False)
