#!/usr/bin/env python3
"""CLI coverage and worker-count parity checks against a supplied binary."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix="thecount-walk-") as temporary:
        root = Path(temporary) / "tree"
        root.mkdir()
        expected = set()

        def put(name, contents=b"int x;\n"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            expected.add(name)

        # Many batches exercise growth; sparse directories exercise inline reads.
        for i in range(300):
            put(f"wide/file-{i}.c")
            put(f"wide/unknown-{i}.unrecognized")
        for i in range(40):
            put(f"sparse/{i}/data.json", b'{"answer":42}\n')
            put(f"sparse/{i}/code.ll", b"; comment\ndefine i32 @f() { ret i32 0 }\n")
            put(f"sparse/{i}/README.md", b"# Title\n\nParagraph\n")
        put("upper.F08", b"! comment\nprogram test\nend program test\n")
        put("Makefile", b"# comment\nall:\n\techo hello\n")
        put("invalid-utf8.c", b"// \xff\nint y;\n")
        put("binary.c", b"\x00int z;\n")
        put(".hidden.c")
        put(".git/never.c")
        put("ignored.c")
        put("skip/sub.c")
        put("keep.c")
        put("Foofile", b"// custom\nint custom;\n")
        put("custom.special", b"// custom\nint custom;\n")
        put(".gitignore", b"ignored.c\nskip/\n")
        expected = {name for name in expected
                    if not name.endswith(".unrecognized")
                    and name not in {"binary.c", ".git/never.c", ".gitignore",
                                     "Foofile", "custom.special"}}

        def run(path, flags, readers=None, config=None):
            env = dict(os.environ)
            env.pop("THECOUNT_READERS", None)
            env.pop("THECOUNT_PHASE_TIMING", None)
            if readers is not None:
                env["THECOUNT_READERS"] = str(readers)
            if config is not None:
                env["THECOUNT_LANGS"] = str(config)
            cmd = [binary, str(path), "--files", "--output", "json", *flags]
            result = subprocess.run(cmd, capture_output=True, check=True,
                                    env=env, timeout=30)
            document = json.loads(result.stdout)
            rows = {str(Path(row["path"]).relative_to(root)): row
                    for row in document["files"]}
            assert len(rows) == len(document["files"]), "duplicate paths"
            assert document["total"]["files"] == len(rows)
            for field in ("lines", "code", "comments", "blanks"):
                assert document["total"][field] == sum(r[field] for r in rows.values())
            return rows

        cases = [
            (["--hidden", "--no-ignore", "--no-config"], expected),
            (["--no-ignore", "--no-config"], expected - {".hidden.c"}),
            (["--hidden", "--no-config"], expected - {"ignored.c", "skip/sub.c"}),
            (["--hidden", "--no-ignore", "--no-config", "--extensions", "c"],
             {name for name in expected if name.endswith(".c")}),
            (["--hidden", "--no-ignore", "--no-config", "--exclude", "skip/",
              "--exclude", str(root / "keep.c")], expected - {"skip/sub.c", "keep.c"}),
        ]
        for flags, paths in cases:
            reference = run(root, flags, 1)
            assert reference.keys() == paths, (flags, reference.keys() ^ paths)
            for readers in (None, 8, 32):
                for _ in range(3):
                    assert run(root, flags, readers) == reference, (flags, readers)
        # Exact custom filenames and extensions must survive early selection.
        config = Path(temporary) / "languages"
        config.write_text("lang Custom\nlike c\next special\nfile Foofile\n")
        custom = run(root, ["--hidden", "--no-ignore"], config=config)
        assert custom.keys() == expected | {"Foofile", "custom.special"}
        assert custom["upper.F08"]["lines"] == 3
        assert custom["invalid-utf8.c"]["lines"] == 2
        assert run(root / "keep.c", ["--no-config"]).keys() == {"keep.c"}
        empty = root / "empty"
        empty.mkdir()
        assert not run(empty, ["--no-config"])
        unknown = root / "unknown"
        unknown.mkdir()
        (unknown / "file.unrecognized").write_text("not selected\n")
        assert not run(unknown, ["--no-config"])
    print("walk coverage, filters, custom registrations, and worker parity: PASS")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as failure:
        print(failure.stderr.decode(errors="replace"))
        raise
