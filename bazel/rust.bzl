"""Thin wrappers around rules_rust used by the generated per-crate BUILD files.

The wrappers hold everything that is the same for every crate of a Cargo
workspace (edition, package version, workspace lints, `Cargo.toml` env vars,
data globs) so that the generated `BUILD.bazel` files only carry per-crate
facts: crate root, features, and dependencies.

Every macro takes the `WORKSPACE` struct of the crate's project (generated
into `<project>/bazel/workspace.bzl` from the root `Cargo.toml`), which
supplies the values inherited from `[workspace.package]` and the labels of the
root manifest and the lint config.

Regenerate BUILD files with `python3 bazel/generate.py`.
"""

load("@rules_rust//cargo:defs.bzl", "cargo_build_script", "cargo_toml_env_vars")
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_library", "rust_proc_macro", "rust_test")
load("//bazel:process_per_test.bzl", "process_per_test")

# Non-Rust files a crate may `include_str!`/`include_bytes!` at compile time.
_COMPILE_DATA_DIRS = ["src", "res", "assets"]

# Non-Rust files tests may read at run time (relative to CARGO_MANIFEST_DIR).
_TEST_DATA_DIRS = _COMPILE_DATA_DIRS + ["tests", "testdata", "test-data", "test_data"]

# Keeps the per-action sandbox path out of objects that `cc`-based build scripts
# (reth-mdbx-sys, ...) compile, so their output is bit-identical across clean
# builds; rustc gets the equivalent `--remap-path-prefix` from rules_rust. `$$`
# escapes Bazel's "Make" variable expansion; the build-script runner then
# substitutes `${pwd}`. Same flags as the `crate = "*"` annotation in
# //MODULE.bazel, which covers external crates.
_REPRODUCIBLE_CC_ENV = {
    "CFLAGS": "-ffile-prefix-map=$${pwd}=.",
    "CXXFLAGS": "-ffile-prefix-map=$${pwd}=.",
}

# Manifests some proc macros read at compile time: `proc-macro-crate` (used by
# jsonrpsee's `#[rpc]`, among others) opens `$CARGO_MANIFEST_DIR/Cargo.toml` to
# find out what the crate calls its dependencies, and follows `workspace = true`
# entries up to the workspace root.
def _manifests(workspace):
    if workspace.manifest == None:
        return ["Cargo.toml"]  # single-crate project: the crate manifest is the root
    return ["Cargo.toml", workspace.manifest]

# Threads per test process. libtest defaults to the core count, but every
# test that opens an MDBX environment reserves 8 TiB of virtual address
# space, and on x86-64 (47-bit user VA) sixteen concurrent environments
# exhaust it with `Cannot allocate memory`. `cargo nextest` avoids this by
# running one process per test; Bazel instead runs test *targets* in
# parallel, so a lower per-process thread count costs little throughput.
_TEST_THREADS = "8"

def _non_rust_files(workspace, dirs):
    # `*.md` covers `#![doc = include_str!("../README.md")]`.
    return _manifests(workspace) + native.glob(
        [d + "/**" for d in dirs] + ["*.md"],
        exclude = ["**/*.rs"],
        allow_empty = True,
    )

def _rust_srcs(dirs):
    return native.glob([d + "/**/*.rs" for d in dirs], allow_empty = True)

def crate_cargo_toml_env_vars(workspace, name = "cargo_toml_env_vars"):
    """Exposes `CARGO_PKG_*` env vars derived from the crate's Cargo.toml.

    Args:
        workspace: The project's `WORKSPACE` struct.
        name: Target name; the other macros expect the default.
    """
    cargo_toml_env_vars(
        name = name,
        src = "Cargo.toml",
        workspace = workspace.manifest,
    )

def _check_cfg_flags(declared_features):
    """The `--check-cfg` flags cargo passes to every crate.

    `unexpected_cfgs` is warn-by-default in rustc, so without these every
    `#[cfg(feature = "...")]`, `#[cfg(test)]` and `#[cfg(docsrs)]` would warn.
    Workspace-level `check-cfg` entries come from the project's lint config.
    """
    values = ", ".join(['"%s"' % f for f in declared_features])
    return [
        "--check-cfg=cfg(docsrs,test)",
        "--check-cfg=cfg(feature, values(%s))" % values,
    ]

