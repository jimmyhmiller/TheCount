# TheCount

## Summary

`thecount` is an scc-style lines-of-code counter written in Coil. It counts
code / comment / blank lines per language at scc speed, and adds two things
scc doesn't have:

- **Trivial language registration.** `thecount add-lang Jai --like c --ext jai`
  and Jai files count from that moment on. Languages live in a plain-text file
  (`~/.config/thecount/languages`) you can also edit by hand — no rebuild, no
  JSON, no source dive. Define a language from scratch with a handful of
  tokens, or inherit everything from an existing one with `like`.
- **Per-folder rollups.** `--dirs` (with `--depth N`) breaks counts up by
  directory, not just by language or file. Depth is measured from each path
  you name, so `thecount src --dirs` splits src up by its own subdirectories
  instead of reporting one lump row for src. Files directly in a named root
  are labeled `src/.`, distinguishing them from rows such as `src/lib`.

None of scc's estimation extras (COCOMO, complexity scores) — just counting.

## Usage

    thecount [paths...]          count by language (default: .)
    thecount --files             break up by file
    thecount --dirs --depth 2    break up by folder AND language
    thecount --hidden            include hidden files
    thecount --no-ignore         don't honor .gitignore
    thecount --no-config         use built-in language definitions only
    thecount --extensions c,py   count only these extensions
    thecount --output json       emit machine-readable totals
    thecount --files --output json  include sorted per-file counts
    thecount langs               list every known language + extensions
    thecount remove-lang NAME    delete a user-registered language
    thecount --exclude STR       skip paths containing STR (repeatable)
    thecount add-lang NAME [--like BASE] [--ext "a b"] [--line "// #"]
             [--block "/* */"] [--nested] [--quotes "\" '"] [--multi '"""']
             [--file "Somefile"]

## The language config format

