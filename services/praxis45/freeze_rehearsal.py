
import os
from pathlib import Path
SOURCE = Path(os.environ.get("PRAXIS_ENGINE_SOURCE", "~/.cache/praxis/engine/source")).expanduser()
ENGINE_CLOSURE = Path(os.environ.get("PRAXIS_ENGINE_CLOSURE", "~/.config/praxis/engine-closure.json")).expanduser()

def graph(names):
    rows = ['digraph rehearsal {', 'start [shape=Mdiamond]; done [shape=Msquare]; halt [shape=Msquare];']
    rows += [name + ' [shape=box, prompt="Bounded evidence audit"];' for name in names]
    rows += ['start -> ' + names[0] + ';']
    for index, name in enumerate(names):
        next_name = names[index+1] if index+1 < len(names) else 'done'
        rows += [name + ' -> ' + next_name + ' [condition="outcome=success"];',
                 name + ' -> halt [condition="outcome=fail"];']
    return '\n'.join(rows + ['}'])
