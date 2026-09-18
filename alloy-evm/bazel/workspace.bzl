# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-evm project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-evm",
    manifest = "//alloy-evm:Cargo.toml",
    lints = "//alloy-evm/bazel:lints",
    edition = "2021",
    version = "0.39.0",
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
    "option-if-let-else": "allow",
    "redundant-clone": "warn",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
