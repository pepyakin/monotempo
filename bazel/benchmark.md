# CI build stopwatch

Run the **build stopwatch** workflow manually, after the prerequisite CI fix
has landed and the normal Bazel build/test workflow is green. Start with one
Alloy `ci` repetition as a smoke test, then at least three repetitions for each
of `alloy`/`tempo` × `ci`/`dev`. The script refuses to benchmark outside GitHub
Actions. Do not report smoke-test timings as a stable result.

For the scoped comparison matrix, select `workload: all`, `mode: both`,
`scoped: true`, and four repetitions. The six jobs run serially, retain separate
artifacts, and continue independently if one fails. Four repetitions balance
Cargo-first and Bazel-first order. `all` includes the node, so it requires scoped
mode; these aggregate choices belong to the workflow, not the Python CLI.

Set `profile: true` (`benchmark.py --profile`) to capture Cargo's stable HTML
`--timings` report and Bazel's compressed JSON action profile for each cold
`build`/`test_compile` phase. Profiles carry unique sample prefixes and survive
cleanup of build outputs. Bazel events include target labels and primary outputs
for matching against the audited graph. Profiling is inside those measured
commands, so compare profiled repetitions with each other; do not attribute small
differences from older unprofiled runs to optimizations. Warm phases are unchanged.

## Workload boundaries

Without `--scoped`, the historical workloads remain:

| Choice | Cargo workspace/package | Bazel library and unit-test targets |
|---|---|---|
| `alloy` | `alloy/`, `alloy-consensus --lib` | `//alloy/crates/consensus:alloy_consensus{,_test}` |
| `tempo` | `tempo/`, `tempo-payload-builder --lib` | `//tempo/crates/payload/builder:tempo_payload_builder{,_test}` |

These are package development workloads, **not** full Tempo node builds,
project-wide test suites, doctests, release builds, or a comparison of
`bazel //...` against a single Cargo project. Both suites work offline and
use ordinary libtest, with eight test threads on each side. Root features
come from the generated Bazel `FEATURES` and are explicitly passed to Cargo.
Exact test names and ignored-test sets must match before a summary is emitted.

**Dependency features are still different.** Bazel globally unifies features
and uses its generated lockfile; Cargo uses the project's own workspace and
lockfile. Artifact logs retain `cargo tree -e features`, Bazel dependency rule
dumps, verbose Cargo compiler commands/artifact events, Bazel action queries,
and Bazel build events. Interpret ratios as timings of these configured
workflows, not a pure comparison of build engines with identical compiler
inputs. Do not switch Cargo to `bazel/cargo/`
to disguise this difference: that is not the project's Cargo setup.

## Opt-in scoped-graph prototype

The workflow's `scoped: true` input (`benchmark.py --scoped`)
is a **separate diagnostic comparison**, not a replacement for the project-local
Cargo baseline above. Both sides use `bazel/cargo/Cargo.lock` and the following
root selections, always with `--target x86_64-unknown-linux-gnu`:

| Workflow workload | Scope preset | Cargo selection | Checks |
|---|---|---|---|
| `tempo` | `tempo-payload-builder` | `-p tempo-payload-builder --lib --no-default-features` | Library build and unit tests |
| `alloy` | `alloy-consensus` | `-p alloy-consensus --lib` (default features) | Library build and unit tests |
| `tempo-node` | `tempo-node` | `-p tempo --bin tempo` (default features) | Binary build and offline `--help`/`--version` |

`tempo-node` requires scoped mode. It does not start a node, run network-dependent
CLI tests, or claim coverage of Tempo's integration suite. Its cold/noop/leaf/
foundation scenarios measure only `build`; the leaf edit targets `src/main.rs`.
Alloy's scoped root uses Cargo defaults, not the historical globally generated
root feature list, so its test inventory can differ from the unscoped benchmark.
The scoped Cargo and Bazel inventories must still match each other exactly.

Cargo runs from a temporary manifest-only view of the shared workspace.
That view restores source-level dependency aliases erased for crate-universe
hub generation (for example `hex`), exposes build-script data such as `libmdbx`,
and points back to the original sources. Its lockfile must remain byte-identical
to the shared lockfile. No second dependency hub, source checkout, or independent
version resolution is added.

`tempo_scope.py --scope <preset>` asks the pinned Cargo for build and (for
library presets) test `--unit-graph` plans.
This unstable inspection API needs `RUSTC_BOOTSTRAP=1`, confined to these
non-compiling commands. The plans distinguish host/target units and build/test
features. The prototype copies the already-patched rules_rust repository into
the trial's temporary directory and wraps its public rules to instantiate
additional, explicitly named variants in the existing source packages. It
retains generated native inputs, tool settings and annotations, but replaces
features, Rust dependencies, renames and native `links` edges from Cargo's plans.
Normal targets and committed BUILD files are unchanged.
Variant names depend on compilation inputs, not the preset name: identical
dependency units get identical labels across presets. Each benchmark still uses
fresh outputs; this does not measure cross-preset cache reuse.

