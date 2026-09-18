# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the revm-inspectors project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "revm-inspectors",
    manifest = None,
    lints = "//revm-inspectors/bazel:lints",
    edition = "2021",
    version = "0.43.0",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "missing_debug_implementations": "warn",
    "missing_docs": "warn",
    "unreachable_pub": "warn",
    "unused_must_use": "deny",
    "rust_2018_idioms": "deny",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {
    "lint_groups_priority": "allow",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
