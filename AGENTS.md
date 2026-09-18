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
bazel test //alloy/...                      # all of alloy
bazel test //...                            # everything; only changed crates rebuild/rerun
```

`bazel build //...` needs ~16 cores, 32 GiB RAM and ~40 GiB disk; prefer
building and testing the crates you changed and their dependants
(`bazel query 'rdeps(//..., //reth/crates/foo:reth_foo)'`).

## Changing a `Cargo.toml` or `Cargo.lock`

`BUILD.bazel` files are generated; never edit them by hand. After changing a
manifest in any project:

```bash
python3 bazel/generate.py                   # regenerate BUILD.bazel files (all projects)
CARGO_BAZEL_REPIN=1 bazel mod deps           # regenerate <project>/Cargo.Bazel.lock if deps changed
```

`python3 bazel/generate.py --check` must pass before committing. Per-project
Bazel settings that cannot be derived from Cargo (disabled features, build
script inputs, test tags) live in `<project>/bazel/project.toml`.

## Cross-project dependencies

Each project is its own Cargo workspace with its own crate_universe
(`@reth_crates`, `@alloy_crates`); the lockfiles are kept aligned so shared
third-party crates resolve to the same versions. reth still consumes alloy from
crates.io (the version its `Cargo.lock` pins, currently equal to `alloy/`);
wiring reth to the in-tree alloy is the next step.

## Project-specific guidance

Each project keeps its upstream guidance: `reth/AGENTS.md` applies to files
under `reth/`, `alloy/CONTRIBUTING.md` to `alloy/`. Their Cargo commands still
work when run from the project directory, but Bazel is the build of record here.

## Git

Projects are imported with full upstream history (subtree merges), so keep
project-local changes in commits that touch only that project's directory
where practical; it keeps `git subtree pull` from upstream tractable.
