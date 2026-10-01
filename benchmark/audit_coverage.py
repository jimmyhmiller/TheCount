#!/usr/bin/env python3
"""Audit full language coverage without workload filters or fairness exclusions."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

from compare_files import check_totals, index_rows
from measure_workloads import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thecount", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    audit_dir = args.output.with_suffix("").with_name(args.output.stem + "-audits")
    audit_dir.mkdir(parents=True, exist_ok=True)
    result = {"binary_sha256": {name: digest(getattr(args, name))
                                for name in ("thecount", "baseline")}, "roots": []}
    for i, root in enumerate(args.roots):
        records, summary = {}, {"path": str(root), "counters": {}}
        for name in ("thecount", "baseline"):
            cmd = [getattr(args, name), str(root), "--hidden", "--no-ignore",
                   "--no-config", "--files", "--output", "json"]
            output = audit_dir / f"{i}-{name}.json"
            with output.open("wb") as sink:
                subprocess.run(cmd, stdout=sink, check=True)
            document = json.loads(output.read_text())
            records[name] = index_rows(document["files"], name)
            check_totals(document, records[name], name)
            summary["counters"][name] = {
                "command": cmd, "total": document["total"], "sha256": digest(output),
                "files_by_language": dict(Counter(row["language"]
                                                  for row in document["files"]))}
        current, baseline = records["thecount"], records["baseline"]
        summary["only_current"] = sorted(current.keys() - baseline.keys())
        summary["only_baseline"] = sorted(baseline.keys() - current.keys())
        summary["differing_rows"] = sorted(path for path in current.keys() & baseline.keys()
                                           if current[path] != baseline[path])
        result["roots"].append(summary)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"path": str(root), "files": len(current),
                          "added": summary["only_current"],
                          "removed": len(summary["only_baseline"]),
                          "changed": len(summary["differing_rows"])}), flush=True)
        if summary["only_baseline"] or summary["differing_rows"]:
            raise SystemExit("coverage regression")


if __name__ == "__main__":
    main()
