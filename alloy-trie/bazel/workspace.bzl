# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-trie project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-trie",
    manifest = None,
    lints = "//alloy-trie/bazel:lints",
    edition = "2024",
    version = "0.9.5",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "missing-debug-implementations": "warn",
    "missing-docs": "warn",
    "unreachable-pub": "warn",
    "unused-must-use": "deny",
    "rust-2018-idioms": "deny",
    "unnameable-types": "warn",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {
    "all": "warn",
    "missing-const-for-fn": "warn",
    "use-self": "warn",
    "redundant-clone": "warn",
    "result_large_err": "allow",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
