#!/usr/bin/env python3
"""Audit selected files before rotating warm-cache trials over real workloads."""
import argparse
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

from compare_files import check_totals, index_rows


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def commands(workload, binaries):
    root, exts = workload["path"], workload["extensions"]
    tc = [root, "--extensions", exts, "--hidden", "--no-ignore", "--no-config",
          "--output", "json"]
    mz = [root, "--languages", exts, "--no-gitignore", "--no-ignore-files",
          "--search-in-dotted", "--no-local", "--no-default-config", "--hide",
          "keywords", "--count-minified", "--count-generated", "--count-not-code",
          "--counting", "region", "--no-shebang", "--output", "json"]
    for path in workload.get("exclude_paths", []):
        tc.extend(["--exclude", path])
    # Mezura aliases extensions to whole languages, so constrain its extra
    # extensions explicitly before interpreting a matched-work comparison.
    excluded = ["*." + ext for ext in workload.get("mezura_exclude_extensions", [])]
    # TheCount's path exclusions are literal substrings; Mezura's are globs.
    # Escape glob metacharacters to make the symmetric paths literal there too.
    escaped = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]",
               "{": "[{]", "}": "[}]"}
    excluded.extend("".join(escaped.get(c, c) for c in path)
                    for path in workload.get("exclude_paths", []))
    if excluded:
        if any("," in path for path in excluded):
            raise ValueError("Mezura's comma-separated exclusions cannot represent this path")
        mz.extend(["--exclude", ",".join(excluded)])
    return {name: [binary, *(mz if name == "mezura" else tc)]
            for name, binary in binaries.items()
            if name != "mezura" or workload.get("mezura_supported", True)}


