# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy",
    manifest = "//alloy:Cargo.toml",
    lints = "//alloy/bazel:lints",
    edition = "2021",
    version = "2.4.2",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "rust_2018_idioms": "deny",
    "missing_debug_implementations": "warn",
    "missing_docs": "warn",
    "unreachable_pub": "warn",
    "unused_must_use": "deny",
    "unnameable_types": "warn",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {
    "all": "warn",
    "missing_const_for_fn": "warn",
    "use_self": "warn",
    "redundant_clone": "warn",
    "large_enum_variant": "allow",
    "result_large_err": "allow",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
