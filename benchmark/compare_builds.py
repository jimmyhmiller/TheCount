#!/usr/bin/env python3
"""Audit and benchmark normal-release and Zen 5 builds on the full saved survey."""
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


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--survey', type=Path, required=True)
    p.add_argument('--source-audits', type=Path, required=True)
    p.add_argument('--thecount-release', required=True)
    p.add_argument('--mezura-release', required=True)
    p.add_argument('--mezura-zen5', required=True)
    p.add_argument('--mezura-data', required=True)
    p.add_argument('--hyperfine', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--runs', type=int, default=40)
    args = p.parse_args()
    if args.runs < 2:
        p.error('runs must be at least 2')
    source = json.loads(args.survey.read_text())
    folder = args.output.with_suffix('')
    folder.mkdir(exist_ok=True)
    result = {'started_utc': now(), 'source_sha256': digest(args.survey),
              'affinity': sorted(os.sched_getaffinity(0)), 'hostname': os.uname().nodename,
              'settings': {'cache': 'warm', 'workers': 'product defaults; not equal worker counts',
                           'runs_per_block': args.runs, 'warmups': 10,
                           'order': 'one rotation per variant; all positions balanced'},
              'binary_sha256': {}, 'workloads': []}
    clean = ['/usr/bin/env', '-u', 'THECOUNT_READERS', '-u', 'THECOUNT_PHASE_TIMING',
             '-u', 'MEZURA_PHASE_TIMING']
    for w in source['workloads']:
        tc = w['commands']['thecount']
        commands = {'tc-release': [*clean, args.thecount_release, *tc[1:]],
                    'tc-zen5': [*clean, *tc]}
        if 'mezura' in w['commands']:
            mz = w['commands']['mezura']
            for name, binary in [('mz-stock', mz[0]), ('mz-release', args.mezura_release),
                                 ('mz-zen5', args.mezura_zen5)]:
                commands[name] = [*clean, f'MEZURA_DATA_DIR={args.mezura_data}', binary, *mz[1:]]
        entry = {'name': w['name'], 'commands': commands, 'audit': {}, 'blocks': []}
        refs = {}
        for name, cmd in commands.items():
            is_mz = name.startswith('mz-')
            binary = cmd[len(clean) + (1 if is_mz else 0)]
            result['binary_sha256'][binary] = digest(binary)
            expected_hash = source['binary_sha256']['mezura' if is_mz else 'thecount']
            if name in ('tc-zen5', 'mz-stock') and digest(binary) != expected_hash:
                raise RuntimeError('original benchmark binary changed')
            path = folder / f'{w["name"]}-{name}-files.json'
            with path.open('wb') as out:
                run = subprocess.run([*cmd, '--by-file' if is_mz else '--files'],
                                     stdout=out, stderr=subprocess.PIPE, check=True)
            doc = json.loads(path.read_text())
            records = index_rows([r for lang in doc['languages'] for r in lang['by_file']]
                                 if is_mz else doc['files'], name)
            key = 'mezura' if is_mz else 'thecount'
            if key not in refs:
                previous = json.loads((args.source_audits / w['name'] / f'{key}-files.json').read_text())
                refs[key] = index_rows([r for lang in previous['languages'] for r in lang['by_file']]
                                      if is_mz else previous['files'], key)
            if records != refs[key]:
                raise RuntimeError(f'{w["name"]}: {name} per-file results changed')
            entry['audit'][name] = {'total': doc['total'], 'sha256': digest(path),
                                   'stderr': run.stderr.decode(errors='replace'),
                                   'same_records_as_original': True}
        if 'mezura' in refs and (refs['thecount'].keys() != refs['mezura'].keys() or any(
                refs['thecount'][k]['lines'] != refs['mezura'][k]['lines'] for k in refs['thecount'])):
            raise RuntimeError('unequal file/line workload')
        result['workloads'].append(entry)
        print('audited', w['name'], flush=True)
    result['binary_sha256'][args.hyperfine] = digest(args.hyperfine)
    result['hyperfine_version'] = subprocess.check_output([args.hyperfine, '--version'], text=True).strip()
    # Compilation and every per-file audit finish before any timings begin.
    for entry in result['workloads']:
        names = list(entry['commands'])
        for block in range(len(names)):
            order = names[block:] + names[:block]
            export = folder / f'{entry["name"]}-block-{block}.json'
            invocation = [args.hyperfine, '--shell=none', '--output=null', '--style=none',
                          '--warmup', '10', '--runs', str(args.runs), '--export-json', str(export)]
            for name in order:
                invocation += ['--command-name', name, shlex.join(entry['commands'][name])]
            started, load = now(), os.getloadavg()
            completed = subprocess.run(invocation, capture_output=True, text=True, check=True)
            raw = json.loads(export.read_text())
            if any(any(r['exit_codes']) or len(r['times']) != args.runs for r in raw['results']):
                raise RuntimeError('unsuccessful timing')
            entry['blocks'].append({'invocation': invocation, 'started_utc': started,
                                    'finished_utc': now(), 'loadavg_start': load,
                                    'loadavg_end': os.getloadavg(), 'raw': raw,
                                    'stdout': completed.stdout, 'stderr': completed.stderr})
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        entry['mean_ms'] = {name: statistics.mean(t for b in entry['blocks']
                           for r in b['raw']['results'] if r['command'] == name
                           for t in r['times']) * 1000 for name in names}
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(entry['name'], entry['mean_ms'], flush=True)
    result['finished_utc'] = now()
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
