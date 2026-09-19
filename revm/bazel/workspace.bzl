# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the revm project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "revm",
    manifest = "//revm:Cargo.toml",
    lints = "//revm/bazel:lints",
    edition = "2021",
    version = None,
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "rust_2018_idioms": "deny",
    "missing_debug_implementations": "warn",
    "missing_docs": "warn",
    "unreachable_pub": "warn",
    "unused_must_use": "deny",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {
    "missing_const_for_fn": "warn",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
