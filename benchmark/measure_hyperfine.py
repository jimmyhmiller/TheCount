#!/usr/bin/env python3
"""Reaudit a workload survey, then validate it with balanced Hyperfine blocks."""
import argparse
import datetime
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import statistics
import subprocess

from measure_workloads import audit, digest
from compare_files import index_rows


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", type=Path, required=True)
    parser.add_argument("--source-audit-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hyperfine", default="hyperfine")
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--warmup", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 2 or args.warmup < 0:
        parser.error("runs must be at least 2 and warmup must be nonnegative")
    for setting in ("THECOUNT_READERS", "THECOUNT_PHASE_TIMING"):
        if setting in os.environ:
            parser.error(f"unset {setting} to validate default worker settings")
    hyperfine = shutil.which(args.hyperfine)
    if hyperfine is None:
        parser.error("Hyperfine executable not found")
    source = json.loads(args.survey.read_text())
    binaries = {name: cmd[0] for workload in source["workloads"]
                for name, cmd in workload["commands"].items()}
    hashes = {name: digest(path) for name, path in binaries.items()}
    if hashes != {name: source["binary_sha256"][name] for name in binaries}:
        raise SystemExit("counter binary hashes differ from the source survey")
    directory = args.output.with_suffix("").with_name(args.output.stem + "-raw")
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "started_utc": now(), "source_survey_sha256": digest(args.survey),
        "binary_sha256": hashes,
        "hyperfine": {"path": hyperfine, "sha256": digest(hyperfine),
                      "version": subprocess.check_output([hyperfine, "--version"], text=True).strip()},
        "machine": {"hostname": os.uname().nodename, "kernel": os.uname().release,
                    "arch": os.uname().machine, "loadavg_start": os.getloadavg()},
        "settings": {"runs_per_block": args.runs, "warmup_per_command_per_block": args.warmup,
                     "cache": "warm", "shell": "none", "output": "null",
                     "order": "one block per command; rotate positions across blocks",
                     "audit": "all workloads before any timed blocks"},
        "workloads": [],
    }
    # Audit all selected paths/buckets first. No audit or profiler runs alongside
    # the timed blocks; preserve the source survey's exact commands/exclusions.
    for workload in source["workloads"]:
        folder = directory / workload["name"]
        folder.mkdir(exist_ok=True)
        checked = audit(workload["commands"], folder)
        comparison = checked["comparisons"].get("thecount")
        if comparison and not comparison["same_file_and_line_workload"]:
            raise SystemExit(f"{workload['name']}: counters no longer count the same workload")
        if checked["totals"] != workload["audit"]["totals"]:
            raise SystemExit(f"{workload['name']}: audited totals changed")
        previous = args.source_audit_dir / workload["name"]
        if workload.get("exclude_paths"):
            previous = previous / "common-files"
        for name in workload["commands"]:
            old_path = previous / f"{name}-files.json"
            if digest(old_path) != workload["audit"]["per_file_json_sha256"][name]:
                raise SystemExit(f"{workload['name']}: original audit artifact changed")
            # Runtime metadata can change, and equal-code rows can reorder
            # after parallel aggregation. Compare records by path, not JSON bytes.
            def records(path):
                document = json.loads(path.read_text())
                rows = (document["files"] if name != "mezura" else
                        [row for language in document["languages"] for row in language["by_file"]])
                return index_rows(rows, name)
            if records(old_path) != records(folder / f"{name}-files.json"):
                raise SystemExit(f"{workload['name']}: per-file records changed")
        result["workloads"].append({"name": workload["name"], "commands": workload["commands"],
                                    "audit": checked, "blocks": []})
        print(json.dumps({"audited": workload["name"]}), flush=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    rng = random.Random(20261001)
    for workload in result["workloads"]:
        names = list(workload["commands"])
        for block in range(len(names)):
            order = names[block:] + names[:block]
            folder = directory / workload["name"]
            exported = folder / f"block-{block}.json"
            invocation = [hyperfine, "--shell=none", "--output=null", "--style=none",
                          "--warmup", str(args.warmup), "--runs", str(args.runs),
                          "--export-json", str(exported)]
            for name in order:
                invocation.extend(["--command-name", name, shlex.join(workload["commands"][name])])
            started, load = now(), os.getloadavg()
            completed = subprocess.run(invocation, capture_output=True, text=True, check=True)
            (folder / f"block-{block}-stdout.txt").write_text(completed.stdout)
            (folder / f"block-{block}-stderr.txt").write_text(completed.stderr)
            raw = json.loads(exported.read_text())
            if any(len(entry["times"]) != args.runs or any(entry["exit_codes"])
                   for entry in raw["results"]):
                raise SystemExit("incomplete or unsuccessful Hyperfine run")
            workload["blocks"].append({"command": invocation, "order": order,
                                        "started_utc": started, "finished_utc": now(),
                                        "loadavg_start": load, "loadavg_end": os.getloadavg(),
                                        "stdout": completed.stdout, "stderr": completed.stderr,
                                        "hyperfine_json_sha256": digest(exported), "raw": raw})
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        samples = {name: [time for block in workload["blocks"]
                         for entry in block["raw"]["results"] if entry["command"] == name
                         for time in entry["times"]] for name in names}
        workload["summary"] = {name: {"mean_s": statistics.mean(times),
                                       "median_s": statistics.median(times),
                                       "stddev_s": statistics.stdev(times), "runs": len(times)}
                                for name, times in samples.items()}
        workload["comparisons"] = {}
        for reference in names:
            if reference == "thecount":
                continue
            ratios = []
            # Stratify independent resampling by balanced block. Hyperfine runs
            # each command consecutively; individual timings are not paired.
            for _ in range(5000):
                current_sum, reference_sum = 0.0, 0.0
                for block in workload["blocks"]:
                    times = {entry["command"]: entry["times"] for entry in block["raw"]["results"]}
                    current_sum += sum(rng.choices(times["thecount"], k=args.runs))
                    reference_sum += sum(rng.choices(times[reference], k=args.runs))
                ratios.append(reference_sum / current_sum)
            ratios.sort()
            block_ratios = []
            for block in workload["blocks"]:
                means = {entry["command"]: entry["mean"] for entry in block["raw"]["results"]}
                block_ratios.append(means[reference] / means["thecount"])
            workload["comparisons"][reference] = {
                "speedup": workload["summary"][reference]["mean_s"] / workload["summary"]["thecount"]["mean_s"],
                "bootstrap_95pct": [ratios[125], ratios[4874]], "block_speedups": block_ratios}
        result["updated_utc"] = now()
        result["bootstrap_method"] = "5000 independent resamples stratified by block; seed 20261001; percentile interval; conditional on measured blocks, not a guarantee across machine states"
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"completed": workload["name"], "summary": workload["summary"],
                          "comparisons": workload["comparisons"]}), flush=True)


if __name__ == "__main__":
    main()
