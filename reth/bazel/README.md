# Building reth with Bazel

reth is one project of the monotempo monorepo, whose root is the Bazel
workspace: run `bazel` from the repository root, and every reth label starts
with `//reth/`. The root `MODULE.bazel`, `.bazelrc` and `.bazelignore` are
shared with the other projects (see the root README); this document covers
reth's part.

Bazel builds reth hermetically with [rules_rust]: a pinned Rust toolchain, a
pinned LLVM/clang C toolchain, sandboxed actions without network access, and a
per-crate dependency graph so that a change to one file only rebuilds (and
retests) the crates that depend on it.

Cargo stays the source of truth for crate metadata. `Cargo.toml` files are not
duplicated by hand; the Bazel `BUILD.bazel` files are generated from them.

[rules_rust]: https://github.com/bazelbuild/rules_rust

## Quick start

Install [bazelisk] as `bazel`; it reads `.bazelversion`. Nothing else is needed:
rustc, cargo, clang and libclang are downloaded and pinned by Bazel.

```bash
bazel build //reth                               # debug build of the node binary
bazel build --config=release //reth              # opt build (thin LTO)
bazel build //reth/crates/storage/db:reth_db     # one crate
bazel test //reth/crates/tracing/...             # tests of one crate
bazel test //reth/...                            # everything in reth (cached per crate)
```

Outputs land in `bazel-bin/`, e.g. `bazel-bin/reth/bin/reth/reth`.

[bazelisk]: https://github.com/bazelbuild/bazelisk

## Layout

| Path | Purpose |
| --- | --- |
| `/MODULE.bazel` | Root module (shared): rules_rust, Rust toolchain version, LLVM toolchain; `include()`s the file below. |
| `reth.MODULE.bazel` | reth's part of the module: crate_universe (`@reth_crates`) and its crate annotations. |
| `/.bazelrc` | Hermeticity flags, `--config=release`, `--config=ci`, `--config=dev`. |
| `Cargo.Bazel.lock` | crate_universe's rendered view of `Cargo.lock` (which crates, features, and build scripts each external crate needs). |
| `bazel/rust.bzl` | `reth_library`, `reth_binary`, `reth_unit_test`, `reth_integration_test`, `reth_build_script` wrappers around rules_rust. |
| `bazel/workspace.bzl` | Generated: workspace version, edition, `[workspace.lints]`. |
| `bazel/BUILD.bazel` | Lint config shared by all crates, clang builtin headers for bindgen. |
| `bazel/cargo/` | The Bazel shadow of the Cargo workspace (see below). |
| `bazel/process_per_test.bzl`, `bazel/process_per_test.sh` | Test rule that runs each test of a `rust_test` in its own process (used for node-launching tests). |
| `bazel/test_fuzz/` | Stub cargo package that `#[test_fuzz]`-instrumented tests point `cargo metadata` at. |
| `bazel/patches/` | Patches applied to external crates by crate_universe annotations. |
| `<crate>/BUILD.bazel` | Generated per crate from its `Cargo.toml`. |
| `scripts/bazel/generate.py` | The generator (derives the `//reth/` label prefix from its location under the workspace root). |

Every workspace crate `foo-bar` becomes `//reth/path/to/crate:foo_bar` (its lib),
`//reth/path/to/crate:foo_bar_test` (its `#[cfg(test)]` tests) and
`//reth/path/to/crate:foo_bar_<name>_test` per `tests/<name>.rs`. Binaries keep
their Cargo name (`//reth/bin/reth:reth`, aliased as `//reth`). External crates
are `@reth_crates//:<name>`.

## When you change a `Cargo.toml` or `Cargo.lock`

Run the generator, then repin crate_universe if external dependencies or
features changed:

```bash
python3 reth/scripts/bazel/generate.py    # rewrites BUILD.bazel files (~2 s)
CARGO_BAZEL_REPIN=1 bazel mod deps         # rewrites reth/Cargo.Bazel.lock (~2 min)
```

CI runs `reth/scripts/bazel/generate.py --check` and builds with
`--config=ci` (`--lockfile_mode=error`), so a stale generated file or lockfile
fails the build with a message naming the file.

The generator needs `cargo` on `PATH` (any recent toolchain; it only runs
`cargo metadata --locked`).

## The shadow workspace (`bazel/cargo/`)

Cargo resolves features for the whole workspace at once, using every member's
`default` features, and crate_universe has no way to subtract a feature from
that resolution. Two of `bin/reth`'s default features cannot be built
hermetically yet, so they are dropped from the Bazel build:

