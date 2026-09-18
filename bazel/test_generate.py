"""Regression checks for Cargo target kinds used by the monorepo projects."""

import unittest
from unittest.mock import patch

import generate


class RenderTargetsTest(unittest.TestCase):
    def crate(self, targets, features=()):
        return generate.Crate(
            project=generate.Project(
                root=generate.WORKSPACE_ROOT / "tempo",
                disabled_features={}, build_scripts={}, compile_data={},
                exported_files={}, process_per_test=[], test_tags={},
            ),
            name="example",
            ident="example",
            package_path="crates/example",
            features=list(features),
            declared_features=["localnet"],
            workspace_lints=True,
            targets=targets,
            label="//tempo/crates/example:example",
            normal=[generate.Dep("@tempo_crates//:reth-node-api", False, None, None)],
        )

    def test_proc_macro_uses_host_rule_and_preserves_dependency_names(self):
        crate = self.crate([generate.Target("proc-macro", "example", "src/lib.rs", [])])
        with patch.object(generate, "RUST_BZL", "//tempo/bazel:rust.bzl"):
            output = generate.render_build_file(crate, {})
        self.assertIn('load("//tempo/bazel:rust.bzl",', output)
        self.assertIn("crate_proc_macro(\n", output)
        self.assertIn("crate_unit_test(\n", output)
        self.assertIn('"@tempo_crates//:reth-node-api"', output)
        self.assertNotIn("crate_library(\n", output)

    def test_cargo_integration_name_is_not_bazel_target_or_source_directory(self):
        crate = self.crate([
            generate.Target("test", "storage", "tests/storage_tests/main.rs", []),
        ])
        output = generate.render_build_file(crate, {}, cargo_test_names=True)
        self.assertIn('name = "example_storage_test"', output)
        self.assertIn('crate_name = "storage"', output)
        self.assertNotIn('crate_name =', generate.render_build_file(crate, {}))

    def test_required_features_gate_binary_and_integration_test_runfiles(self):
        targets = [
            generate.Target("lib", "example", "src/lib.rs", []),
            generate.Target("bin", "example", "src/main.rs", []),
            generate.Target("bin", "localnet", "src/localnet.rs", ["localnet"]),
            generate.Target("test", "cli", "tests/cli.rs", []),
        ]
        for features, expected in [((), False), (("localnet",), True)]:
            with self.subTest(features=features):
                output = generate.render_build_file(self.crate(targets, features), {})
                self.assertIn('name = "example_lib"', output)
                self.assertIn('name = "example"', output)
                self.assertEqual('name = "localnet"' in output, expected)
                self.assertEqual('":localnet"' in output, expected)


if __name__ == "__main__":
    unittest.main()
