# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 tempo/scripts/bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values derived from the root Cargo.toml."""

WORKSPACE_VERSION = "1.14.0"

RUST_EDITION = "2024"

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

# Published crates may have versions independent of the node.
PACKAGE_VERSIONS = {
    "tempo/bin/tempo": "1.14.0",
    "tempo/bin/tempo-sidecar": "1.14.0",
    "tempo/crates/alloy": "1.11.0",
    "tempo/crates/chainspec": "1.11.0",
    "tempo/crates/consensus": "1.14.0",
    "tempo/crates/consensus-config": "1.14.0",
    "tempo/crates/contracts": "1.11.0",
    "tempo/crates/dkg-onchain-artifacts": "1.14.0",
    "tempo/crates/e2e": "1.14.0",
    "tempo/crates/evm": "1.14.0",
    "tempo/crates/ext": "1.14.0",
    "tempo/crates/eyre": "1.14.0",
    "tempo/crates/faucet": "1.14.0",
    "tempo/crates/hardfork": "1.11.0",
    "tempo/crates/nitro-attestation": "1.14.0",
    "tempo/crates/node": "1.14.0",
    "tempo/crates/payload/builder": "1.14.0",
    "tempo/crates/payload/types": "1.14.0",
    "tempo/crates/precompiles": "1.14.0",
    "tempo/crates/precompiles-macros": "1.14.0",
    "tempo/crates/primitives": "1.11.0",
    "tempo/crates/revm": "1.14.0",
    "tempo/crates/telemetry-util": "1.14.0",
    "tempo/crates/transaction-pool": "1.14.0",
    "tempo/crates/validator-config": "1.14.0",
    "tempo/xtask": "1.14.0",
}
