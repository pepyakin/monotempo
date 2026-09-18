# Building the Cargo workspaces with Bazel

The monorepo root is the Bazel workspace; every project (`reth/`, `alloy/`,
...) is a Cargo workspace inside it. Run `bazel` from the root, and every label
starts with the project directory (`//reth/...`, `//alloy/...`). This document
covers how the Cargo workspaces are turned into Bazel targets and the
day-to-day workflow; the root README lists the projects.

Bazel builds hermetically with [rules_rust]: a pinned Rust toolchain, a pinned
LLVM/clang C toolchain, sandboxed actions without network access, and a
per-crate dependency graph so that a change to one file only rebuilds (and
retests) the crates that depend on it.

Cargo stays the source of truth for crate metadata. `Cargo.toml` files are not
duplicated by hand; the Bazel `BUILD.bazel` files are generated from them.

[rules_rust]: https://github.com/bazelbuild/rules_rust

## Quick start

Install [bazelisk] as `bazel`; it reads `.bazelversion`. Nothing else is needed:
rustc, cargo, clang and libclang are downloaded and pinned by Bazel.

```bash
bazel build //reth                               # debug build of the reth node binary
bazel build --config=release //reth              # opt build (thin LTO)
bazel build //reth/crates/storage/db:reth_db     # one crate
bazel test //alloy/crates/consensus/...          # tests of one crate
bazel test //alloy/...                           # everything in alloy (cached per crate)
bazel test //...                                 # everything
```

Outputs land in `bazel-bin/`, e.g. `bazel-bin/reth/bin/reth/reth`.

[bazelisk]: https://github.com/bazelbuild/bazelisk

## Layout

Shared, at the root:

| Path | Purpose |
| --- | --- |
| `MODULE.bazel` | Root module: rules_rust, Rust toolchain version, LLVM toolchain; `include()`s each project's part. |
| `.bazelrc` | Hermeticity flags, `--config=release`, `--config=ci`, `--config=dev`. |
| `.bazelignore` | Directories Bazel must not treat as packages (Cargo `target/` dirs, docs, shadow-workspace symlinks; the last block is generated). |
| `bazel/generate.py` | The generator: renders every project's `BUILD.bazel` files from its `Cargo.toml` files. |
| `bazel/rust.bzl` | `crate_library`, `crate_proc_macro`, `crate_binary`, `crate_unit_test`, `crate_integration_test`, `crate_build_script` wrappers around rules_rust, used by the generated files. |
| `bazel/BUILD.bazel` | clang builtin headers for bindgen; exports the test runner. |
| `bazel/process_per_test.bzl`, `bazel/process_per_test.sh` | Test rule that runs each test of a `rust_test` in its own process (used for node-launching tests). |
| `bazel/test_fuzz/` | Stub cargo package that `#[test_fuzz]`-instrumented tests point `cargo metadata` at. |

Per project (`<p>/`):

| Path | Purpose |
| --- | --- |
| `<p>.MODULE.bazel` | The project's part of the module: its crate_universe (`@<p>_crates`) and crate annotations (patches, build-script inputs of external crates). |
| `Cargo.Bazel.lock` | crate_universe's rendered view of the project's `Cargo.lock` (which crates, features, and build scripts each external crate needs). |
| `bazel/project.toml` | Hand-written: what Bazel needs to know about the project beyond its manifests (see below). |
| `bazel/workspace.bzl` | Generated: the `WORKSPACE` struct (version, edition, manifest and lint labels) and `[workspace.lints]`. |
| `bazel/BUILD.bazel` | Generated: the project's `rust_lint_config`. |
| `bazel/cargo/` | The Bazel shadow of the Cargo workspace (see below). |
| `bazel/patches/` | Patches applied to external crates by crate_universe annotations (optional). |
| `<crate>/BUILD.bazel` | Generated per crate from its `Cargo.toml`. |

Every workspace crate `foo-bar` becomes `//<p>/path/to/crate:foo_bar` (its lib),
`//<p>/path/to/crate:foo_bar_test` (its `#[cfg(test)]` tests) and
`//<p>/path/to/crate:foo_bar_<name>_test` per `tests/<name>.rs`. Binaries keep
their Cargo name (`//reth/bin/reth:reth`, aliased as `//reth`). External crates
are `@<p>_crates//:<name>`.

