#!/usr/bin/env python3
"""Prepare the exact public Amplifier closure in a new isolated directory.

No service, provider account, room, registry or active-release pointer is changed.
Use --download for explicit network acquisition or supply verified local inputs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'services/praxis45'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_relative(value):
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in {'..', '.'} for x in p.parts):
        raise ValueError('invalid pinned relative path')
    return p


def locked_inputs():
    raw = (RUNTIME / 'engine-closure.public.json').read_bytes()
    closure = json.loads(raw)
    lock = json.loads((RUNTIME / 'engine-lock.json').read_bytes())
    if hashlib.sha256(raw).hexdigest() != lock['closure_sha256']:
        raise ValueError('public closure hash differs from lock')
    for name in ('commit', 'archive_sha256', 'source_files', 'packages'):
        if closure[name] != lock[name]:
            raise ValueError('public closure differs from pinned ' + name)
    for row in closure['source_files']:
        safe_relative(row['path'])
    return raw, closure


def copy_sources(source, target, rows):
    source = source.resolve(strict=True)
    for row in rows:
        original = source / row['path']
        resolved = original.resolve(strict=True)
        if not resolved.is_relative_to(source) or original.is_symlink() or not original.is_file():
            raise ValueError('source containment/type refused: ' + row['path'])
        if sha(original) != row['sha256']:
            raise ValueError('pinned source mismatch: ' + row['path'])
        dest = target / row['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, dest)


def extract_sources(archive, target, rows):
    wanted = {row['path']: row for row in rows}
    seen = set()
    with tarfile.open(archive, 'r:gz') as tf:
        for entry in tf:
            parts = safe_relative(entry.name).parts
            rel = '/'.join(parts[1:])
            if rel not in wanted:
                continue
            if rel in seen or not entry.isfile():
                raise ValueError('duplicate/nonregular pinned archive entry: ' + rel)
            raw = tf.extractfile(entry).read()
            if hashlib.sha256(raw).hexdigest() != wanted[rel]['sha256']:
                raise ValueError('pinned archive source mismatch: ' + rel)
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            seen.add(rel)
    if seen != set(wanted):
        raise ValueError('archive omits pinned source files')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--source-tree', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    raw, closure = locked_inputs()
    if (platform.system(), platform.machine(), sys.version_info[:2]) != ('Darwin', 'arm64', (3, 14)):
        parser.error('this wheel closure requires macOS arm64 and Python 3.14; other platforms need a separately reviewed lock')
    if args.download == bool(args.source_tree or args.wheelhouse) or (not args.download and not (args.source_tree and args.wheelhouse)):
        parser.error('choose --download OR both --source-tree and --wheelhouse')
    target = args.destination.expanduser().absolute()
    if target.exists() or target.is_symlink():
        parser.error('destination must be new; existing environments are preserved')
    target.mkdir(parents=True)
    target = target.resolve()
    source, wheels = target / 'source', target / 'wheels'
    source.mkdir(); wheels.mkdir()
    (target / 'engine-closure.json').write_bytes(raw)
    requirements = target / 'requirements.lock'
    requirements.write_text(''.join(p['name']+'=='+p['version']+' --hash=sha256:'+p['sha256']+'\n' for p in closure['packages']))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CONFIG_FILE=os.devnull)
    if args.download:
        archive = target / 'upstream.tar.gz'
        url = 'https://codeload.github.com/microsoft/amplifier-bundle-dot-runner/tar.gz/' + closure['commit']
        with urllib.request.urlopen(url, timeout=60) as response, archive.open('xb') as handle:
            shutil.copyfileobj(response, handle)
        if sha(archive) != closure['archive_sha256']:
            raise ValueError('upstream archive hash mismatch; preserve evidence and use a new destination')
        extract_sources(archive, source, closure['source_files'])
        subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'download', '--index-url', 'https://pypi.org/simple',
            '--only-binary=:all:', '--no-deps', '--require-hashes', '-r', str(requirements), '--dest', str(wheels)], check=True, env=env)
    else:
        copy_sources(args.source_tree.expanduser(), source, closure['source_files'])
        inventory = {}
        for file in args.wheelhouse.expanduser().glob('*.whl'):
            if file.is_file() and not file.is_symlink():
                inventory[sha(file)] = file
        for package in closure['packages']:
            original = inventory.get(package['sha256'])
            if original is None:
                raise ValueError('locked wheel missing: ' + package['name'])
            shutil.copyfile(original, wheels / original.name)
    expected = {p['sha256'] for p in closure['packages']}
    actual = {sha(p) for p in wheels.glob('*.whl')}
    if actual != expected or len(list(wheels.glob('*.whl'))) != len(expected):
        raise ValueError('wheel inventory differs from exact closure')
    subprocess.run([sys.executable, '-m', 'venv', str(target / 'venv')], check=True, env=env)
    python = target / 'venv/bin/python'
    subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps', '--require-hashes',
        '--find-links', str(wheels), '-r', str(requirements)], check=True, env=env)
    subprocess.run([str(python), str(ROOT / 'scripts/praxis-runner.py'), 'verify', '--engine-root', str(target)], check=True, env=env)
    print(json.dumps({'state':'prepared', 'engine_root':str(target), 'engine_commit':closure['commit'],
        'closure_sha256':hashlib.sha256(raw).hexdigest(), 'service_activated':False, 'native_launches':0}, sort_keys=True))


if __name__ == '__main__':
    main()
