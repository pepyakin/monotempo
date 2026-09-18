# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-eips project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-eips",
    manifest = "//alloy-eips:Cargo.toml",
    lints = "//alloy-eips/bazel:lints",
    edition = "2024",
    version = None,
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
    "option-if-let-else": "warn",
    "redundant-clone": "warn",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
