#!/usr/bin/env python3
"""Check that TheCount and Mezura counted the same files and total lines.

Inputs are TheCount's ``--files --output json`` and Mezura's
``--by-file --output json``, collected with the strict linebench flags.
Classification differences are reported separately from workload parity.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys


BUCKETS = ("code", "comments", "blanks")


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def index_rows(rows: list[dict], source: str) -> dict[str, dict]:
    indexed = {}
    for row in rows:
        path = row["path"]
        if path in indexed:
            raise ValueError(f"{source}: duplicate path: {path}")
        if row["lines"] != sum(row[bucket] for bucket in BUCKETS):
            raise ValueError(f"{source}: buckets do not add to lines: {path}")
        indexed[path] = row
    return indexed


def check_totals(document: dict, rows: dict[str, dict], source: str) -> None:
    totals = document["total"]
    for field in ("files", "lines", *BUCKETS):
        actual = len(rows) if field == "files" else sum(row[field] for row in rows.values())
        if totals[field] != actual:
            raise ValueError(f"{source}: total.{field}={totals[field]}, rows sum to {actual}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thecount", type=Path, help="TheCount --files --output json")
    parser.add_argument("mezura", type=Path, help="Mezura --by-file --output json")
    parser.add_argument("--show", type=int, default=15, help="maximum differing rows to show")
    args = parser.parse_args()

    thecount = read_json(args.thecount)
    mezura = read_json(args.mezura)
    if "files" not in thecount:
        raise ValueError("TheCount JSON has no files array; pass --files")
    if any("by_file" not in language for language in mezura["languages"]):
        raise ValueError("Mezura JSON has no complete by_file data; pass --by-file without --top")

    tc = index_rows(thecount["files"], "TheCount")
    mz = index_rows(
        [row for language in mezura["languages"] for row in language["by_file"]],
        "Mezura",
    )
    check_totals(thecount, tc, "TheCount")
    check_totals(mezura, mz, "Mezura")

    only_tc = sorted(tc.keys() - mz.keys())
    only_mz = sorted(mz.keys() - tc.keys())
    line_diffs = sorted(
        (path, tc[path]["lines"], mz[path]["lines"])
        for path in tc.keys() & mz.keys()
        if tc[path]["lines"] != mz[path]["lines"]
    )
    print(f"TheCount files: {len(tc):,}; Mezura files: {len(mz):,}")
    print(f"Only TheCount: {len(only_tc):,}; only Mezura: {len(only_mz):,}")
    print(f"Per-file line differences: {len(line_diffs):,}")
    for title, rows in (("Only TheCount", only_tc), ("Only Mezura", only_mz)):
        for path in rows[: args.show]:
            print(f"  {title}: {path}")
    for path, tc_lines, mz_lines in line_diffs[: args.show]:
        print(f"  Lines: {path}: TheCount={tc_lines}, Mezura={mz_lines}")

    shared = tc.keys() & mz.keys()
    class_diffs = []
    by_extension = Counter()
    for path in shared:
        delta = tuple(tc[path][bucket] - mz[path][bucket] for bucket in BUCKETS)
        if any(delta):
            class_diffs.append((path, delta))
            by_extension[Path(path).suffix.lower() or "[none]"] += 1
    class_diffs.sort(key=lambda entry: (-sum(abs(x) for x in entry[1]), entry[0]))
    print(f"Classification differences: {len(class_diffs):,} files")
    print("By extension:", ", ".join(f"{ext}={count}" for ext, count in by_extension.most_common()))
    for path, delta in class_diffs[: args.show]:
        print(f"  {path}: code {delta[0]:+}, comments {delta[1]:+}, blanks {delta[2]:+}")

    same_work = not (only_tc or only_mz or line_diffs)
    print("Same file and line workload:", "yes" if same_work else "NO")
    return 0 if same_work else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"comparison failed: {error}", file=sys.stderr)
        sys.exit(2)
