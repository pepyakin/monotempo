# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-core project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-core",
    manifest = "//alloy-core:Cargo.toml",
    lints = "//alloy-core/bazel:lints",
    edition = "2024",
    version = "1.7.3",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "missing_copy_implementations": "warn",
    "missing_debug_implementations": "warn",
    "missing_docs": "warn",
    "rust_2018_idioms": "warn",
    "unreachable_pub": "warn",
    "unused_must_use": "warn",
    "redundant_lifetimes": "warn",
    "unnameable_types": "warn",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {
    "dbg_macro": "warn",
    "manual_string_new": "warn",
    "uninlined_format_args": "warn",
    "use_self": "warn",
    "redundant_clone": "warn",
    "missing_const_for_fn": "warn",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
