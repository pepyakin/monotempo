"""Cargo-compatible rules for Tempo's generated targets."""

load("@rules_rust//cargo:defs.bzl", "cargo_build_script", "cargo_toml_env_vars")
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_library", "rust_proc_macro", "rust_test")
load("//reth/bazel:process_per_test.bzl", _process_per_test = "process_per_test")
load(":workspace.bzl", "PACKAGE_VERSIONS", "RUST_EDITION")

def tempo_cargo_toml_env_vars():
    cargo_toml_env_vars(
        name = "cargo_toml_env_vars",
        src = "Cargo.toml",
        workspace = "//tempo:Cargo.toml",
    )

def _data():
    return ["Cargo.toml", "//tempo:Cargo.toml"] + native.glob(
        ["src/**", "tests/**", "testdata/**", "assets/**", "abi/**", "*.md"],
        exclude = ["**/*.rs"],
        allow_empty = True,
    )

def _common(crate_features, declared_features, workspace_lints):
    return dict(
        edition = RUST_EDITION,
        version = PACKAGE_VERSIONS[native.package_name()],
        crate_features = crate_features,
        rustc_env_files = [":cargo_toml_env_vars"],
        rustc_flags = [
            "--check-cfg=cfg(docsrs,test)",
            "--check-cfg=cfg(feature, values(%s))" % ", ".join(['"%s"' % f for f in declared_features]),
        ],
        lint_config = "//tempo/bazel:lints" if workspace_lints else None,
    )

def _compile(rule, name, crate_root, crate_features, declared_features, workspace_lints, deps, compile_data, build_script, **kwargs):
    common = _common(crate_features, declared_features, workspace_lints)
    common["rustc_flags"] += kwargs.pop("rustc_flags", [])
    common.update(kwargs)
    rule(
        name = name,
        crate_root = crate_root,
        srcs = native.glob(["src/**/*.rs"]),
        deps = deps + ([build_script] if build_script else []),
        compile_data = compile_data + _data(),
        visibility = ["//visibility:public"],
        **common
    )

def tempo_library(name, crate_root = "src/lib.rs", crate_features = [], declared_features = [], workspace_lints = True, deps = [], compile_data = [], build_script = None, **kwargs):
    _compile(rust_library, name, crate_root, crate_features, declared_features, workspace_lints, deps, compile_data, build_script, **kwargs)

def tempo_proc_macro(name, crate_root = "src/lib.rs", crate_features = [], declared_features = [], workspace_lints = True, deps = [], compile_data = [], build_script = None, **kwargs):
    # Bazel builds host tools in opt mode, but this macro generates the storage
    # layout constants used by tests only when *it* has debug assertions enabled.
    _compile(rust_proc_macro, name, crate_root, crate_features, declared_features, workspace_lints, deps, compile_data, build_script, rustc_flags = ["-Cdebug-assertions=yes"], **kwargs)

def tempo_binary(name, crate_root = "src/main.rs", crate_features = [], declared_features = [], workspace_lints = True, deps = [], compile_data = [], build_script = None, **kwargs):
    _compile(rust_binary, name, crate_root, crate_features, declared_features, workspace_lints, deps, compile_data, build_script, **kwargs)

def _test(name, crate_features, declared_features, workspace_lints, process_per_test, tags, data, compile_data, **kwargs):
    # CLI tests initialize global defaults. Match nextest's process isolation
    # so one test cannot initialize a OnceLock before another test configures it.
    process_per_test = process_per_test or native.package_name() == "tempo/bin/tempo"
    rust_test(
        name = name + "_bin" if process_per_test else name,
        size = "large",
        tags = ["manual"] if process_per_test else tags,
        data = data + _data(),
        compile_data = compile_data + _data(),
        # Keep fixture paths valid both at compile time and in test runfiles.
        rustc_env = {"CARGO_MANIFEST_DIR": native.package_name()},
        env = {
            "RUST_TEST_THREADS": "8",
            # A multi-node e2e test alone can retain over 10 GiB. Bazel can run
            # suites in parallel, but fan-out within each suite exhausts RAM.
            "RETH_TEST_PROCESSES": "1",
            "INSTA_WORKSPACE_ROOT": ".",
            "INSTA_UPDATE": "no",
        },
        **dict(_common(crate_features, declared_features, workspace_lints), **kwargs)
    )
    if process_per_test:
        _process_per_test(name = name, test = ":" + name + "_bin", size = "large", tags = tags)

def tempo_unit_test(name, crate, crate_features = [], declared_features = [], workspace_lints = True, process_per_test = False, tags = [], data = [], **kwargs):
    _test(name, crate_features, declared_features, workspace_lints, process_per_test, tags, data, [], crate = crate, **kwargs)

def tempo_integration_test(name, crate_root, crate_features = [], declared_features = [], workspace_lints = True, process_per_test = False, tags = [], data = [], compile_data = [], **kwargs):
    _test(
        name, crate_features, declared_features, workspace_lints, process_per_test, tags, data, compile_data,
        crate_root = crate_root,
        srcs = native.glob(["tests/**/*.rs"]),
        **kwargs
    )

def tempo_build_script(name, **kwargs):
    cargo_build_script(
        name = name,
        srcs = ["build.rs"],
        edition = RUST_EDITION,
        version = PACKAGE_VERSIONS[native.package_name()],
        rustc_env_files = [":cargo_toml_env_vars"],
        **kwargs
    )
