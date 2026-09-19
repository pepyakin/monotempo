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
| `MODULE.bazel` | Root module: rules_rust, Rust toolchain version, LLVM toolchain, the crate_universe repository `@crates`; `include()`s each project's part. |
| `Cargo.Bazel.lock` | crate_universe's rendered view of `bazel/cargo/Cargo.lock` (which crates, features and build scripts each external crate needs). |
| `.bazelrc` | Hermeticity flags, `--config=release`, `--config=ci`, `--config=dev`. |
| `.bazelignore` | Directories Bazel must not treat as packages (Cargo `target/` dirs, docs, the Bazel Cargo workspace; the last block is generated). |
| `bazel/generate.py` | The generator: derives the Bazel Cargo workspace from the projects' manifests and renders every project's `BUILD.bazel` files. |
| `bazel/cargo/` | The Bazel Cargo workspace: one Cargo workspace over every project, with its own `Cargo.lock` (see below). Generated. |
| `bazel/cargo/member_deps.MODULE.bazel` | Generated: `crate.annotation`s giving external crates their dependencies on in-tree crates (see "Cross-project dependencies"). |
| `bazel/rust.bzl` | `crate_library`, `crate_proc_macro`, `crate_binary`, `crate_unit_test`, `crate_integration_test`, `crate_build_script` wrappers around rules_rust, used by the generated files. |
| `bazel/BUILD.bazel` | clang builtin headers for bindgen; exports the test runner. |
| `bazel/process_per_test.bzl`, `bazel/process_per_test.sh` | Test rule that runs each test of a `rust_test` in its own process (used for node-launching tests). |
| `bazel/test_fuzz/` | Stub cargo package that `#[test_fuzz]`-instrumented tests point `cargo metadata` at. |

Per project (`<p>/`):

| Path | Purpose |
| --- | --- |
| `<p>.MODULE.bazel` | Optional: the project's crate_universe annotations (patches, build-script inputs of external crates). |
| `bazel/project.toml` | Hand-written: what Bazel needs to know about the project beyond its manifests (see below). |
| `bazel/workspace.bzl` | Generated: the `WORKSPACE` struct (version, edition, manifest and lint labels) and `[workspace.lints]`. |
| `bazel/BUILD.bazel` | Generated: the project's `rust_lint_config`. |
| `bazel/patches/` | Patches applied to external crates by crate_universe annotations (optional). |
| `<crate>/BUILD.bazel` | Generated per crate from its `Cargo.toml`. |

Every workspace crate `foo-bar` becomes `//<p>/path/to/crate:foo_bar` (its lib),
`//<p>/path/to/crate:foo_bar_test` (its `#[cfg(test)]` tests) and
`//<p>/path/to/crate:foo_bar_<name>_test` per `tests/<name>.rs`. Binaries keep
their Cargo name (`//reth/bin/reth:reth`, aliased as `//reth`). External crates
are `@crates//:<name>`, the same targets for every project.

## When you change a `Cargo.toml` or `Cargo.lock`

Run the generator, then repin crate_universe if external dependencies or
features changed:

```bash
python3 bazel/generate.py           # rewrites bazel/cargo/ and the BUILD.bazel files of every project (~5 s)
CARGO_BAZEL_REPIN=1 bazel mod deps   # rewrites Cargo.Bazel.lock (~1 min)
```

CI runs `bazel/generate.py --check` and builds with `--config=ci`
(`--lockfile_mode=error`), so a stale generated file or lockfile fails the
build with a message naming the file.

The generator needs `cargo` on `PATH` (any recent toolchain; it only runs
`cargo metadata`: `--locked` on each project, and unlocked on the Bazel
Cargo workspace so that `bazel/cargo/Cargo.lock` follows the projects).

## `bazel/project.toml`

Everything the generator cannot derive from Cargo, per project. Labels are
written in full. Keys:

* `disabled_features.<crate>.<feature> = "<reason>"`: default features not
  built by Bazel (see the Bazel Cargo workspace below).
* `build_scripts.<crate>`: `data_globs` (relative to the crate), `data`
  (labels) and `env` for the crate's `build.rs`. Build scripts run in the
  sandbox and see only their declared inputs.
* `compile_data.<crate>.<target> = [labels]`: files a target `include_*!`s
  that are not globbed automatically (outside `src/`, `res/`, `assets/` and
  the test-data directories); `<target>` is an integration test name or
  `crate` for the lib, binaries and unit tests. Files in another package need
  a full label and an `exported_files.<crate>` entry there.
* `filegroups.<crate>.<name> = [globs]`: public fixture groups for inputs
  shared across packages (for example alloy-core's ABI JSON files).
* `test_env.<crate>.<variable> = "value"`: runtime environment for the
  crate's unit and integration tests.
* `rust_bzl = "//project/bazel:rust.bzl"`: project-specific build/test macros;
  defaults to the shared rules. Tempo uses this for storage macros and test budgets.
* `cargo_test_names = true`: preserve Cargo integration-test crate names,
  required by Tempo's snapshot filenames.
* `process_per_test = [crates]`: tests of these crates and of their
  dependants run one process per test.
* `test_tags.<crate> = [tags]`: tags for the crate's test targets (`manual`
  keeps a test out of `bazel test //...`); or `test_tags.<crate>.<target>`
  for one target, `<target>` being an integration test name or `crate` for
  the unit tests.

