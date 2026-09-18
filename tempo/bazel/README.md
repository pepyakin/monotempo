# Tempo in monotempo

Run Bazel from the monorepo root:

```sh
bazel build //tempo
bazel test //tempo/crates/hardfork/...
bazel test //tempo/...
```

Tempo remains its own Cargo workspace. Its reth dependencies point into
`../reth`, and `[patch.crates-io]` redirects Alloy, reth-core, alloy-evm,
revm-inspectors and the Alloy foundations to their local sources, including
transitive dependencies. Bazel shares the monorepo's unified feature resolution.
Optional binaries such as `tempo-localnet` are omitted when their required
features are disabled. Examples and benchmarks are not generated as Bazel
targets.

After changing a manifest, regenerate and repin:

```sh
python3 bazel/generate.py
CARGO_BAZEL_REPIN=1 bazel mod deps
python3 bazel/generate.py --check
```

`tempo/bazel/project.toml` registers Tempo with the shared generator. Its
derived manifests live in `bazel/cargo/tempo/`; all projects resolve together
through `@crates`, pinned by the root `Cargo.Bazel.lock`. Tempo's Rust macros
remain project-specific to preserve its snapshot names and test policies.

To inspect the local dependency chain:

```sh
bazel query 'somepath(//tempo, //reth/crates/node/builder:reth_node_builder)'
bazel query 'somepath(//tempo, //alloy-core/crates/primitives:alloy_primitives)'
```

Build scripts use the root's Rust/LLVM toolchains. Version metadata uses
`VERGEN_IDEMPOTENT=1`, as in reth, rather than depending on `.git` or the clock.
The shared Rust pin is 1.96.1: Tempo's tests use `std::assert_matches!`, which
is unavailable on the manifest's declared 1.95 MSRV.

Node-launching and CLI tests run one test per process, like upstream's
`cargo nextest`, to bound node memory use and isolate global CLI defaults.
They run serially within each suite: a single multi-node e2e test can retain
over 10 GiB. Use `--local_test_jobs=1` to also serialize suites on smaller hosts.
The e2e and node suites have Bazel's `enormous` size (a one-hour timeout)
because serial recovery and RPC scenarios exceed the normal 15-minute budget.
The storage proc macro is compiled with debug assertions so it emits the
layout constants consumed by the Solidity compatibility tests. Pipelining is
disabled only for `age`, whose separate metadata/library builds otherwise
produce incompatible crate hashes.
