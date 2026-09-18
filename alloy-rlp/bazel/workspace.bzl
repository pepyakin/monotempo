# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-rlp project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-rlp",
    manifest = "//alloy-rlp:Cargo.toml",
    lints = "//alloy-rlp/bazel:lints",
    edition = "2021",
    version = "0.3.16",
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
