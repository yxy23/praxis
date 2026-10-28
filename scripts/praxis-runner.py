#!/usr/bin/env python3
"""Verify a pinned engine or run synthetic graphs through the actual Amplifier runner."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/praxis45'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'rehearse'))
    parser.add_argument('--engine-root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    engine_root = args.engine_root.expanduser().resolve(strict=True)
    python = engine_root / 'venv/bin/python'
    if Path(sys.prefix).resolve() != (engine_root / 'venv').resolve():
        if not python.is_file():
            parser.error('prepared engine virtual environment is missing')
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    import engine_qualification as q
    lock = q.configure(engine_root / 'source', engine_root / 'engine-closure.json')
    expected = {p['sha256'] for p in lock['packages']}
    if {sha(p) for p in (engine_root / 'wheels').glob('*.whl')} != expected:
        raise ValueError('prepared wheel evidence differs from exact closure')
    base = {'engine':'amplifier_module_loop_pipeline.PipelineEngine', 'commit':lock['commit'],
        'engine_lock_sha256':sha(ROOT / 'services/praxis45/engine-lock.json'),
        'closure_sha256':sha(engine_root / 'engine-closure.json'), 'native_launches':0, 'service_activated':False}
    if args.command == 'verify':
        print(json.dumps(dict(base, state='verified'), sort_keys=True)); return
    if args.output is None:
        parser.error('rehearse requires --output pointing to a new directory')
    target = args.output.expanduser().absolute()
    if target.exists() or target.is_symlink():
        parser.error('output must be new; prior entry intents cannot be reused or retried')
    target.mkdir(parents=True); target=target.resolve()
    from freeze_rehearsal import graph
    trials = []
    for name, behavior, visits, expected_state in (
        ('success',{},['first','second'],'succeeded'),
        ('failure',{'first':'fail'},['first'],'failed'),
        ('uncertain',{'first':'contact_loss'},['first'],'held'),
        ('unsafe_outcome',{'first':'retry'},['first'],'failed')):
        folder=target/name; folder.mkdir()
        dot=graph(['first','second']); (folder/'graph.dot').write_text(dot)
        backend=q.StubBackend('public-'+name, behavior=behavior)
        engine=q.construct_adapter(q.restricted_graph(dot),backend,folder/'engine',
            session_dir=folder/'session',workspace=str(folder),graph_run_id='public-'+name)
        try:
            result=asyncio.run(engine.run())
            state=q.evidence_status(result,backend)
        except q.UnresolvedAttempt:
            state='held'
        if state != expected_state or backend.history != visits or any(n != 1 for n in backend.calls.values()):
            raise AssertionError('runner boundary failed: ' + name)
        # The durable intent must also reject a second entry, even with a new backend.
        replay=q.StubBackend('public-'+name)
        repeated=q.construct_adapter(q.restricted_graph(dot),replay,folder/'repeat-engine',
            session_dir=folder/'session',workspace=str(folder),graph_run_id='public-'+name)
        try:
            asyncio.run(repeated.run())
        except q.UnresolvedAttempt:
            pass
        else:
            raise AssertionError('durable entry replay did not stop')
        if replay.history:
            raise AssertionError('replay reached backend')
        trials.append({'trial':name,'state':state,'visited_nodes':backend.history,
            'graph_sha256':sha(folder/'graph.dot'),'durable_reentry_refused':True})
    files=[{'path':str(p.relative_to(target)),'sha256':sha(p)} for p in sorted(target.rglob('*')) if p.is_file()]
    result=dict(base,state='passed',kind='synthetic-real-engine-rehearsal',trials=trials,files=files,
        limits=['synthetic node backend','no native agent qualification','no provider calls','no ordinary service activation'])
    (target/'receipt.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'state':'passed','trials':len(trials),'native_launches':0,'receipt':str(target/'receipt.json')},sort_keys=True))


if __name__ == '__main__':
    main()
