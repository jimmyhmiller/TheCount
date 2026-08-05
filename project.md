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
  directory, not just by language or file.

None of scc's estimation extras (COCOMO, complexity scores) — just counting.

## Usage

    thecount [paths...]          count by language (default: .)
    thecount --files             break up by file
    thecount --dirs --depth 2    break up by folder
    thecount --hidden            include hidden files
    thecount --no-ignore         don't honor .gitignore
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
- `src/output.coil` — the three tables (language / file / directory).
- `src/specialize.coil` — compile-time specialization of the counter (below).
- `src/simd.coil` — 16-byte byte-class bitmasks over `(primitive/llvm-ir …)`.

`coil verify` runs fmt + lint + check + build + tests (72 tests across 5 suites).

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

`src/simd.coil` replaces it with the simdjson approach: load 16 bytes, compare
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
became a per-16-byte bitmask.

End-to-end on ladybird (1.8M lines, 20 runs): 285 ms → 280 ms wall, but **user
CPU 329 ms → 164 ms**. Wall time barely moves because counting is no longer the
bottleneck — ~1.1 s of system time across the reader threads is, and that is
unchanged. Any further work on throughput belongs in the reader, not the
counter.

Not done, and probably where the next counter win is: the scan still restarts
per line, so a 38-byte average C++ line gets two vector chunks and a ~6-byte
scalar tail. simdjson computes masks over the whole buffer once and derives line
boundaries from the newline mask; that would put the tail work at ~0 and let the
escaped-quote and inside-string masks be computed branchlessly with a
carry-less-multiply prefix XOR, replacing `backslashes-before` and the ST-STRING
arm outright. Nested block comments still need a real counter, so those
languages keep a sequential pass over the candidate positions. The architecture (informed by scc/ripgrep/dumac writeups):

- **Split pools.** Syscalls and counting want opposite thread counts on
  macOS — VFS contention grows superlinearly with concurrent open/read
  (measured), while counting wants every core. So a small READER pool
  (default 4 on darwin, 8 on linux; THECOUNT_READERS overrides) walks
  directories and reads files, and a COUNTER pool (THECOUNT_COUNTERS)
  drains a ring of shared buffers into per-thread aggregates, connected by
  condvar-signaled queues with batch draining. Backpressure comes free: at
  most 32 files are in flight.
- **Cheap syscalls.** Three per file (open/read/close — a short read from a
  regular file is EOF, so no fstat needed), opened with openat through the
  enclosing directory's fd so the kernel resolves one path component, not
  the whole path. Directory batches share basenames, not full paths.
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
