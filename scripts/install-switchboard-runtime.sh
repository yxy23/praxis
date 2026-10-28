#!/bin/bash
# Explicit local setup only: room launch never downloads dependencies.
set -euo pipefail
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${HOME}/.local/share/praxis-switchboard/.venv"
python3 -m venv "$runtime_dir"
"$runtime_dir/bin/python" -m pip install --disable-pip-version-check \
  -r "$source_root/services/praxis-switchboard/requirements.txt"
"$runtime_dir/bin/python" -c 'import prompt_toolkit, wcwidth; print("Room editor ready:", prompt_toolkit.__version__, "wcwidth", wcwidth.__version__)'
