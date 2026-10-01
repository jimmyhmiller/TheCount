#!/usr/bin/env python3
"""Rotate matched warm-cache trials, retaining commands and every sample."""
import argparse
import datetime
import hashlib
import json
import os
import statistics
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--thecount", required=True)
parser.add_argument("--mezura", required=True)
parser.add_argument("--corpus", required=True)
parser.add_argument("--baseline")
parser.add_argument("--generic")
parser.add_argument("--trials", type=int, default=60)
args = parser.parse_args()
if args.trials < 2:
    parser.error("--trials must be at least 2")

tc_args = [args.corpus, "--extensions", "c,h,s,asm,py,pl,pm,rs,sh",
           "--hidden", "--no-ignore", "--no-config", "--output", "json"]
commands = {
    "thecount": [args.thecount, *tc_args],
    "mezura": [args.mezura, args.corpus, "--languages", "c,h,s,asm,py,pl,pm,rs,sh",
               "--no-gitignore", "--no-ignore-files", "--search-in-dotted",
               "--no-local", "--no-default-config", "--hide", "keywords",
               "--count-minified", "--count-generated", "--count-not-code",
               "--counting", "region", "--no-shebang", "--output", "json"],
}
for name in ("baseline", "generic"):
    binary = getattr(args, name)
    if binary:
        commands[name] = [binary, *tc_args]


def once(name):
    start = time.perf_counter_ns()
    subprocess.run(commands[name], stdout=subprocess.DEVNULL, check=True)
    return (time.perf_counter_ns() - start) / 1e9


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


for _ in range(5):
    for name in commands:
        once(name)
samples = {name: [] for name in commands}
names = list(commands)
for trial in range(args.trials):
    offset = trial % len(names)
    for name in names[offset:] + names[:offset]:
        samples[name].append(once(name))

summary = {
    name: {"mean_s": statistics.mean(xs), "median_s": statistics.median(xs),
           "stddev_s": statistics.stdev(xs), "min_s": min(xs), "max_s": max(xs)}
    for name, xs in samples.items()
}
print(json.dumps({
    "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "machine": {"hostname": os.uname().nodename, "arch": os.uname().machine,
                "loadavg_end": os.getloadavg()},
    "corpus": {"path": args.corpus, "commit": subprocess.check_output(
        ["git", "-C", args.corpus, "rev-parse", "HEAD"], text=True).strip()},
    "settings": {"warmup_each": 5, "trials_each": args.trials,
                 "order": "rotating", "readers": os.environ.get("THECOUNT_READERS", "default"),
                 "output": "JSON totals to /dev/null"},
    "commands": commands,
    "binary_sha256": {name: sha256(cmd[0]) for name, cmd in commands.items()},
    "samples_s": samples,
    "summary": summary,
    "speedup_thecount_over_mezura": summary["mezura"]["mean_s"] / summary["thecount"]["mean_s"],
}, indent=2))
