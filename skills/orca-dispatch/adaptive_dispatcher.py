
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'services/praxis45'))
from adaptive_collectors import execute_selected, SUPPORT
from native_adapter import load_dispatcher

def execute(selector, plan, router, client, *, root, policy, **kwargs):
    return execute_selected(load_dispatcher(), selector, plan, router, client,
                            root=root, policy=policy, **kwargs)
