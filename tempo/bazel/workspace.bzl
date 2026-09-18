# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the tempo project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "tempo",
    manifest = "//tempo:Cargo.toml",
    lints = "//tempo/bazel:lints",
    edition = "2024",
    version = "1.14.0",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
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
    "cast_lossless": "deny",
    "default_constructed_unit_structs": "allow",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
