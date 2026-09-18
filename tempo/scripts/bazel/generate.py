#!/usr/bin/env python3
"""Generate Tempo's Bazel targets from its unchanged Cargo workspace.

Run from the monorepo root, with --check to verify instead of writing.
The metadata resolver and BUILD renderer are shared with reth; only project
configuration and the crate_universe manifest list live here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(WORKSPACE / "reth/scripts/bazel"))
import generate as renderer


def configure() -> None:
    renderer.REPO_ROOT = ROOT
    renderer.PREFIX = "tempo"
    renderer.CRATES_REPO = "@tempo_crates"
    renderer.RULE_PREFIX = "tempo"
    renderer.GENERATOR = "tempo/scripts/bazel/generate.py"
    renderer.HEADER = (
        "# GENERATED FILE - DO NOT EDIT.\n#\n"
        f"# Regenerate with `python3 {renderer.GENERATOR}` after changing Cargo.toml.\n"
    )
    renderer.BUILD_SCRIPT_DATA = {}
    renderer.BUILD_SCRIPT_ENV = {"tempo-node": {"VERGEN_IDEMPOTENT": "1"}}
    renderer.EXPORTED_FILES = {
        "tempo-node": ["tests/assets/test-genesis.json"],
        "tempo-chainspec": ["src/genesis/dev.json", "src/genesis/moderato.json", "src/genesis/presto.json"],
        "tempo-nitro-attestation": ["testdata/aws_attestation_2026_01_03.b64"],
    }
    renderer.EXTRA_COMPILE_DATA = {
        "tempo-xtask": {"crate": [
            '"//tempo/crates/chainspec:src/genesis/dev.json"',
            '"//tempo/crates/chainspec:src/genesis/moderato.json"',
            '"//tempo/crates/chainspec:src/genesis/presto.json"',
            '"//tempo:.github/workflows/bench.yml"',
        ]},
        "tempo-e2e": {"crate": ['"//tempo/crates/node:tests/assets/test-genesis.json"']},
        "tempo-node": {"it": [
            '"//tempo/crates/chainspec:src/genesis/moderato.json"',
            '"//tempo/crates/chainspec:src/genesis/presto.json"',
        ]},
        "tempo-precompiles": {"crate": [
            '"//tempo/crates/nitro-attestation:testdata/aws_attestation_2026_01_03.b64"',
        ]},
    }
    renderer.E2E_TEST_UTILS = "tempo-e2e"
    renderer.E2E_TEST_UTILS_LABEL = "//tempo/crates/e2e:tempo_e2e"
    renderer.TEST_TAGS = {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    configure()
    root = renderer.load_root_manifest()
    result = subprocess.run(
        ["cargo", "metadata", "--locked", "--format-version", "1"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    metadata = json.loads(result.stdout)
    crates = renderer.build_crates(metadata)
    packages = {p["id"]: p for p in metadata["packages"]}
    versions = sorted(
        (f"tempo/{crate.package_path}", packages[id]["version"])
        for id, crate in crates.items()
    )
    outputs = {ROOT / "bazel/workspace.bzl": (
        renderer.render_workspace_bzl(root)
        + "\n# Published crates may have versions independent of the node.\n"
        + "PACKAGE_VERSIONS = " + renderer.render_dict(versions) + "\n"
    )}
    for crate in crates.values():
        # Every member manifest is an explicit crate_universe input, so editing
        # a member dependency invalidates the lock even if the root is unchanged.
        outputs[ROOT / crate.package_path / "BUILD.bazel"] = (
            renderer.render_build_file(crate, cargo_test_names=True)
            + '\nexports_files(["Cargo.toml"])\n'
        )
    outputs[ROOT / "BUILD.bazel"] = renderer.HEADER + '''
package(default_visibility = ["//visibility:public"])

exports_files(["Cargo.toml", "Cargo.lock", "Cargo.Bazel.lock", ".cargo/config.toml", ".github/workflows/bench.yml"])

alias(
    name = "tempo",
    actual = "//tempo/bin/tempo:tempo",
)
'''
    outputs[ROOT / "bazel/BUILD.bazel"] = renderer.HEADER + '''
load("@rules_rust//rust:defs.bzl", "rust_lint_config")
load(":workspace.bzl", "CLIPPY_LINTS", "RUSTC_CHECK_CFG", "RUSTC_LINTS", "RUSTDOC_LINTS")

package(default_visibility = ["//visibility:public"])

rust_lint_config(
    name = "lints",
    clippy = CLIPPY_LINTS,
    rustc = RUSTC_LINTS,
    rustc_check_cfg = RUSTC_CHECK_CFG,
    rustdoc = RUSTDOC_LINTS,
)
'''
    manifests = ["//tempo:Cargo.toml"] + sorted(
        f"//tempo/{crate.package_path}:Cargo.toml" for crate in crates.values()
    )
    outputs[ROOT / "bazel/cargo.MODULE.bazel"] = renderer.HEADER + '''
tempo_crate = use_extension("@rules_rust//crate_universe:extensions.bzl", "crate")
tempo_crate.from_cargo(
    name = "tempo_crates",
    cargo_config = "//tempo:.cargo/config.toml",
    cargo_lockfile = "//tempo:Cargo.lock",
    lockfile = "//tempo:Cargo.Bazel.lock",
    manifests = ''' + renderer.render_list([json.dumps(m) for m in manifests]) + ''',
    supported_platform_triples = [
        "aarch64-apple-darwin",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "x86_64-unknown-linux-gnu",
    ],
)
use_repo(tempo_crate, "tempo_crates")
'''
    stale = renderer.write_outputs(outputs, args.check)
    if args.check and stale:
        return renderer.report_stale(stale)
    if not args.check:
        print(f"{len(crates)} crates, {len(stale)} file(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
