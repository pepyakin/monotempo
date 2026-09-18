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
CARGO_BAZEL_REPIN=1 bazel mod deps           # regenerate Cargo.Bazel.lock if deps or features changed
```

`python3 bazel/generate.py --check` must pass before committing. Per-project
Bazel settings that cannot be derived from Cargo (disabled features, build
script inputs, test tags) live in `<project>/bazel/project.toml`.

## Cross-project dependencies

Each project is its own Cargo workspace for `cargo`, but Bazel resolves all of
them as one workspace (`bazel/cargo/`, generated, with its own committed
`Cargo.lock`) and one crate_universe repository, `@crates`. reth depends on the
in-tree alloy, reth-core, alloy-evm and revm-inspectors through
`[patch.crates-io]` in `reth/Cargo.toml` (and those three patch alloy the same
way); the generator turns that into `//alloy/...` etc. dependencies in reth's
`BUILD.bazel` files, and into `crate.annotation(deps = ...)` entries in the
generated `bazel/cargo/member_deps.MODULE.bazel` for any external crate that
depends on in-tree crates (crate_universe drops those edges itself; currently
there are none). Keep
the projects' lockfiles aligned on shared external crates: the generator fails
when `bazel/cargo/Cargo.lock` would pin a version no project pins, and prints
the `cargo update --precise` command that fixes it.

## Project-specific guidance

Each project keeps its upstream guidance: `reth/AGENTS.md` applies to files
under `reth/`, `alloy/CONTRIBUTING.md` to `alloy/`. Their Cargo commands still
work when run from the project directory, but Bazel is the build of record here.

## Git

Projects are imported with full upstream history (subtree merges), so keep
project-local changes in commits that touch only that project's directory
where practical; it keeps `git subtree pull` from upstream tractable.
