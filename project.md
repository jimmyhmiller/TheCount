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

`coil verify` runs fmt + lint + check + build + tests (29 tests across 4 suites).

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
the kernel time. The architecture (informed by scc/ripgrep/dumac writeups):

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
