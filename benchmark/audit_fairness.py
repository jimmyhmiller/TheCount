#!/usr/bin/env python3
"""Check stock/native Mezura and matched 32-worker budgets on saved workloads."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import statistics
import subprocess

from compare_files import index_rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--survey', type=Path, required=True)
    p.add_argument('--native-mezura', required=True)
    p.add_argument('--mezura-data', required=True)
    p.add_argument('--hyperfine', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--runs', type=int, default=40)
    args = p.parse_args()
    source = json.loads(args.survey.read_text())
    folder = args.output.with_suffix('')
    folder.mkdir(exist_ok=True)
    result = {'affinity': sorted(os.sched_getaffinity(0)), 'cache': 'warm',
              'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'runs_per_block': args.runs, 'warmups': 10,
              'order': 'three rotated blocks; not all seven positions balanced',
              'native_build': 'Mezura v3.2.0, cargo build --release --locked -p mezura; RUSTFLAGS=-C target-cpu=znver5',
              'binary_sha256': {}, 'workloads': []}
    clean = ['/usr/bin/env', '-u', 'THECOUNT_READERS', '-u', 'THECOUNT_PHASE_TIMING',
             '-u', 'MEZURA_PHASE_TIMING']
    for w in source['workloads']:
        if w['name'] not in ('linux-control', 'scriptc-c', 'llvm-fortran'):
            continue
        tc, mz = w['commands']['thecount'], w['commands']['mezura']
        native = [args.native_mezura, *mz[1:]]
        for path in (tc[0], mz[0], native[0], args.hyperfine):
            result['binary_sha256'][path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        env = [f'MEZURA_DATA_DIR={args.mezura_data}']
        cmds = {'tc-default': [*clean, *tc],
                'tc-32': [*clean, 'THECOUNT_READERS=32', *tc],
                'mz-stock-default': [*clean, *env, *mz],
                'mz-native-default': [*clean, *env, *native],
                'mz-native-4+28': [*clean, *env, *native, '--threads', '4', '28'],
                'mz-native-8+24': [*clean, *env, *native, '--threads', '8', '24'],
                'mz-native-16+16': [*clean, *env, *native, '--threads', '16', '16']}
        entry = {'name': w['name'], 'commands': cmds, 'blocks': [], 'audit': {}}
        # Each override must preserve the exact records of its stock counterpart.
        refs = {}
        for name, cmd in cmds.items():
            is_mz = name.startswith('mz-')
            raw = folder / f'{w["name"]}-{name}-files.json'
            with raw.open('wb') as out:
                subprocess.run([*cmd, '--by-file' if is_mz else '--files'],
                               stdout=out, stderr=subprocess.PIPE, check=True)
            doc = json.loads(raw.read_text())
            rows = ([r for lang in doc['languages'] for r in lang['by_file']]
                    if is_mz else doc['files'])
            records = index_rows(rows, name)
            key = 'mz' if is_mz else 'tc'
            if key in refs and records != refs[key]:
                raise RuntimeError(f'{w["name"]}: changed records in {name}')
            refs[key] = records
            entry['audit'][name] = {'total': doc['total'],
                                   'sha256': hashlib.sha256(raw.read_bytes()).hexdigest()}
        if refs['tc'].keys() != refs['mz'].keys() or any(
                refs['tc'][k]['lines'] != refs['mz'][k]['lines'] for k in refs['tc']):
            raise RuntimeError('unequal file/line workload')
        result['workloads'].append(entry)
    # Finish every audit before starting any timed runs.
    for entry in result['workloads']:
        names = list(entry['commands'])
        for block in range(3):
            order = names[block * 2:] + names[:block * 2]
            export = folder / f'{entry["name"]}-block-{block}.json'
            invocation = [args.hyperfine, '--shell=none', '--output=null', '--style=none',
                          '--warmup', '10', '--runs', str(args.runs), '--export-json', str(export)]
            for name in order:
                invocation += ['--command-name', name, shlex.join(entry['commands'][name])]
            completed = subprocess.run(invocation, capture_output=True, text=True, check=True)
            raw = json.loads(export.read_text())
            if any(any(r['exit_codes']) or len(r['times']) != args.runs for r in raw['results']):
                raise RuntimeError('unsuccessful timing')
            entry['blocks'].append({'invocation': invocation, 'raw': raw,
                                    'stdout': completed.stdout, 'stderr': completed.stderr})
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        entry['mean_ms'] = {name: statistics.mean(t for b in entry['blocks']
                           for r in b['raw']['results'] if r['command'] == name
                           for t in r['times']) * 1000 for name in names}
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(entry['name'], entry['mean_ms'], flush=True)
    result['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
