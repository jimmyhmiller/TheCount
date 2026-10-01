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
- `src/cscan.coil` — fused newline/syntax scanner for non-nesting C-style
  languages; ordinary line spans are classified together using bit masks.

`coil verify` runs fmt + lint + check + build + tests.

### Compile-time specialized scanners

`count-bytes` is an interpreter over a `LangSpec`: every hot byte costs a loop
over the line-comment tokens, a loop over the block pairs, a byte-by-byte
`match-at`, and a `quote-byte?` scan. For the languages whose delimiters are
known when thecount is compiled, none of that has to happen at runtime.

`src/specialize.coil` holds a table of 26 such languages and a MACRO that walks
it at compile time, emitting a scanner entry point per row. Non-nesting C-style rows with exactly
`//`, `/* */`, and single/double quotes select the shared fused scanner; other
rows generate scanners in which dead states are deleted, every delimiter
comparison is folded to a literal byte compare, and state a language cannot
vary is not stored. The
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

- **Combined workers.** Four workers on macOS; Linux starts up to eight and grows
  toward its online CPU count (maximum 32) as selected file batches accumulate
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


### Linux target reached: 1.53× Mezura throughput

On 2026-10-01, 60 rotating warm-cache trials on `computer.jimmyhmiller.com`
(Ryzen AI Max+ 395), with five warmups per binary, measured:

| binary | mean ± sample SD |
|---|---:|
| TheCount, Zen 5 optimized | **44.61 ± 1.79 ms** |
| Mezura 3.2.0 | **68.40 ± 3.36 ms** |
| prior Zen 5 TheCount | 55.64 ± 2.61 ms |
| current generic x86-64 TheCount | 54.32 ± 2.10 ms |

The optimized result is **1.533× throughput** (53.3% faster, 34.8% less elapsed
time) than Mezura. A paired bootstrap over the rotating rounds gives a 95%
interval of 1.508–1.558×. The generic build does not meet the 1.5× target.
[Raw samples and build metadata](benchmark/results/linux-corpus-x86-20261001-private-fds.json)
include all commands and binary hashes. Run `benchmark/measure_linux.py --help`
for the reproducible timing harness.

Two changes account for the improvement:

