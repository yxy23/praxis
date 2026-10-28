
from pathlib import Path
import os
import sys

RUNTIME = Path.home() / ".local/share/praxis-switchboard/.venv"
INSTALLER = Path(__file__).resolve().parents[2] / "scripts/install-switchboard-runtime.sh"

def ensure_interactive_runtime() -> None:

    if Path(sys.prefix) == RUNTIME:
        try:
            import prompt_toolkit              
            import wcwidth              
        except ImportError as exc:
            raise RuntimeError(f"Room input runtime needs repair. Run: bash '{INSTALLER}'") from exc
        return
    python = RUNTIME / "bin/python"
    if not python.is_file():
        raise RuntimeError(f"Room input runtime is missing. Run: bash '{INSTALLER}'")
    os.execv(str(python), [str(python), str(Path(__file__).with_name("switchboard.py")),
                          *sys.argv[1:]])