One directive per line; tokens are space-separated; `#` starts a comment line.
`$THECOUNT_LANGS` overrides the config path.

    lang Foo Script        # start a stanza; rest of the line is the name
    like c                 # inherit every field from an existing language
    ext foo fooz           # extensions, no dots
    file Foofile           # exact filenames (like Makefile)
    line ;;                # line comment starters
    block {- -}            # block comment start/end pairs
    nested                 # block comments nest
    quotes " '             # single-char string delimiters
    multiline-quotes " '  # these quotes can span physical lines
    multi """ '''          # multiline string delimiters

User languages are loaded on top of the builtins (~160 languages); registering
an extension a builtin already claims overrides it.

## Design

- `src/counter.coil` — byte-level state machine: line comments, block comments
  (with nesting), single-line strings (escape-aware), multiline strings, and
  Python-style docstrings (a multiline string opening a line counts as
  comment; one in value position counts as code — deliberately more consistent
  than scc, which flips to code when a docstring contains an embedded quote).
- `src/lang.coil` / `src/registry.coil` — LangSpec built from space-separated
  token strings; registries map extension / filename / name → spec.
- `src/config.coil` — the user language file: parse, validate, write.
- `src/ignore.coil` — .gitignore subset: literal + glob patterns (`*` `?` `**`),
  anchoring, dir-only, `!` negation, per-directory files, last-match-wins.
- `src/engine.coil` — parallel walker: worker pool over a mutex-guarded
  directory queue; each worker readdirs, gitignore-filters, reads and counts
  inline into its own aggregate; aggregates merge at the end. Binary files are
  skipped by NUL-sniff on the first 8000 bytes.
- `src/walk.coil` — dirent externs (darwin arm64 layout) + path helpers.
- `src/output.coil` — the three tables (language / file / directory). `--files`
  and `--dirs` both carry a Language column; `--dirs` holds one row per
  (directory, language) and orders directories by total code, keeping a
  directory's languages together (see `dir-row-cmp` / `agg-dir-groups!`).
- `src/specialize.coil` — compile-time specialization of the counter (below).
- `src/simd.coil` — vector byte-class bitmasks over `(primitive/llvm-ir …)`.

`coil verify` runs fmt + lint + check + build + tests.

### Compile-time specialized scanners

`count-bytes` is an interpreter over a `LangSpec`: every hot byte costs a loop
over the line-comment tokens, a loop over the block pairs, a byte-by-byte
`match-at`, and a `quote-byte?` scan. For the languages whose delimiters are
known when thecount is compiled, none of that has to happen at runtime.

`src/specialize.coil` holds a table of 26 such languages and a MACRO that walks
it at compile time, emitting one scanner per row (~6,000 lines of generated
Coil) in which dead states are deleted, every delimiter comparison is folded to
a literal byte compare, and state a language cannot vary is not stored. The
registry keeps a `counters` array parallel to `specs`; entries default to
`count-bytes` and `add-specialized!` swaps in the generated scanner. Everything
else — every user-registered language from the config file — keeps running
through the interpreter, so the table is purely an optimization.

The table is also the *only* place those 26 languages' tokens are written: the
registry registers them from it, so the scanner and the registration cannot
drift apart. `tests/specialize_test.coil` holds each generated scanner to
byte-for-byte agreement with `count-bytes` on delimiter-at-EOF, unterminated
strings and blocks, nesting, and docstrings.

Two notes for anyone extending it:

- It is a macro, not `(meta …)`. Meta-generated forms are spliced *after* macro
  expansion, so they cannot call `while`/`cond`/`when`; a macro's output is
  expanded normally.
- Macro hygiene renames template locals, including where a bare symbol is used
  as a struct field name. The generated locals are therefore `n-lines`,
  `n-code`, `cls-tab` and so on, never `lines`/`code`/`classes`.

Read the generated code with `coil expand` (needs a copy of the file with the
`thecount.*` imports stripped — `coil expand` does not read `Coil.toml`).

### The scan: bitmasks instead of a byte-class load

Specializing the *dispatch* turned out to be worth ~1% (see Speed), because only
1.24% of bytes in real C++ are delimiters — the other 98.8% were spending one
byte-class table load each in the ST-NORMAL loop, and that load was the whole
cost of counting.

`src/simd.coil` replaces it with the simdjson approach: load a vector, compare
against the language's delimiter bytes and against whitespace, and extract one
bit per lane. "Is there a delimiter in this chunk" and "is any byte before it
non-blank" then fall out of bit arithmetic, and the scan jumps straight to the
delimiter with a count-trailing-zeros instead of walking to it. A chunk that
would cross the line end falls through to the original byte-at-a-time loop.

This is where the two halves meet: the delimiter set is a compile-time constant
*because* of the specialization table, so the generator emits one
`v16-eq-mask` call per hot byte with a literal operand, and -O3 folds each to a
constant vector compare. A runtime-configurable scanner could not do this.

It is all `(primitive/llvm-ir …)` — no compiler support, same technique as
`coil.simd`. Two details: the loads are `align 1` (arbitrary offsets into a file
buffer), and mask extraction is `bitcast <16 x i1> to i16`, which arm64 has no
single instruction for but LLVM lowers correctly.

## Speed

Faster than scc on every benchmarked repo (hyperfine, warm cache, arm64 mac):

| repo | files | scc | thecount | speedup |
|---|---|---|---|---|
| llvm22 | 139k | 3807 ms | 2821-3216 ms | 1.18-1.34x |
| swc | 69k | 2076 ms | 1564 ms | 1.33x |
| rhino | 56k | 1211 ms | 1037 ms | 1.17x |
| next.js | 27k counted | 721 ms | 641 ms | 1.12x |
| cpython | 4.8k | 108 ms | 111-123 ms | ~par |

thecount uses ~2.5x less CPU than scc for the same work and roughly half
the kernel time.

### What compile-time specialization actually bought

Measured with `src/countbench.coil`, which runs both scanners over the same
buffer (40 passes, arm64 mac). This is the counter alone — end-to-end timings
are dominated by the reader's syscalls.

| corpus | interpreter | + specialized dispatch | + SIMD scan | total |
|---|---|---|---|---|
| 4.2 MB C++ | 303 MB/s | 302 MB/s | 935 MB/s | **3.1x** |
| 535 KB Python | 363 MB/s | 370 MB/s | 869 MB/s | **2.4x** |
| 362 KB Markdown | 282 MB/s | 2600 MB/s | 3250 MB/s | **11.5x** |

The middle column is the lesson. Folding delimiters to literal compares bought
essentially nothing on code-heavy languages: only **1.24% of bytes in the C++
corpus are `/`, `"` or `'`** — one per 80 bytes — so 98.8% of iterations never
reached the code that was specialized. The byte-class table was already keeping
token matching off the hot path; what remained was one table load per byte, and
constant-folding does not remove a load.

Both real wins are algorithmic. Markdown jumped first because with no delimiters
the class table has no purpose, so it drops out and the scan can stop at the
line's first non-space byte. Everything else jumped once the per-byte load
became a vector bitmask.

End-to-end on ladybird (1.8M lines, 20 runs): 285 ms → 280 ms wall, but **user
CPU 329 ms → 164 ms**. Wall time barely moves because counting is no longer the
bottleneck — ~1.1 s of system time across the reader threads is, and that is
unchanged. Any further work on throughput belongs in the reader, not the
counter.

The scan still restarts per line. The current scanner masks the final vector
against the newline when it can safely load from the file buffer, but it still
finds newlines in a separate pass. A combined pass could let the
escaped-quote and inside-string masks be computed branchlessly with a
carry-less-multiply prefix XOR, replacing `backslashes-before` and the ST-STRING
arm outright. Nested block comments still need a real counter, so those
languages keep a sequential pass over the candidate positions. The architecture (informed by scc/ripgrep/dumac writeups):

- **Combined workers.** Four workers on macOS and up to 32 online CPUs on
  Linux by default
  (`THECOUNT_READERS` overrides) walk directories, read files, and count them
  into private aggregates. This removes the per-file transfer between reader
  and counter pools. A condition variable wakes workers when a directory or
  file batch is queued.
- **Cheap syscalls.** Files are opened with openat through the
  enclosing directory's fd so the kernel resolves one path component, not
  the whole path. Reads continue through short returns until EOF; directory
  batches share basenames, not full paths.
- **memchr + byte-class-table counting core** (~330 MB/s/core): memchr
  jumps between newlines/quote-closers/comment-enders; a 256-entry class
  table drives the in-line hot loop. mmap deliberately not used (slower on
  macOS; ripgrep disables it there too).

Tried and rejected: getattrlistbulk enumeration (darwin). It cut sys time
35% but nearly doubled wall time on llvm — the bulk call is synchronously
slower than readdir when you only need names and types; it only pays when
it replaces per-file stat calls (which thecount never makes).

`src/countbench.coil` is a dev tool: single-thread counting throughput on a
50 MB corpus (`coil build src/countbench.coil -o /tmp/cb && /tmp/cb`).

### linebench comparison

`benchmark/thecount.toml` lets [linebench](https://github.com/loc-conformance/linebench)
run TheCount against Mezura and scc with matched extension filters,
hidden-file handling, ignore behavior, and no user language config. Build
TheCount, then run:

    linebench check linux --add benchmark/thecount.toml --counters thecount,mezura,scc --given thecount=/absolute/path/to/thecount
    linebench run linux --add benchmark/thecount.toml --counters thecount,mezura,scc --given thecount=/absolute/path/to/thecount

The benchmark's count check should run before comparing times; otherwise a
counter can appear fast by doing less work. `THECOUNT_PHASE_TIMING=1` prints
worker, walk, file I/O, and scan times to stderr for diagnosis.

To verify the exact file workload, capture TheCount with `--files --output json`
and Mezura with `--by-file --output json` using the same strict linebench flags,
then run `python3 benchmark/compare_files.py thecount.json mezura.json`. The
script checks unique paths, each row's line-bucket sum, aggregate totals,
identical file sets, and each shared file's physical line count. It reports
code/comment/blank differences separately. The per-file JSON array is sorted
by path; totals-only JSON retains its previous `total` shape. Invalid UTF-8
filename bytes use `\udcXX` escapes, which round-trip through Python's
`surrogateescape` convention.

The pinned Linux corpus gave these strict same-work results on 2026-09-29:

| machine | TheCount | Mezura 3.2.0 | TheCount speedup | control drift |
|---|---:|---:|---:|---:|
| Apple M2 Max, macOS, 4 workers | 1,349 ± 105 ms | 3,465 ± 163 ms | 2.57× faster | 8.1% |
| Ryzen AI Max+ 395, Linux, 32 workers | 64 ± 3 ms | 68 ± 3 ms | 1.07× faster | 5.1% |

The Mac's other running jobs strongly affected Mezura: an earlier eight-worker
run on the same corpus had Mezura at 2,502 ms and TheCount at 1,747 ms. The
2× result is therefore specific to the measured Mac state; it does not carry
over to the quiet Linux host. All three counters reported exactly 63,726
files and 36,017,734 lines on macOS, and 63,738 files and 36,018,801 lines on
Linux. The Linux checkout preserves twelve case-colliding names that the Mac
filesystem does not. The raw [Mac](benchmark/results/linux-corpus-macos-run.json)
and [Linux](benchmark/results/linux-corpus-x86-run.json) linebench records are
included for review.

After the counting fixes, a per-file audit of the macOS checkout confirms all
63,726 paths and all 36,017,734 physical lines match Mezura exactly. Of those
files, 210 still differ in code/comment/blank classification: 160 Python files
because TheCount calls docstrings comments, 8 Perl files where Mezura's quote
state is incorrect, and 42 shell files, mostly here-document bodies containing
text that resembles comments. These classification differences are visible in
the JSON audit and are distinct from the benchmark's same-file workload check.
The timing table above predates these fixes. A six-run Mac spot check after
the fixes measured TheCount at 2.257 ± 0.107 s and Mezura at 2.040 ± 0.208 s;
the Mac's concurrent load has made these timings unstable.

On 2026-09-30, the current uncommitted TheCount build was measured again on the
quiet Linux host. The strict per-file audit matched all 63,738 paths and every
file's total lines. Over 30 alternating warm-cache trials, TheCount averaged
**74.39 ± 2.10 ms** and Mezura **67.78 ± 2.86 ms**; Mezura was **1.10× faster**.
TheCount's older Linux binary averaged 64.06 ms in a separate rotating worker
trial, versus 73.61 ms for the current build at 32 workers. This confirms a
regression in the current build without identifying which change caused it.
`strace` counted 127,473 `read` calls for that build versus 63,759 for
the older binary. The new reader's extra EOF check accounts for roughly one
additional read per selected file and is a likely contributor to the slowdown;
the traced syscall times are not comparable to the untraced benchmark times.
Raw [full-run](benchmark/results/linux-corpus-x86-20260930.json),
[worker-count](benchmark/results/linux-corpus-x86-workers-20260930.json), and
[extension-subset](benchmark/results/linux-corpus-x86-subsets-20260930.json)
measurements include individual samples and commands.

On 2026-10-01, a scanner optimization pass reduced the strict Linux time to
**55.30 ± 2.38 ms** in 30 alternating warm-cache trials with a Zen 5 optimized
LLVM build. Mezura averaged **68.46 ± 3.37 ms** in the same trials, making
TheCount **1.24× faster**. The target of 1.30× has not been reached. The generic
Linux build is about 57–58 ms on this host. The optimized binary's per-file JSON
is byte-for-byte identical to the earlier audited output. Raw
[samples](benchmark/results/linux-corpus-x86-20261001-optimized-znver5.json)
include commands, binary hashes, and every timing. This build uses `coil emit-ir`
followed by `zig cc -O3 -march=znver5` and is specific to the tested CPU.

## Known divergences from scc

- Python docstrings: counted as comments consistently (scc miscounts
  docstrings containing embedded quotes as code).
- Language coverage is ~160 languages vs scc's ~300 — but adding one is a
  single command here.
- .gitignore support is the common subset (no `[class]` globs, no
  `.git/info/exclude`, no global core.excludesFile).
- Only walks real directories; symlinks are never followed.

## Portability

Platform-specific constants (dirent layout, _SC_NPROCESSORS_ONLN) are selected
at compile time via `os-pick`, so a Linux build gets the right struct offsets
(verified to compile with `--target x86_64-unknown-linux-gnu`; linking needs a
Linux toolchain).