## When you change a `Cargo.toml` or `Cargo.lock`

Run the generator, then repin crate_universe if external dependencies or
features changed:

```bash
python3 bazel/generate.py           # rewrites BUILD.bazel files of every project (~2 s)
CARGO_BAZEL_REPIN=1 bazel mod deps   # rewrites <p>/Cargo.Bazel.lock (~1 min per project)
```

CI runs `bazel/generate.py --check` and builds with `--config=ci`
(`--lockfile_mode=error`), so a stale generated file or lockfile fails the
build with a message naming the file.

The generator needs `cargo` on `PATH` (any recent toolchain; it only runs
`cargo metadata --locked`).

## `bazel/project.toml`

Everything the generator cannot derive from Cargo, per project. Labels are
written in full. Keys:

* `crates_repo`: the crate_universe repository, `"@<p>_crates"`.
* `disabled_features.<crate>.<feature> = "<reason>"`: default features not
  built by Bazel (see the shadow workspace below).
* `build_scripts.<crate>`: `data_globs` (relative to the crate), `data`
  (labels) and `env` for the crate's `build.rs`. Build scripts run in the
  sandbox and see only their declared inputs.
* `compile_data.<crate>.<target> = [labels]`: files a target `include_*!`s
  that are not globbed automatically (outside `src/`, `res/`, `assets/` and
  the test-data directories); `<target>` is an integration test name or
  `crate` for the lib, binaries and unit tests. Files in another package need
  a full label and an `exported_files.<crate>` entry there.
* `process_per_test = [crates]`: tests of these crates and of their
  dependants run one process per test.
* `test_tags.<crate> = [tags]`: tags for the crate's test targets (`manual`
  keeps a test out of `bazel test //...`); or `test_tags.<crate>.<target>`
  for one target, `<target>` being an integration test name or `crate` for
  the unit tests.

See `reth/bazel/project.toml` for a commented example.

## The shadow workspace (`<p>/bazel/cargo/`)

crate_universe and the generator read each Cargo workspace through a
*shadow*: a directory that mirrors the project layout with symlinks
(`crates`, `examples`, ...) and replaces a few manifests by generated copies:

* The root `Cargo.toml` is a copy that carries a digest of every member
  manifest, so that editing any crate's `Cargo.toml` invalidates
  `Cargo.Bazel.lock` (crate_universe only watches the manifests it is given).
  `path` dependencies on other projects (`../alloy/crates/x`) are rewritten to
  go through a symlink inside the shadow (`alloy -> ../../../alloy`), because
  crate_universe copies the shadow root's children into a temporary workspace
  and cannot follow `../` out of it.
* Manifests of crates in `disabled_features` are copies without those
  features in `default`. Cargo resolves features for the whole workspace at
  once, using every member's default features, and crate_universe has no way
  to subtract a feature from that resolution. reth drops two of `bin/reth`'s
  defaults this way: `jit` (revmc -> llvm-sys, needs a system LLVM) and `gmp`
  (gmp-mpfr-sys, builds GMP with autotools); reth falls back to the pure-Rust
  `modexp` implementation.

The symlinks are listed in the generated block of `.bazelignore` so Bazel
does not discover each package a second time through them.

## Cross-project dependencies

reth uses alloy from the tree: `[patch.crates-io]` in `reth/Cargo.toml` points
every `alloy-*` crate at `../alloy/crates/*`, so `cargo` in `reth/` and Bazel
build the same code. On the Bazel side crate_universe renders the patched
crates into `@reth_crates` from the source in `alloy/` (through the shadow
symlink), so an edit to alloy rebuilds the reth crates that use it.

Those rendered crates are compiled separately from the `//alloy/...` targets
(the two universes do not share compilation), which is the same duplication as
between any two crate_universe repositories; it costs build time, not
correctness. Sharing them is possible later with crate_universe's
`override_targets` annotation.

## Hermeticity

