# Tempo in monotempo

Run Bazel from the monorepo root:

```sh
bazel build //tempo
bazel test //tempo/crates/hardfork/...
bazel test //tempo/...
```

Tempo remains its own Cargo workspace. `Cargo.toml` and `Cargo.lock` are
unchanged from upstream; Bazel uses the default workspace feature resolution.
Optional binaries such as `tempo-localnet` are omitted when their required
features are disabled. Examples and benchmarks are not generated as Bazel
targets.

After changing a manifest, regenerate and repin:

```sh
python3 tempo/scripts/bazel/generate.py
CARGO_BAZEL_REPIN=1 CARGO_BAZEL_REPIN_ONLY=tempo_crates bazel mod deps
python3 tempo/scripts/bazel/generate.py --check
```

The generator reuses the shared `bazel/generate.py` resolver and renderer. All member
manifests are explicit crate_universe inputs so dependency edits invalidate
`Cargo.Bazel.lock`. Unlike reth, Tempo needs no shadow Cargo workspace.

Tempo currently consumes its locked upstream reth Git revision, **not the
local `reth/` targets**. The two workspaces pin different revisions and resolve
their external crates independently (`@tempo_crates` versus `@crates`).
Aligning those versions and sharing the dependency graph is a separate change;
simply replacing labels can link incompatible versions of Rust types.

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