def _common_kwargs(workspace, crate_features, declared_features, workspace_lints, kwargs):
    common = dict(
        edition = workspace.edition,
        version = workspace.version,
        crate_features = crate_features,
        rustc_flags = _check_cfg_flags(declared_features) + kwargs.pop("rustc_flags", []),
        rustc_env_files = [":cargo_toml_env_vars"],
        lint_config = workspace.lints if workspace_lints else None,
    )
    common.update(kwargs)
    return common

# What `#[test_fuzz]`-instrumented tests need at run time: they shell out to
# `cargo metadata` to find a `target/` directory for the corpus they record.
# `//bazel/test_fuzz` is a self-contained stub package so that neither the
# real workspace nor a host cargo is needed; see its Cargo.toml.
_TEST_FUZZ_DATA = [
    "//bazel/test_fuzz:Cargo.toml",
    "//bazel/test_fuzz:src/lib.rs",
    "@rules_rust//rust/toolchain:current_cargo_files",
]

_TEST_FUZZ_ENV = {
    "CARGO": "$(rootpath @rules_rust//rust/toolchain:current_cargo_files)",
    "TEST_FUZZ_MANIFEST_PATH": "$(rootpath //bazel/test_fuzz:Cargo.toml)",
}

def _test_kwargs(workspace, crate_features, declared_features, workspace_lints, test_fuzz, kwargs):
    """`_common_kwargs` plus what only test targets need.

    rules_rust bakes an absolute sandbox path into `CARGO_MANIFEST_DIR` at
    compile time, so `env!("CARGO_MANIFEST_DIR")` (used by tests to locate
    `testdata/`) would point at a directory that no longer exists when the
    test runs. Overriding it with the package path keeps it valid at both
    points, since rustc runs from the exec root and the test from the
    runfiles root, and the package lives at the same relative path in both.
    """
    rustc_env = {"CARGO_MANIFEST_DIR": native.package_name()}
    rustc_env.update(kwargs.pop("rustc_env", {}))
    env = {
        "RUST_TEST_THREADS": _TEST_THREADS,
        # insta resolves snapshot files as `<workspace root>/<file!()>`, and
        # would otherwise try `cargo metadata` (no cargo in the sandbox) and
        # fall back to the manifest dir, doubling the crate path. `file!()`
        # is exec-root relative under Bazel, so the root is the cwd.
        "INSTA_WORKSPACE_ROOT": ".",
        # Writing `.snap.new` files into the sandbox is pointless; review
        # snapshots with `cargo insta`.
        "INSTA_UPDATE": "no",
    }
    if test_fuzz:
        env.update(_TEST_FUZZ_ENV)
    env.update(kwargs.pop("env", {}))
    return _common_kwargs(
        workspace,
        crate_features,
        declared_features,
        workspace_lints,
        dict(kwargs, rustc_env = rustc_env, env = env),
    )

def _rust_test(name, per_process, size, tags, flaky = False, **kwargs):
    """Defines the test target, optionally behind a `process_per_test` wrapper.

    With `per_process`, the `rust_test` itself becomes `<name>_bin` (tagged
    `manual`, so `bazel test //...` runs only the wrapper; `bazel run` it to
    invoke the plain binary) and `<name>` runs each of its tests in a fresh
    process.
    """
    if not per_process:
        rust_test(name = name, size = size, tags = tags, flaky = flaky, **kwargs)
        return
    rust_test(name = name + "_bin", tags = ["manual"], flaky = flaky, **kwargs)
    process_per_test(
        name = name,
        test = ":" + name + "_bin",
        size = size,
        tags = tags,
        flaky = flaky,
    )