* Rust: `rust.toolchain(versions = ["1.95.0"])`, matching the workspaces'
  `rust-version`. No rustup involved.
* C/C++: `toolchains_llvm` downloads clang 20.1.7. It is used for `cc` build
  scripts (libmdbx, jemalloc, secp256k1, aws-lc, ...) and provides `libclang`
  for bindgen in `reth-mdbx-sys` and `librocksdb-sys`. The toolchain targets
  the host glibc (no sysroot), so binaries are as portable as the machine
  that built them.
* Actions run in the sandbox with `--incompatible_strict_action_env` and no
  network. Build scripts see only their declared inputs (`build_scripts` in
  `project.toml`; e.g. libmdbx sources and libclang for `reth-mdbx-sys`, and
  `VERGEN_IDEMPOTENT=1` for `reth-node-core` so the binary is not stamped
  with the current git commit, which would defeat caching).
* Exceptions: `tikv-jemalloc-sys` runs jemalloc's `configure`, which needs a
  shell and the usual POSIX tools from the host (`build_script_use_default_shell_env`
  annotation in `reth.MODULE.bazel`).

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
  `//reth/crates/net/`) to keep the cache small, or add a second line with
  `//alloy/crates/@` to cover alloy.
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
  `cargo build --workspace` in the project, minus `disabled_features`. Per-crate
  feature combinations are not modelled; a crate is built once with its unified
  features.
* Platform-specific dependencies (`[target.'cfg(...)'.dependencies]`) become
  `select()`s over the configured platforms (Linux and macOS); wasm-only
  dependencies are dropped and `not(wasm)` ones are unconditional.
* `[lints] workspace = true` crates get the workspace lints through
  `//<p>/bazel:lints`; crates without it (e.g. `reth-mdbx-sys`, reth's examples)
  get rustc's defaults, as with cargo.
* Tests default to `size = "large"` (15 min timeout) because many reth tests
  spin up nodes or databases.
* Each test process runs with `RUST_TEST_THREADS=8`. libtest would otherwise
  use one thread per core, and every MDBX environment reserves 8 TiB of
  address space, which exhausts the 47-bit x86-64 address space around sixteen
  concurrent environments (`Cannot allocate memory`). `cargo nextest` avoids
  this by running one process per test; Bazel runs test targets in parallel
  instead, so throughput is similar.
* Tests that launch nodes (`process_per_test` crates and everything with a
  dependency on them) run one process per test, like `cargo nextest`: the
  `rust_test` is `<name>_bin` (tagged `manual`) and `<name>` is a
  `process_per_test` wrapper around it. A launched reth node retains ~2 GiB
  until the process exits, so a single-process run of e.g.
  `reth_node_ethereum_e2e_test` (69 tests) grew to ~28 GiB; per process it
  stays under 3 GiB. The wrapper runs `TEST_PROCESSES` (default 4) tests
  at a time and forwards `--test_arg`s (filters, `--nocapture`) to the binary.
  Regular tests still run as one process per target.
* Tests see `CARGO_MANIFEST_DIR` as the crate's path relative to the
  monorepo root (`reth/crates/net/eth-wire`) rather than an absolute path.
  rustc and the test binary both run from a directory where that relative
  path resolves, whereas the absolute sandbox path rustc sees no longer exists
  when the test runs. Runtime test data must live in `src/`, `tests/`,
  `testdata/`, `test-data/`, or `test_data/` (or be added to `data`).
* `#[test_fuzz]` tests record a corpus by running `cargo metadata`. Under
  Bazel they use the toolchain's cargo and the stub package in
  `bazel/test_fuzz/`, so the corpus lands in the sandbox and is discarded.
  Actual fuzzing (`cargo test-fuzz`) is still a cargo workflow.
* Tests that need a running node or the network are tagged `manual` per
  project (`test_tags` in `project.toml`); reth's `ef-tests` and
  `reth-era-utils`, and in alloy the tests that spawn `anvil`/`geth` or reach
  public RPC endpoints.
* Windows is not supported (the shadow workspace uses symlinks; no Windows
  platform triple is configured).
