#!/usr/bin/env python3
"""Separate sparse-tree traversal from counting on identical selected contents."""
import argparse
from collections import Counter
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess

from measure_workloads import audit, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survey", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--hyperfine", required=True)
    args = parser.parse_args()
    if "THECOUNT_READERS" in os.environ or "THECOUNT_PHASE_TIMING" in os.environ:
        parser.error("unset worker/timing overrides")
    args.directory.mkdir(parents=True, exist_ok=True)
    source = json.loads(args.survey.read_text())
    result = {"source_sha256": digest(args.survey), "binary_sha256": source["binary_sha256"],
              "hyperfine_version": subprocess.check_output([args.hyperfine, "--version"], text=True).strip(),
              "settings": {"phase_samples": 20, "hyperfine_runs_per_block": 60,
                           "warmup": 10, "shell": "none", "output": "null"}, "workloads": []}
    for workload in source["workloads"]:
        if workload["name"] not in ("scriptc-c", "llvm-fortran"):
            continue
        cmds = workload["commands"]
        for name, cmd in cmds.items():
            assert digest(cmd[0]) == source["binary_sha256"][name]
        root = Path(workload["path"])
        folder = args.directory / workload["name"]
        compact = folder / "selected-only"
        compact.mkdir(parents=True, exist_ok=False)
        original = json.loads((args.audit_dir / workload["name"] / "thecount-files.json").read_text())
        directory_count, entry_count = 0, 0
        for path, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git" and not (Path(path) / name).is_symlink()]
            directory_count += 1
            entry_count += len(files)
        mapping = {}
        for i, row in enumerate(original["files"]):
            target = compact / f"{i:06d}-{Path(row['path']).name}"
            try:
                os.link(row["path"], target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                shutil.copyfile(row["path"], target)
            mapping[str(target)] = row
        (folder / "origins.json").write_text(json.dumps(mapping, indent=2) + "\n")
        compact_cmds = {name: [cmd[0], str(compact), *cmd[2:]] for name, cmd in cmds.items()}
        checked = audit(compact_cmds, folder)
        assert checked["comparisons"]["thecount"]["same_file_and_line_workload"]
        assert checked["totals"]["thecount"] == original["total"]
        compact_rows = json.loads((folder / "thecount-files.json").read_text())["files"]
        for row in compact_rows:
            assert {k: v for k, v in row.items() if k != "path"} == {
                k: v for k, v in mapping[row["path"]].items() if k != "path"}
        phases = []
        for _ in range(20):
            env = {**os.environ, "THECOUNT_PHASE_TIMING": "1"}
            run = subprocess.run(cmds["thecount"], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, check=True)
            measurements = {name: int(value) for name, value in
                            re.findall(r"(worker|walk|io|scan) ns=(\d+)", run.stderr)}
            measurements["workers"] = len(re.findall(r"\d+:\d+/\d+", run.stderr))
            phases.append({"measurements": measurements, "stderr": run.stderr})
        walk_only = list(cmds["thecount"])
        walk_only[walk_only.index("--extensions") + 1] = "__benchmark_no_files__"
        variants = {"real-thecount": cmds["thecount"], "real-mezura": cmds["mezura"],
                    "walk-only-thecount": walk_only,
                    "compact-thecount": compact_cmds["thecount"],
                    "compact-mezura": compact_cmds["mezura"]}
        entry = {"name": workload["name"], "source_root": str(root),
                 "directories": directory_count, "non_directory_entries": entry_count,
                 "selected_files": len(original["files"]),
                 "selected_bytes": workload["audit"]["selected_bytes"],
                 "median_selected_bytes": workload["audit"]["median_file_bytes"],
                 "compact_audit": checked, "phases": phases,
                 "worker_count_distribution": dict(Counter(p["measurements"]["workers"] for p in phases)),
                 "variants": variants, "blocks": []}
        names = list(variants)
        for i in range(len(names)):
            order = names[i:] + names[:i]
            exported = folder / f"hyperfine-{i}.json"
            invocation = [args.hyperfine, "--shell=none", "--output=null", "--style=none",
                          "--warmup", "10", "--runs", "60", "--export-json", str(exported)]
            for name in order:
                invocation.extend(["--command-name", name, shlex.join(variants[name])])
            completed = subprocess.run(invocation, capture_output=True, text=True, check=True)
            raw = json.loads(exported.read_text())
            assert all(not any(r["exit_codes"]) for r in raw["results"])
            entry["blocks"].append({"command": invocation, "raw": raw,
                                    "stdout": completed.stdout, "stderr": completed.stderr})
        entry["mean_s"] = {name: statistics.mean(t for block in entry["blocks"]
                            for run in block["raw"]["results"] if run["command"] == name
                            for t in run["times"]) for name in variants}
        result["workloads"].append(entry)
        (args.directory / "results.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"name": entry["name"], "directories": directory_count,
                          "entries": entry_count, "workers": entry["worker_count_distribution"],
                          "mean_ms": {k: round(v * 1000, 3) for k, v in entry["mean_s"].items()}}), flush=True)


if __name__ == "__main__":
    main()
