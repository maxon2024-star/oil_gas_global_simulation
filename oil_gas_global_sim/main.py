# main.py
import sys
from rich.console import Console
from verify_stage1 import run_stage1_diagnostics
from verify_stage2 import run_stage2_diagnostics
from verify_stage3 import run_stage3_diagnostics
from verify_stage4 import run_stage4_diagnostics
from verify_stage5 import run_stage5_diagnostics


def main() -> None:
    console = Console()
    stage = sys.argv[1].lower() if len(sys.argv) > 1 else "gui"

    if stage == "gui":
        from core.desktop_app import launch_3d_situational_center
        launch_3d_situational_center()
    elif stage == "1":
        run_stage1_diagnostics()
    elif stage == "2":
        run_stage2_diagnostics()
    elif stage == "3":
        run_stage3_diagnostics()
    elif stage == "4":
        run_stage4_diagnostics()
    elif stage == "5":
        run_stage5_diagnostics(launch_gui=False)
    elif stage == "all":
        run_stage1_diagnostics()
        console.print("\n" + "=" * 100 + "\n")
        run_stage2_diagnostics()
        console.print("\n" + "=" * 100 + "\n")
        run_stage3_diagnostics()
        console.print("\n" + "=" * 100 + "\n")
        run_stage4_diagnostics()
        console.print("\n" + "=" * 100 + "\n")
        run_stage5_diagnostics(launch_gui=False)
    else:
        console.print("[bold cyan]Oil & Gas Global Simulator[/bold cyan]")
        console.print("Использование: [bold]python main.py [gui | 1 | 2 | 3 | 4 | 5 | all][/bold]")


if __name__ == "__main__":
    main()