See `reth/bazel/project.toml` for a commented example.

## The Bazel Cargo workspace (`bazel/cargo/`)

Bazel builds the projects from one Cargo workspace that the generator derives
from them, not from the projects' own workspaces. Two facts force this:

* A project depends on another in-tree (reth on alloy, through
  `[patch.crates-io]` in `reth/Cargo.toml`), and a crate compiled against
  `@x//:alloy-primitives` cannot be linked with one compiled against
  `@y//:alloy-primitives`. Everything that shares types must share one
  crate_universe repository, and one crate_universe repository is one Cargo
  resolution with one lockfile.
* Cargo does not nest workspaces, and the projects' `[workspace.package]` and
  `[workspace.dependencies]` tables (versions, feature sets) cannot be merged
  into one root without editing every crate.

So `bazel/cargo/` holds, for each member crate of each project, a *derived*
manifest at `bazel/cargo/<p>/<crate path>/Cargo.toml`: what `cargo metadata`
reports for that crate in its own project, with `workspace = true`
inheritance resolved, every target listed explicitly, path dependencies
pointing at the other derived manifests, and the sources reached through
symlinks (`src`, `build.rs`, `tests`, ...). `bazel/cargo/Cargo.toml` lists them
all as members and carries the union of the projects' `[patch]` sections
rebased onto the derived tree, plus a digest of every derived manifest so that
editing any crate's `Cargo.toml` invalidates `Cargo.Bazel.lock` (crate_universe
only watches the manifests it is given).

`bazel/cargo/Cargo.lock` is that workspace's lockfile and is committed. The
generator seeds it from the union of the projects' lockfiles the first time,
lets Cargo update it afterwards, and fails if it pins an external crate
version that no project's `Cargo.lock` pins (with the `cargo update
--precise` command to fix it). The projects' lockfiles are kept aligned with
each other for the same reason: whatever they disagree on, Bazel can only
build one version of.

The derived manifests also drop the features listed in `disabled_features`
from `default`. Cargo resolves features for the whole workspace at once,
using every member's default features, and crate_universe has no way to
subtract a feature from that resolution. reth drops two of `bin/reth`'s
defaults this way: `jit` (revmc -> llvm-sys, needs a system LLVM) and `gmp`
(gmp-mpfr-sys, builds GMP with autotools); reth falls back to the pure-Rust
`modexp` implementation.

`bazel/cargo/<p>/` is listed in the generated block of `.bazelignore` so
Bazel does not discover each crate a second time through the symlinks.
Features are unified across all projects: an alloy crate is built once, with
the features alloy's own crates and reth's together enable.

## Cross-project dependencies

reth uses alloy from the tree: `[patch.crates-io]` in `reth/Cargo.toml` points
every `alloy-*` crate at `../alloy/crates/*`, so `cargo` in `reth/` builds the
in-tree alloy. Under Bazel the same patch, carried into the Bazel Cargo
workspace, makes the alloy crates workspace members, so reth's generated
`BUILD.bazel` files depend on `//alloy/crates/<x>:alloy_<x>` directly and an
edit to alloy rebuilds (and retests) exactly the reth crates that use it.

The same goes for `reth-core/`, `alloy-evm/`, `revm-inspectors/` and `revm/`:
reth patches their crates to the tree, and they in turn patch `alloy-*` to
`../alloy/crates/*`, so a project's `[patch.crates-io]` lists every in-tree
project it uses, directly or through another in-tree project. A project may
be a single crate without a `[workspace]` (`revm-inspectors/`); its own
`[package]` and `[lints]` then stand in for the workspace tables. The Bazel
Cargo workspace uses the highest `resolver` any project asks for.

When a `[patch.crates-io]` leaves an external crate depending on a workspace
member, crate_universe drops that edge (it deliberately leaves
dependencies on workspace members out of the crates it renders). The
generator writes such edges back as
`crate.annotation(crate = ..., version = "=...", deps = ["@@//alloy/..."])` in
`bazel/cargo/member_deps.MODULE.bazel`, which the root `MODULE.bazel`
`include()`s. These annotations connect external crates (`ruint`, `nybbles`,
`discv5`, ...) to the in-tree alloy foundations, preserving type identity
across the graph.
Changing which external crates reach into the tree needs a repin
(`CARGO_BAZEL_REPIN=1 bazel mod deps`) after the generator.

One more thing the single workspace forces: crate_universe names its
`@crates//:<x>` aliases after the dependency's rename, and cannot render two
different packages under the same name (reth's `criterion = { package =
"codspeed-criterion-compat" }` next to revm-inspectors' real `criterion`).
The generator drops the rename in the derived manifest of the renaming crate
and restores it in its `BUILD.bazel` with `aliases`, so the sources still see
`criterion`. Feature references to that dependency are rewritten in the
derived manifest too, preserving public feature names and optionality.

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
* Windows is not supported (the Bazel Cargo workspace uses symlinks; no
  Windows platform triple is configured).
