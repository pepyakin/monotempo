# monotempo

Monorepo for [Tempo](https://github.com/tempoxyz/tempo) and the projects it is
built on, with one hermetic [Bazel](https://bazel.build) build across all of
them: a pinned Rust toolchain, a pinned LLVM/clang C toolchain, sandboxed
actions without network access, and a per-crate dependency graph so that a
change to one file only rebuilds (and retests) the crates that depend on it.

## Projects

| Directory | Project | Upstream | Status |
| --- | --- | --- | --- |
| `reth/` | Ethereum execution client Tempo is built on | [paradigmxyz/reth](https://github.com/paradigmxyz/reth) | imported, builds and tests with Bazel |
| `tempo/` | The Tempo node itself | [tempoxyz/tempo](https://github.com/tempoxyz/tempo) | imported with full history; Bazel targets generated from Cargo |
| `alloy/` | Ethereum types, RPC and transports used by reth and tempo | [alloy-rs/alloy](https://github.com/alloy-rs/alloy) (Tempo's fork, with `signer-tempo`) | imported; reth builds against it (`[patch.crates-io]` in `reth/Cargo.toml`, `//alloy/...` targets under Bazel) |
| `reth-core/` | `reth-primitives-traits`, `reth-codecs`, `reth-rpc-traits`, `reth-zstd-compressors`: reth's foundation crates, published separately | [paradigmxyz/reth-core](https://github.com/paradigmxyz/reth-core) | imported; reth builds against it |
| `alloy-evm/` | EVM abstraction layer between alloy and revm | [alloy-rs/alloy-evm](https://github.com/alloy-rs/alloy-evm) | imported; reth builds against it |
| `revm-inspectors/` | EVM tracing inspectors for the debug/trace RPC namespaces | [paradigmxyz/revm-inspectors](https://github.com/paradigmxyz/revm-inspectors) | imported; reth builds against it |
| `revm/` or `evm2/` | The EVM used by reth | [bluealloy/revm](https://github.com/bluealloy/revm) (what reth and tempo use today) or [alloy-rs/evm2](https://github.com/alloy-rs/evm2) (its successor, in development) | not yet imported; which one is still open |

Until a project is imported, reth consumes it as an external crate from
crates.io through crate_universe, exactly as its `Cargo.lock` says. Importing a
project means moving it under its directory here and pointing the dependants
at it with `[patch.crates-io]` in their `Cargo.toml`, as reth does for alloy;
the Bazel build follows the patch. `reth-core`, `alloy-evm` and
`revm-inspectors` are in-tree because they are the crates that sit between
reth and alloy: every external crate that depended on alloy came from one of
them, so with them imported no external crate reaches into the tree.

Tempo resolves external crates separately through `@tempo_crates`, exactly as
its `Cargo.lock` says. It still consumes its
pinned upstream reth Git revision, which differs from the imported `reth/`.
Importing sources and generating targets does not automatically replace those
dependencies: sharing cross-project targets also requires aligning revisions
and dependency resolution.

## Quick start

Install [bazelisk] as `bazel`; it reads `.bazelversion`. Nothing else is
needed: rustc, cargo, clang and libclang are downloaded and pinned by Bazel.

```bash
bazel build //reth                               # debug build of the reth node binary
bazel build //tempo                              # debug build of the Tempo node binary
bazel build --config=release //reth              # opt build (thin LTO)
bazel build //reth/crates/storage/db:reth_db     # one crate
bazel test //reth/crates/tracing/...             # tests of one crate
bazel test //tempo/crates/hardfork/...           # tests of one Tempo crate
bazel test //...                                 # everything (cached per crate)
```

Outputs land in `bazel-bin/`, e.g. `bazel-bin/reth/bin/reth/reth`.

[bazelisk]: https://github.com/bazelbuild/bazelisk

## Layout

Bazel has a single module rooted here; the projects are directories of it, not
separate Bazel modules, so a target in one project can depend on a target in
another with a plain label (`//reth/crates/primitives:reth_primitives`).

| Path | Purpose |
| --- | --- |
| `MODULE.bazel` | The module: rules_rust, the Rust and LLVM toolchains, `@crates` shared by the version-aligned projects, and Tempo's separate `@tempo_crates`. |
| `Cargo.Bazel.lock` | crate_universe's pinned rendering of the external crates. |
| `.bazelrc` | Hermeticity flags, `--config=release`, `--config=ci`, `--config=dev`. |
| `.bazelignore` | Directories Bazel must not treat as packages (Cargo `target/` dirs, docs, the Bazel Cargo workspace). |
| `bazel/` | Shared across projects: the `crate_*` macros wrapping rules_rust, the `BUILD.bazel` generator, the test runners. |
| `bazel/cargo/` | Generated: one Cargo workspace (and `Cargo.lock`) over the version-aligned projects' crates, which crate_universe resolves. |
| `<project>/<project>.MODULE.bazel` | Optional: the project's crate_universe annotations (patches, build-script inputs of external crates). |
| `<project>/bazel/project.toml` | What the generator cannot read from Cargo: disabled features, build-script inputs, extra `compile_data`, test tags. |
| `<project>/bazel/` | Generated per project: lint config and `workspace.bzl`. |

Each project remains a Cargo workspace (`<project>/Cargo.toml`, `Cargo.lock`)
that `cargo` works in as before, and Cargo stays the source of truth for crate
metadata: `BUILD.bazel` files are generated by `python3 bazel/generate.py`
from the version-aligned projects at once, not written by hand. Bazel resolves
those projects as one Cargo workspace (`bazel/cargo/`, also generated) so that they
can depend on each other in-tree and share every external crate. See [`bazel/README.md`](bazel/README.md) for how that works and for the day-to-day workflow (changing a `Cargo.toml`,
repinning, incremental dev builds).
See [`tempo/bazel/README.md`](tempo/bazel/README.md) for Tempo's generation and
repinning commands and the current cross-project dependency boundary.

## Adding a project

1. Import the source under `<project>/` (with history: `git subtree add
   --prefix=<project> <url> <ref>`).
2. Align its `Cargo.lock` with the other projects' (shared external crates
   at the same versions; the generator reports the mismatches it cannot
   build). If another project should use it in-tree, add
   `[patch.crates-io]` entries pointing at `../<project>/...` there.
3. Add `<project>/bazel/project.toml` (may be empty apart from comments) and,
   unless the project is a single crate, a `<project>/BUILD.bazel` that
   `exports_files` its root `Cargo.toml` and `Cargo.lock` (copy `alloy/`'s).
   Run `python3 bazel/generate.py` to generate the `BUILD.bazel` files and
   extend the Bazel Cargo workspace, then `CARGO_BAZEL_REPIN=1 bazel mod deps`.
4. Add the project's Cargo `target/` directory to `.bazelignore` (the
   generator maintains the `bazel/cargo/` entries itself). If external crates
   of the project need annotations, add `<project>/<project>.MODULE.bazel`
   and `include()` it from `MODULE.bazel`.

## Updating an imported project from upstream

Projects are imported with full history, so upstream changes merge in with
`git subtree pull --prefix=<project> <url> <ref>` (or the equivalent
`git fetch` + `git merge -X subtree=<project>`), and `git blame` works through
the import.