- Each Linux worker calls `unshare(CLONE_FILES)` before opening files. Workers
  exchange paths and memory, never descriptors, so they can use private
  descriptor tables and avoid contention on a shared table. If the kernel
  rejects the call, the worker continues with its shared table. macOS skips
  the call. See the [Linux API documentation](https://man7.org/linux/man-pages/man2/unshare.2.html).
- The C-family scanner processes newlines and syntax candidates together. In
  ordinary spans, subtracting non-whitespace bits from newline bits identifies
  nonblank line endings; population counts classify several lines together.
  Delimiter and quote transitions still inspect the original bounded buffer,
  including tokens crossing vector boundaries. Other language scanners retain
  their existing behavior.

A separate 60-round rotating
[ablation](benchmark/results/linux-corpus-x86-20261001-private-fds-ablation.json)
measured the prior optimized scanner at 54.83 ms, private descriptor tables
alone at 45.82 ms, and both changes at 44.25 ms; Mezura was 69.14 ms.

All 63,738 selected paths and 36,018,801 physical lines match Mezura, and the
entire per-file JSON is byte-identical to the previous audited TheCount output.
The existing 213 classification differences (160 Python, 45 shell, 8 Perl)
remain. Generic and optimized binaries emit identical per-file JSON. Injecting
`EPERM` into every `unshare` call with `strace` also produces identical totals.
`coil verify` passes all 100 tests, including randomized differential syntax
cases and every partial vector tail length.

The optimized build uses LLVM 22.1.8 (matching the emitted IR). Generate a Linux
ELF object on either platform:

    coil emit-ir src/main.coil --target x86_64-unknown-linux-gnu > thecount.ll
    opt -O3 thecount.ll -o thecount.bc
    llc -O3 -mcpu=znver5 -relocation-model=pic -filetype=obj thecount.bc -o thecount.o

Then link on Linux (the measured build used Zig 0.16.0):

    zig cc thecount.o -o thecount -lpthread -lm -ldl

`znver5` is specific to the tested CPU. Use `-mcpu=x86-64` to emit a portable
x86-64 object. Older LLVM parsers on the Linux host could not accept attributes
emitted by LLVM 22.1.8, so optimization and object emission used that version on
the Mac, followed by Linux linking.

### Varied real Linux workloads — optimized follow-up

The final 2026-10-01 build wins **all 14 supported Mezura comparisons** on
`computer.jimmyhmiller.com`. JSON and LLVM IR, unsupported by this Mezura
installation, also beat the older TheCount (`thecount-v17-znver5`). These are
60 rotating warm-cache trials per binary, three warmups, and per-file audits
before timing, using the same LLVM 22.1.8 / Zen 5 build pipeline.

| workload | files | TheCount | reference | throughput ratio |
|---|---:|---:|---:|---:|
| linux-control | 63,738 | 44.57 ms | 67.61 ms (Mezura) | 1.52× |
| llvm-mixed | 77,565 | 39.56 ms | 63.81 ms (Mezura) | 1.61× |
| llvm-cpp | 49,727 | 25.35 ms | 47.59 ms (Mezura) | 1.88× |
| llvm-python | 2,631 | 16.48 ms | 18.39 ms (Mezura) | 1.12× |
| test262-javascript | 53,711 | 12.08 ms | 35.39 ms (Mezura) | 2.93× |
| scriptc-web | 9,763 | 10.68 ms | 18.39 ms (Mezura) | 1.72× |
| scriptc-c | 1,490 | 7.46 ms | 8.61 ms (Mezura) | 1.15× |
| projects-rust | 685 | 3.08 ms | 6.22 ms (Mezura) | 2.02× |
| projects-mixed | 1,310 | 32.20 ms | 43.38 ms (Mezura) | 1.35× |
| abseil-cpp | 1,171 | 1.77 ms | 5.70 ms (Mezura) | 3.23× |
| heapster-rust | 67 | 0.69 ms | 4.75 ms (Mezura) | 6.87× |
| perfbot-json | 1,669 | 2.76 ms | 4.39 ms (older TheCount) | 1.59× |
| single-large-c | 1 | 4.46 ms | 7.07 ms (Mezura) | 1.58× |
| single-large-js | 1 | 4.11 ms | 10.38 ms (Mezura) | 2.52× |
| llvm-fortran | 3,061 | 17.16 ms | 18.70 ms (Mezura) | 1.09× |
| llvm-ir | 41,539 | 41.46 ms | 52.05 ms (older TheCount) | 1.26× |

Ratios measure throughput, reference elapsed time divided by TheCount elapsed
time. The original 50% throughput target is met on the Linux control by its
sample mean (1.52×); ten of fourteen supported comparisons reach 1.5×. Every
supported comparison's paired bootstrap 95% interval exceeds 1×, including
Python and Fortran. This does not establish a 50% advantage on every workload
or predict cold-cache performance.

The previous Python and Fortran losses came from sparse language selection
through a large tree. The walker now resolves language selection before name
allocation, path allocation, and file-batch scheduling, then carries that
immutable registry index to the reader. This also avoids a second lookup.

The JSON regression was caused by excess worker overhead in its small-file,
sparse tree. A controlled worker sweep of the same early-filter binary measured
JSON at 4.69 ms with 32 workers versus 2.44 ms with eight, whereas Linux and
Test262 benefited from larger pools. Linux therefore begins with up to eight
workers and grows toward the online-CPU limit (maximum 32) when selected file
batches accumulate. Explicit `THECOUNT_READERS` preserves fixed sizing, and
plain-file roots start no unnecessary threads. There are no language-specific
or benchmark-path-specific scheduling rules.

Coverage remains broader than this Mezura installation. Full default-language
audits of LLVM, the web corpus, Perfbot, and personal projects preserve all
previous paths and every code/comment/blank bucket across 159,310 previously
counted files. The missing `.f08` registration adds one Fortran file, for 159,311
files now. These full audits contain 4,236 JSON files, 1,388 Markdown files, and
41,539 LLVM IR files. Invalid UTF-8 source bytes remain supported.

Timing comparisons still restrict both counters to identical paths and physical
line counts. Mezura's language aliases include related extensions, so its extras
are explicitly excluded. Asymmetric fixtures are excluded symmetrically only
by the benchmark harness: 31 unique paths (14 NUL-containing fixtures skipped
by TheCount's documented binary heuristic and 17 invalid-UTF-8 fixtures omitted
by Mezura). The `.f08` exclusion is no longer needed. Parser classification
policy differences remain separate from workload parity; no count rules were
changed to copy Mezura's output.

Validation: `coil verify` and all **101 tests** pass. The Linux CLI suite
(`python3 tests/check_walk.py <binary>`) checks sparse and batched directories,
ignores, hidden paths, exclusions, custom registrations, uppercase `.F08`,
invalid UTF-8, direct files, empty trees, and repeated agreement between adaptive
and 1/8/32-worker runs. The same suite passes under ThreadSanitizer on macOS:

    coil build --release --sanitize=thread -o /tmp/thecount-walk-tsan
    python3 tests/check_walk.py /tmp/thecount-walk-tsan

That check found two shared-library initialization races. TheCount now snapshots
its string-map capability table before worker startup and resolves the lazy errno
accessor on the coordinator before concurrent I/O. Both underlying Coil issues
are recorded in its `coil-bugs` pad; no sanitizer suppression was used.

[Final timings, audits, hashes, commands, and samples](benchmark/results/linux-workload-updated-20261001.json),
[full coverage audit](benchmark/results/linux-workload-full-coverage-20261001.json),
[early filtering experiment](benchmark/results/linux-workload-early-filter-20261001.json),
[worker sweep](benchmark/results/linux-workload-worker-sweep-20261001.json), and
[first adaptive sweep](benchmark/results/linux-workload-adaptive-20261001.json)
retain the evidence. The experimental sweeps predate the initialization fixes.
Reproduce with `benchmark/measure_workloads.py` and
`benchmark/workloads_linux_20261001.json`; use `benchmark/audit_coverage.py`
for coverage without selection filters or fairness exclusions. Raw final per-file
JSON remains on the host under
`/tmp/thecount-20260930-bench/updated-safe-survey-audits/` and
`/tmp/thecount-20260930-bench/updated-safe-coverage-audits/`.

The tested Linux binary is
`/tmp/thecount-20260930-bench/thecount-updated`. The original survey and JSON
regression confirmation remain under `benchmark/results/` as historical evidence.

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