Before compilation, an action-query audit checks every application compiler
target's exact features, `--extern` edges and host/target context, and rejects
escapes to globally unified targets. Plans, actual actions and the audit verdict
are uploaded with the measurements. The normal exact test-inventory check still
applies. Generation/auditing are untimed setup, so this does **not** measure the
cost of regenerating scoped targets after a manifest edit.

This is limited to the three Linux x86_64 presets above and the pinned
rules_rust layout. It is not automatic feature resolution for arbitrary Bazel
roots. Compiler/profile flags, build-script execution settings, downstream source
patches, repeated build-script compilation and metadata pipelining can still
differ; passing the graph audit does not establish instruction-for-instruction
equivalence. Keep its numbers separate from historical project-local results.

## Scenarios and phases

Every repetition starts with private, empty Bazel output and Cargo target
directories, without disk/remote action caches or compiler wrappers. Dependency
downloads, extraction and server startup are outside the measured intervals;
their commands and times remain in `samples.jsonl` with `measured: false`.
"Cold" means **cold compiled outputs**, not cold downloads or OS page cache.

1. **cold:** compile the library and then its test binary from scratch (or just
   the node binary for `tempo-node`).
2. **noop:** repeat with unchanged sources and warm build outputs.
3. **leaf:** add a public, non-inlined arithmetic probe to the selected root.
4. **foundation:** independently add the same probe to `alloy-primitives`, an
   in-tree dependency of both workloads. This invalidates a much wider closure.

Edits are identical for both tools, restored in `finally`, and never committed.
Each edited scenario starts from a restored, re-primed baseline, not from the
previous edit. This tests additive public API invalidation, not every kind of
real edit; private body changes, comment-only edits, and ABI changes may behave
differently. An mtime-only `touch` would unfairly favor content-addressed Bazel.

Each scenario reports separate wall-clock intervals:

* `build`: library or node-binary compilation (including tool startup/analysis).
* `test_compile`: test-binary compilation **after** the library build, not an
  independent cold `cargo test`. Sum build + test_compile for total compilation.
* `test_run`: test command with compiled outputs warm; force Bazel to execute
  tests. Includes command/harness overhead, not just time inside test functions.
* `test_cached`: repeat the test command allowing result reuse. Bazel can skip
  execution; Cargo reruns tests. This is feedback latency, not test throughput.

## Controls and remaining differences

Both tools run sequentially on the **same runner in one job**, alternating
first tool across repeats. The report includes each sample, medians, ranges,
commit, machine information, arguments and exit status. A failed build/test or
inventory mismatch fails the job instead of producing a speedup summary.
Three repeats have a 2:1 order imbalance; use four for a balanced order.

Both use the pinned Rust version and Bazel's fetched LLVM/clang/lld distribution.
Target profiles use opt-level 0, no debug info and four codegen units. `ci`
disables Cargo incremental compilation, matching Bazel's default. `dev` enables
Cargo incremental and Bazel's hermetic Linux incremental sandbox; it **keeps
four codegen units**, overriding the usual Bazel dev value of 256. This isolates
incremental cache effects; it is not a benchmark of untouched developer defaults.
This opt-in diagnostic use of `--config=dev` does not change normal CI policy.

Both tools inherit CPU affinity restricted to the requested number of CPUs;
requests exceeding the runner's available CPU set fail. Job budgets also match,
but scheduler policies do not: Bazel reserves one CPU and 3072 MB per rustc
action, while Cargo's `-j` limits jobs. Both now use unoptimized host tools.
Exec debug assertions remain disabled in Bazel (unlike Cargo) to preserve
build-script behavior, notably BLST's choice of native optimization level.
The original stopwatch runs reserved four CPUs per Bazel rustc action and
used optimized host tools; compare commits explicitly when measuring these
changes. Build scripts/patches/sandboxing still differ from Cargo's. No claim of
instruction-for-instruction equivalence is made. Test RNG seeds are fixed where
proptest supports them; that does not make all runtime noise deterministic.

Run with an otherwise idle runner. Workflow concurrency serializes stopwatch
runs but **cannot prevent other workflows or external processes from competing**.
CPU quotas, thermal state, page caches and storage contention remain confounders.
The workflow records hardware information; vary the job budget (4/8/16) to
simulate different CPU budgets without claiming to have measured actual laptops.
Allow substantially more disk than normal CI: each paired run holds both tools'
outputs and, in dev mode, incremental caches. Temporary build trees are removed
between pairs; raw evidence remains in the uploaded artifact for 30 days.
