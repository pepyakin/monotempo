# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values of the alloy-chains project, derived from its root Cargo.toml."""

# What every crate of the project inherits; see //bazel:rust.bzl.
WORKSPACE = struct(
    name = "alloy-chains",
    manifest = None,
    lints = "//alloy-chains/bazel:lints",
    edition = "2024",
    version = "0.2.37",
)

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
}

CLIPPY_LINTS = {}

RUSTDOC_LINTS = {}