* `jit` (revmc -> llvm-sys): needs `llvm-config` and static LLVM 22 libraries
  from a system install.
* `gmp` (revm-precompile/gmp -> gmp-mpfr-sys): builds GMP with autotools at
  build time. reth falls back to the pure-Rust `modexp` implementation.

To do that without touching the real manifests, `bazel/cargo/` mirrors the
repository layout with symlinks (`crates`, `examples`, `testing`, `bin/reth-bb`,
`bin/reth/src`, ...) and replaces exactly one file: `bazel/cargo/bin/reth/Cargo.toml`,
a generated copy of `bin/reth/Cargo.toml` without `jit`/`gmp` in `default`.
crate_universe and the generator both read the workspace through that root,
so Bazel's view of the dependency graph has a single definition.

The list of dropped features lives in `BAZEL_DISABLED_FEATURES` in
`scripts/bazel/generate.py`; each entry documents why it is disabled. The
shadow manifest also carries a digest of every member manifest so that editing
any crate's `Cargo.toml` invalidates `Cargo.Bazel.lock` (crate_universe only
watches the manifests it is given).

The symlinks are listed in `.bazelignore` so Bazel does not discover each
package a second time through them.

## Hermeticity

* Rust: `rust.toolchain(versions = ["1.95.0"])`, matching the workspace
  `rust-version`. No rustup involved.
* C/C++: `toolchains_llvm` downloads clang 20.1.7. It is used for `cc` build
  scripts (libmdbx, jemalloc, secp256k1, ...) and provides `libclang` for
  bindgen in `reth-mdbx-sys`. The toolchain targets the host glibc (no
  sysroot), so binaries are as portable as the machine that built them.
* Actions run in the sandbox with `--incompatible_strict_action_env` and no
  network. Build scripts see only their declared inputs; those inputs are
  spelled out per crate in `BUILD_SCRIPT_DATA`/`BUILD_SCRIPT_ENV` in the
  generator (e.g. libmdbx sources and libclang for `reth-mdbx-sys`, and
  `VERGEN_IDEMPOTENT=1` for `reth-node-core` so the binary is not stamped with
  the current git commit, which would defeat caching).
* Exceptions: `tikv-jemalloc-sys` runs jemalloc's `configure`, which needs a
  shell and the usual POSIX tools from the host (`build_script_use_default_shell_env`
  annotation in `MODULE.bazel`).

## Incrementality

Each crate is one Bazel target with explicit deps, so Bazel knows exactly which
targets a source change reaches. With `pipelined_compilation` dependants start
compiling as soon as a crate's `.rmeta` exists. `bazel test //...` re-runs only
tests whose transitive inputs changed; everything else is a cache hit. Add a
`--disk_cache=` (or a remote cache) in an uncommitted `user.bazelrc` to share
results across checkouts.

## Incremental rustc (`--config=dev`, Linux only)

By default every rustc action is a full compile of its crate, so editing one
line of a large crate costs the whole crate plus its test binary (~20 s for
`reth-eth-wire` + `reth-network` on 16 cores, vs ~7 s with cargo). rustc's
`-C incremental` cuts that roughly in half, but it needs two things Bazel does
not give it by default: a cache directory outside the sandbox and a stable
working directory (rules_rust puts the cwd into `--remap-path-prefix` and
`CARGO_MANIFEST_DIR`, and rustc treats a changed cwd as a changed crate; the
default sandbox uses a fresh numbered directory per action). `--config=dev`
in `.bazelrc` switches to Bazel's hermetic Linux sandbox, which pivot_roots
so every action runs in `/execroot/_main`. The cache directory is machine
specific, so create it and add it to the uncommitted `user.bazelrc` (absolute
path, both lines):

```
mkdir -p ~/.cache/reth-bazel-incremental
cat >> user.bazelrc <<EOF
build:dev --sandbox_add_mount_pair=/home/<you>/.cache/reth-bazel-incremental
build:dev --@rules_rust//rust/settings:per_crate_rustc_flag=//reth/crates/@-Cincremental=/home/<you>/.cache/reth-bazel-incremental
EOF
bazel test --config=dev //reth/crates/net/network/...
```

Both lines are needed: without the mount pair rustc silently writes its cache
inside the throwaway sandbox and nothing is reused. Notes:

* Measured on 16 cores, touching `crates/net/eth-wire/src/lib.rs` and
  building `//reth/crates/net/eth-wire/... //reth/crates/net/network/...`: 20 s default,
  9–12 s with `--config=dev` (the rustc process for a touched crate drops from
  ~6 s to ~2 s; the rest is Bazel overhead and test-binary linking).
  `-C codegen-units=256` on its own changes nothing measurable.
* `per_crate_rustc_flag=<label prefix>@<flag>` applies the flag only to
  crates whose label starts with the prefix. `//reth/crates/` covers the libraries
  and their test binaries but not `//reth/bin/...` or `//reth/examples/...`: those leaf
  binaries monomorphise the whole node and each would add ~900 MiB of cache
  for code nobody iterates on. External crates never match, so switching the
  config on or off rebuilds the `//reth/crates/` targets once (a few minutes) but
  keeps every external crate cached. Narrow the prefix further (e.g.
  `//reth/crates/net/`) to keep the cache small.
* The cache is large: ~15–20 GiB for all of `//reth/crates/` including test
  binaries (the biggest e2e test binaries take ~1 GiB each), roughly what
  cargo's `target/debug/incremental` costs. Delete the directory to reset it.
* Not for CI: the hermetic sandbox mounts `/usr`, `/bin`, `/lib`, `/lib64`,
  `/etc` from the host, and incremental artifacts are not reproducible.
* macOS has no equivalent: the Darwin sandbox also uses per-action
  directories, and running rustc unsandboxed (`--strategy=Rustc=local`)
  fails because the rlib action rewrites the `.rmeta` the pipelined
  `RustcMetadata` action already produced.

## Machine resources

`.bazelrc` sets `--@rules_rust//rust/settings:codegen_units=4`, which makes
every rustc action reserve 4 CPUs in Bazel's local scheduler (and passes
`-C codegen-units=4`). Bazel otherwise assumes one CPU and a few hundred MB per
action and runs one rustc per core; linking reth's larger test binaries takes
2-3 GiB each, which exhausted a 16-core/32 GiB machine. Override the value in
`user.bazelrc` for machines with more memory per core.

## Differences from cargo

* Features: the Bazel build uses the same feature set as
  `cargo build --workspace`, minus `jit` and `gmp` (see above). Per-crate feature
  combinations are not modelled; a crate is built once with its unified features.
* `[lints] workspace = true` crates get the workspace lints through
  `//reth/bazel:lints`; crates without it (e.g. `reth-mdbx-sys`, the examples) get
  rustc's defaults, as with cargo.
* `ef-tests` is tagged `manual` (it needs the `ethereum-tests` submodule).
* Tests default to `size = "large"` (15 min timeout) because many reth tests
  spin up nodes or databases.
* Each test process runs with `RUST_TEST_THREADS=8`. libtest would otherwise
  use one thread per core, and every MDBX environment reserves 8 TiB of
  address space, which exhausts the 47-bit x86-64 address space around sixteen
  concurrent environments (`Cannot allocate memory`). `cargo nextest` avoids
  this by running one process per test; Bazel runs test targets in parallel
  instead, so throughput is similar.
* Tests that launch nodes (`reth-e2e-test-utils` and everything with a
  dependency on it) run one process per test, like `cargo nextest`: the
  `rust_test` is `<name>_bin` (tagged `manual`) and `<name>` is a
  `process_per_test` wrapper around it. A launched node retains ~2 GiB until
  the process exits, so a single-process run of e.g.
  `reth_node_ethereum_e2e_test` (69 tests) grew to ~28 GiB; per process it
  stays under 3 GiB. The wrapper runs `RETH_TEST_PROCESSES` (default 4) tests
  at a time and forwards `--test_arg`s (filters, `--nocapture`) to the binary.
  Regular tests still run as one process per target.
* Tests see `CARGO_MANIFEST_DIR` as the crate's path relative to the
  workspace root (`crates/net/eth-wire`) rather than an absolute path. rustc
  and the test binary both run from a directory where that relative path
  resolves, whereas the absolute sandbox path rustc sees no longer exists
  when the test runs. Runtime test data must live in `src/`, `tests/`,
  `testdata/`, `test-data/`, or `test_data/` (or be added to `data`).
* `#[test_fuzz]` tests record a corpus by running `cargo metadata`. Under
  Bazel they use the toolchain's cargo and the stub package in
  `bazel/test_fuzz/`, so the corpus lands in the sandbox and is discarded.
  Actual fuzzing (`cargo test-fuzz`) is still a cargo workflow.
* Windows is not supported (the shadow workspace uses symlinks; no Windows
  platform triple is configured).