def audit(cmds, directory):
    rows, totals, hashes, diagnostics = {}, {}, {}, {}
    for name, cmd in cmds.items():
        path = directory / f"{name}-files.json"
        stderr_path = directory / f"{name}-stderr.txt"
        with path.open("wb") as output, stderr_path.open("wb") as stderr:
            subprocess.run([*cmd, "--by-file" if name == "mezura" else "--files"],
                           stdout=output, stderr=stderr, check=True)
        diagnostics[name] = stderr_path.read_text(errors="replace")
        hashes[name] = digest(path)
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
        file_rows = ([row for language in document["languages"]
                      for row in language["by_file"]]
                     if name == "mezura" else document["files"])
        rows[name] = index_rows(file_rows, name)
        check_totals(document, rows[name], name)
        totals[name] = document["total"]
    reference = rows.get("mezura", {})
    comparisons = {}
    for name, files in rows.items():
        if name == "mezura" or "mezura" not in rows:
            continue
        common = files.keys() & reference.keys()
        only_here = sorted(files.keys() - reference.keys())
        only_reference = sorted(reference.keys() - files.keys())
        line_diffs = sorted(p for p in common if files[p]["lines"] != reference[p]["lines"])
        class_diffs = sorted(p for p in common if any(
            files[p][field] != reference[p][field] for field in ("code", "comments", "blanks")))
        comparisons[name] = {
            "same_file_and_line_workload": not (only_here or only_reference or line_diffs),
            "only_counter": len(only_here), "only_mezura": len(only_reference),
            "line_differences": len(line_diffs), "classification_differences": len(class_diffs),
            "excluded_path_candidates": only_here + only_reference,
            "classification_extensions": dict(Counter(Path(p).suffix.lower() for p in class_diffs)),
            "examples": {"only_counter": only_here[:5], "only_mezura": only_reference[:5],
                         "line_differences": [
                             {"path": p, "counter": files[p]["lines"], "mezura": reference[p]["lines"]}
                             for p in line_diffs[:5]]},
        }
    baseline_diffs = sorted(p for p in rows["thecount"].keys() & rows["baseline"].keys()
                            if rows["thecount"][p] != rows["baseline"][p])
    selected_sizes = [os.stat(p).st_size for p in rows["thecount"]]
    return {
        "totals": totals, "comparisons": comparisons, "per_file_json_sha256": hashes,
        "diagnostics": diagnostics,
        "current_vs_baseline": {
            "same_paths": rows["thecount"].keys() == rows["baseline"].keys(),
            "only_current": sorted(rows["thecount"].keys() - rows["baseline"].keys()),
            "only_baseline": sorted(rows["baseline"].keys() - rows["thecount"].keys()),
            "differing_rows": len(baseline_diffs), "examples": baseline_diffs[:5]},
        "selected_bytes": sum(selected_sizes),
        "median_file_bytes": statistics.median(selected_sizes) if selected_sizes else 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thecount", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--mezura", required=True)
    parser.add_argument("--trials", type=int, default=30)
    args = parser.parse_args()
    if args.trials < 2:
        parser.error("--trials must be at least 2")
    binaries = {name: getattr(args, name) for name in ("thecount", "mezura", "baseline")}
    manifest = json.loads(args.manifest.read_text())
    result = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "machine": {"hostname": os.uname().nodename, "arch": os.uname().machine,
                    "kernel": os.uname().release, "loadavg_start": os.getloadavg()},
        "binary_sha256": {name: digest(path) for name, path in binaries.items()},
        "settings": {"warmup_each": 3, "trials_each": args.trials, "order": "rotating",
                     "cache": "warm", "readers": os.environ.get("THECOUNT_READERS", "default")},
        "workloads": [],
    }
    audit_root = args.output.with_suffix("").with_name(args.output.stem + "-audits")
    audit_root.mkdir(parents=True, exist_ok=True)
    for workload in manifest:
        cmds = commands(workload, binaries)
        directory = audit_root / workload["name"]
        directory.mkdir(exist_ok=True)
        print(json.dumps({"auditing": workload["name"]}), flush=True)
        checked = audit(cmds, directory)
        initial = checked
        if "thecount" in checked["comparisons"]:
            comparison = checked["comparisons"]["thecount"]
            candidates = comparison["excluded_path_candidates"]
            if candidates:
                workload = {**workload, "exclude_paths": candidates}
                cmds = commands(workload, binaries)
                directory = directory / "common-files"
                directory.mkdir(exist_ok=True)
                checked = audit(cmds, directory)
        entry = {**workload, "commands": cmds, "audit": checked,
                 "full_selection_audit": initial}

        if checked["comparisons"] and not checked["comparisons"]["thecount"]["same_file_and_line_workload"]:
            raise ValueError(f"{workload['name']}: workload still differs after symmetric path exclusions")

        def once(name):
            start = time.perf_counter_ns()
            subprocess.run(cmds[name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            return (time.perf_counter_ns() - start) / 1e9

        for _ in range(3):
            for name in cmds:
                once(name)
        samples = {name: [] for name in cmds}
        names = list(cmds)
        for trial in range(args.trials):
            offset = trial % len(names)
            for name in names[offset:] + names[:offset]:
                samples[name].append(once(name))
        summary = {name: {"mean_s": statistics.mean(xs), "median_s": statistics.median(xs),
                          "stddev_s": statistics.stdev(xs), "min_s": min(xs), "max_s": max(xs)}
                   for name, xs in samples.items()}
        entry.update(samples_s=samples, summary=summary,
                     speedup_over_mezura=(summary["mezura"]["mean_s"] / summary["thecount"]["mean_s"]
                                         if "mezura" in summary else None),
                     speedup_over_baseline=summary["baseline"]["mean_s"] / summary["thecount"]["mean_s"])
        result["workloads"].append(entry)
        result["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result["machine"]["loadavg_end"] = os.getloadavg()
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"completed": workload["name"], "files": checked["totals"]["thecount"]["files"],
                          "same_work": (checked["comparisons"]["thecount"]["same_file_and_line_workload"]
                                        if checked["comparisons"] else None),
                          "excluded_paths": len(workload.get("exclude_paths", [])),
                          "current_baseline_rows_differ": checked["current_vs_baseline"]["differing_rows"],
                          "mean_ms": {n: round(v["mean_s"] * 1000, 3) for n, v in summary.items()},
                          "speedup": (round(entry["speedup_over_mezura"], 3)
                                      if entry["speedup_over_mezura"] else None)}), flush=True)


if __name__ == "__main__":
    main()
