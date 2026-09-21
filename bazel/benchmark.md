# CI build stopwatch

Run the **build stopwatch** workflow manually, after the prerequisite CI fix
has landed and the normal Bazel build/test workflow is green. Start with one
Alloy `ci` repetition as a smoke test, then at least three repetitions for each
of `alloy`/`tempo` × `ci`/`dev`. The script refuses to benchmark outside GitHub
Actions. Do not report smoke-test timings as a stable result.

## Workload boundaries

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

## Scenarios and phases

Every repetition starts with private, empty Bazel output and Cargo target
directories, without disk/remote action caches or compiler wrappers. Dependency
downloads, extraction and server startup are outside the measured intervals;
their commands and times remain in `samples.jsonl` with `measured: false`.
"Cold" means **cold compiled outputs**, not cold downloads or OS page cache.

1. **cold:** compile the library and then its test binary from scratch.
2. **noop:** repeat with unchanged sources and warm build outputs.
3. **leaf:** add a public, non-inlined arithmetic probe to the selected library.
4. **foundation:** independently add the same probe to `alloy-primitives`, an
   in-tree dependency of both workloads. This invalidates a much wider closure.

Edits are identical for both tools, restored in `finally`, and never committed.
Each edited scenario starts from a restored, re-primed baseline, not from the
previous edit. This tests additive public API invalidation, not every kind of
real edit; private body changes, comment-only edits, and ABI changes may behave
differently. An mtime-only `touch` would unfairly favor content-addressed Bazel.

Each scenario reports separate wall-clock intervals:

* `build`: library compilation (including tool startup/analysis).
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
but scheduler policies do not: Bazel reserves four CPUs per rustc action,
while Cargo's `-j` limits jobs. Bazel optimizes host tools,
and its build scripts/patches/sandboxing differ from Cargo's. No claim of
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
