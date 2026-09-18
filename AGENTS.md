# monotempo: guide for AI agents

This is a Bazel monorepo for Tempo. Each top-level directory is a project that
is also a Cargo workspace; the Bazel workspace is this root. See `README.md`
for the project list and layout.

## Build and test

Run `bazel` from this directory. Labels are workspace-relative, so reth's
crates are `//reth/crates/...`.

```bash
bazel build //reth                          # reth node binary
bazel test //reth/crates/net/eth-wire/...   # tests of one crate
bazel test //...                            # everything; only changed crates rebuild/rerun
```

`bazel build //...` needs ~16 cores, 32 GiB RAM and ~40 GiB disk; prefer
building and testing the crates you changed and their dependants
(`bazel query 'rdeps(//..., //reth/crates/foo:reth_foo)'`).

## Changing a `Cargo.toml` or `Cargo.lock`

`BUILD.bazel` files are generated; never edit them by hand. After changing a
manifest in reth:

```bash
python3 reth/scripts/bazel/generate.py      # regenerate BUILD.bazel files
CARGO_BAZEL_REPIN=1 bazel mod deps           # regenerate reth/Cargo.Bazel.lock if deps changed
```

`python3 reth/scripts/bazel/generate.py --check` must pass before committing.

## Project-specific guidance

Each project keeps its upstream guidance: `reth/AGENTS.md` applies to files
under `reth/`. Its Cargo commands still work when run from `reth/`, but Bazel
is the build of record here.

## Git

Projects are imported with full upstream history (subtree merges), so keep
project-local changes in commits that touch only that project's directory
where practical; it keeps `git subtree pull` from upstream tractable.