def crate_library(
        name,
        workspace,
        crate_name = None,
        crate_root = "src/lib.rs",
        crate_features = [],
        declared_features = [],
        workspace_lints = True,
        deps = [],
        proc_macro_deps = [],
        aliases = {},
        compile_data = [],
        build_script = None,
        **kwargs):
    """A workspace crate's `[lib]` target.

    Args:
        name: Bazel target name; also the Rust crate name unless `crate_name` is set.
        workspace: The project's `WORKSPACE` struct (from `<project>/bazel/workspace.bzl`).
        crate_name: Rust crate name, when a sibling binary already uses `name`.
        crate_root: Path to the crate root (`src/lib.rs` unless `[lib] path` is set).
        crate_features: Cargo features to enable (`--cfg feature=...`).
        declared_features: Every feature the crate declares (for `--check-cfg`).
        workspace_lints: Whether the crate has `[lints] workspace = true`.
        deps: Normal dependencies (workspace crates and `@crates//...` aliases).
        proc_macro_deps: Normal proc-macro dependencies.
        aliases: Mapping of dependency label to the `extern crate` name to use
            (mirrors `package = "..."` renames in Cargo.toml).
        compile_data: Additional compile-time data (files included via `include_*!`).
        build_script: Label of the crate's `cargo_build_script`, if it has one.
        **kwargs: Forwarded to `rust_library`.
    """
    if build_script:
        deps = deps + [build_script]
    rust_library(
        name = name,
        crate_name = crate_name or name,
        crate_root = crate_root,
        srcs = _rust_srcs(["src"]),
        deps = deps,
        proc_macro_deps = proc_macro_deps,
        aliases = aliases,
        compile_data = compile_data + _non_rust_files(workspace, _COMPILE_DATA_DIRS),
        visibility = ["//visibility:public"],
        **_common_kwargs(workspace, crate_features, declared_features, workspace_lints, kwargs)
    )

def crate_proc_macro(
        name,
        workspace,
        crate_name = None,
        crate_root = "src/lib.rs",
        crate_features = [],
        declared_features = [],
        workspace_lints = True,
        deps = [],
        proc_macro_deps = [],
        aliases = {},
        compile_data = [],
        build_script = None,
        **kwargs):
    """A workspace crate's `[lib] proc-macro = true` target.

    Takes the same arguments as `crate_library`.
    """
    if build_script:
        deps = deps + [build_script]
    rust_proc_macro(
        name = name,
        crate_name = crate_name or name,
        crate_root = crate_root,
        srcs = _rust_srcs(["src"]),
        deps = deps,
        proc_macro_deps = proc_macro_deps,
        aliases = aliases,
        compile_data = compile_data + _non_rust_files(workspace, _COMPILE_DATA_DIRS),
        visibility = ["//visibility:public"],
        **_common_kwargs(workspace, crate_features, declared_features, workspace_lints, kwargs)
    )

def crate_binary(
        name,
        workspace,
        crate_root = "src/main.rs",
        crate_features = [],
        declared_features = [],
        workspace_lints = True,
        deps = [],
        proc_macro_deps = [],
        aliases = {},
        compile_data = [],
        build_script = None,
        **kwargs):
    """A workspace crate's `[[bin]]` target.

    Takes the same arguments as `crate_library`. `name` is the binary name.
    """
    if build_script:
        deps = deps + [build_script]
    rust_binary(
        name = name,
        crate_root = crate_root,
        srcs = _rust_srcs(["src"]),
        deps = deps,
        proc_macro_deps = proc_macro_deps,
        aliases = aliases,
        compile_data = compile_data + _non_rust_files(workspace, _COMPILE_DATA_DIRS),
        visibility = ["//visibility:public"],
        **_common_kwargs(workspace, crate_features, declared_features, workspace_lints, kwargs)
    )

