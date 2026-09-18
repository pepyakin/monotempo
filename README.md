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
| `tempo/` | The Tempo node itself | [tempoxyz/tempo](https://github.com/tempoxyz/tempo) | not yet imported |
| `alloy/` | Ethereum types, RPC and transports used by reth and tempo | [alloy-rs/alloy](https://github.com/alloy-rs/alloy) | not yet imported |
| `revm/` or `evm2/` | The EVM used by reth | [bluealloy/revm](https://github.com/bluealloy/revm) (what reth and tempo use today) or [alloy-rs/evm2](https://github.com/alloy-rs/evm2) (its successor, in development) | not yet imported; which one is still open |

Until a project is imported, reth consumes it as an external crate from
crates.io through crate_universe, exactly as its `Cargo.lock` says. Importing a
project means moving it under its directory here and having reth (and tempo)
depend on the Bazel targets instead of the crates.io versions.

## Quick start

Install [bazelisk] as `bazel`; it reads `.bazelversion`. Nothing else is
needed: rustc, cargo, clang and libclang are downloaded and pinned by Bazel.

```bash
bazel build //reth                               # debug build of the reth node binary
bazel build --config=release //reth              # opt build (thin LTO)
bazel build //reth/crates/storage/db:reth_db     # one crate
bazel test //reth/crates/tracing/...             # tests of one crate
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
| `MODULE.bazel` | The module: rules_rust, the Rust and LLVM toolchains shared by every project, and one `include()` per project. |
| `.bazelrc` | Hermeticity flags, `--config=release`, `--config=ci`, `--config=dev`. |
| `.bazelignore` | Directories Bazel must not treat as packages (Cargo `target/` dirs, docs, shadow-workspace symlinks). |
| `<project>/<project>.MODULE.bazel` | The project's part of the module: its crate_universe (`@<project>_crates`) and crate annotations. |
| `<project>/bazel/` | The project's Bazel macros, lint config, shadow Cargo workspace and patches. |
| `<project>/scripts/bazel/generate.py` | Generates the project's `BUILD.bazel` files from its `Cargo.toml` files. |

Each project remains a Cargo workspace (`<project>/Cargo.toml`, `Cargo.lock`),
and Cargo stays the source of truth for crate metadata: `BUILD.bazel` files
are generated, not written by hand. See [`reth/bazel/README.md`](reth/bazel/README.md)
for how that works and for the day-to-day workflow (changing a `Cargo.toml`,
repinning, incremental dev builds).

## Adding a project

1. Import the source under `<project>/` (with history: `git subtree add
   --prefix=<project> <url> <ref>`).
2. Add `<project>/<project>.MODULE.bazel` with a
   `crate.from_cargo(name = "<project>_crates", ...)` for its `Cargo.lock`,
   and `include("//<project>:<project>.MODULE.bazel")` in `MODULE.bazel`.
3. Generate `BUILD.bazel` files (adapt `reth/scripts/bazel/generate.py`, or
   share it once a second project needs it), then
   `CARGO_BAZEL_REPIN=1 bazel mod deps`.
4. Add the project's Cargo `target/` directory and shadow symlinks to
   `.bazelignore`.

## Updating an imported project from upstream

Projects are imported with full history, so upstream changes merge in with
`git subtree pull --prefix=<project> <url> <ref>` (or the equivalent
`git fetch` + `git merge -X subtree=<project>`), and `git blame` works through
the import.