def crate_unit_test(
        name,
        workspace,
        crate,
        crate_features = [],
        declared_features = [],
        workspace_lints = True,
        deps = [],
        proc_macro_deps = [],
        aliases = {},
        data = [],
        test_fuzz = False,
        process_per_test = False,
        size = "large",
        tags = [],
        **kwargs):
    """The `#[cfg(test)]` unit tests of a library or binary crate.

    Args:
        name: Test target name.
        workspace: The project's `WORKSPACE` struct.
        crate: Label of the `crate_library`/`crate_binary` whose sources are compiled with `--test`.
        crate_features: Same features as `crate` (rules_rust does not inherit them).
        declared_features: Every feature the crate declares (for `--check-cfg`).
        workspace_lints: Whether the crate has `[lints] workspace = true`.
        deps: `[dev-dependencies]`; the wrapped crate's normal deps are inherited.
        proc_macro_deps: Proc-macro `[dev-dependencies]`.
        aliases: Renames for dev-dependencies.
        data: Extra runtime data.
        test_fuzz: Whether the crate uses `test-fuzz` (a `[dev-dependencies]` entry).
        process_per_test: Run each test in its own process (see `process_per_test.bzl`).
        size: Bazel test size (controls the default timeout).
        tags: Bazel tags for the test target.
        **kwargs: Forwarded to `rust_test`.
    """
    if test_fuzz:
        data = data + _TEST_FUZZ_DATA
    test_data = _non_rust_files(workspace, _TEST_DATA_DIRS)
    _rust_test(
        name = name,
        per_process = process_per_test,
        size = size,
        tags = tags,
        crate = crate,
        deps = deps,
        proc_macro_deps = proc_macro_deps,
        aliases = aliases,
        # Merged with the crate's own compile_data by rules_rust.
        compile_data = test_data,
        data = data + test_data,
        **_test_kwargs(workspace, crate_features, declared_features, workspace_lints, test_fuzz, kwargs)
    )

def crate_integration_test(
        name,
        workspace,
        crate_root,
        crate_features = [],
        declared_features = [],
        workspace_lints = True,
        deps = [],
        proc_macro_deps = [],
        aliases = {},
        data = [],
        compile_data = [],
        test_fuzz = False,
        process_per_test = False,
        size = "large",
        tags = [],
        **kwargs):
    """A `tests/*.rs` integration test target.

    Args:
        name: Test target name.
        workspace: The project's `WORKSPACE` struct.
        crate_root: The test's root file, e.g. `tests/it/main.rs`.
        crate_features: Features of the crate under test.
        declared_features: Every feature the crate under test declares (for `--check-cfg`).
        workspace_lints: Whether the crate under test has `[lints] workspace = true`.
        deps: The crate under test plus its normal and dev dependencies.
        proc_macro_deps: Proc-macro dependencies (normal and dev).
        aliases: Dependency renames.
        data: Extra runtime data.
        compile_data: Extra compile-time data.
        test_fuzz: Whether the crate uses `test-fuzz` (a `[dev-dependencies]` entry).
        process_per_test: Run each test in its own process (see `process_per_test.bzl`).
        size: Bazel test size (controls the default timeout).
        tags: Bazel tags for the test target.
        **kwargs: Forwarded to `rust_test`.
    """
    test_data = _non_rust_files(workspace, _TEST_DATA_DIRS)
    if test_fuzz:
        data = data + _TEST_FUZZ_DATA
    _rust_test(
        name = name,
        per_process = process_per_test,
        size = size,
        tags = tags,
        crate_root = crate_root,
        srcs = _rust_srcs(["tests"]),
        deps = deps,
        proc_macro_deps = proc_macro_deps,
        aliases = aliases,
        compile_data = compile_data + test_data,
        data = data + test_data,
        **_test_kwargs(workspace, crate_features, declared_features, workspace_lints, test_fuzz, kwargs)
    )

def crate_build_script(
        name,
        workspace,
        version = None,
        crate_features = [],
        deps = [],
        data = [],
        build_script_env = {},
        **kwargs):
    """A crate's `build.rs`, compiled and executed at build time.

    Args:
        name: Target name, referenced by the library's `build_script` argument.
        workspace: The project's `WORKSPACE` struct.
        crate_features: Features of the owning crate (exposed as `CARGO_FEATURE_*`).
        deps: `[build-dependencies]`.
        data: Files the script reads (relative to `CARGO_MANIFEST_DIR`).
        build_script_env: Extra environment for the script; supports `$(execpath ...)`.
            `CFLAGS`/`CXXFLAGS` are appended to the reproducibility defaults.
        **kwargs: Forwarded to `cargo_build_script`.
    """
    env = dict(_REPRODUCIBLE_CC_ENV)
    for key, value in build_script_env.items():
        env[key] = (env[key] + " " + value) if key in env else value
    cargo_build_script(
        name = name,
        srcs = ["build.rs"],
        edition = workspace.edition,
        version = version if version != None else workspace.version,
        crate_features = crate_features,
        deps = deps,
        data = data,
        build_script_env = env,
        rustc_env_files = [":cargo_toml_env_vars"],
        **kwargs
    )
