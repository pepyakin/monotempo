"""Regression checks for Cargo target kinds used by the monorepo projects."""

import tomllib
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

    def test_independent_package_version_reaches_every_target(self):
        crate = self.crate([
            generate.Target("lib", "example", "src/lib.rs", []),
            generate.Target("bin", "example", "src/main.rs", []),
            generate.Target("custom-build", "build-script-build", "build.rs", []),
            generate.Target("test", "integration", "tests/integration.rs", []),
        ])
        crate.version = "0.6.3"
        output = generate.render_build_file(crate, {})
        self.assertEqual(output.count('version = "0.6.3"'), 5)
        workspace = generate.render_workspace_bzl(crate.project, {
            "workspace": {"package": {"edition": "2024"}},
        })
        self.assertIn("version = None", workspace)

    def test_cross_package_fixtures_are_public_and_available_at_runtime(self):
        crate = self.crate([generate.Target("lib", "example", "src/lib.rs", [])])
        crate.project.filegroups = {"example": {"fixtures": ["tests/abi/**"]}}
        crate.project.compile_data = {"example": {"crate": ["//other:fixtures"]}}
        output = generate.render_build_file(crate, {})
        self.assertIn('name = "fixtures"', output)
        self.assertIn('"tests/abi/**"', output)
        self.assertIn('visibility = ["//visibility:public"]', output)
        library, unit_test = output.split("crate_unit_test(\n")
        self.assertIn('compile_data = [\n        "//other:fixtures",\n    ]', library)
        self.assertIn('data = [\n        "//other:fixtures",\n    ]', unit_test)

    def test_runtime_environment_only_reaches_tests(self):
        crate = self.crate([
            generate.Target("lib", "example", "src/lib.rs", []),
            generate.Target("test", "integration", "tests/integration.rs", []),
        ])
        crate.project.test_env = {"example": {"CARGO_PKG_NAME": "example"}}
        output = generate.render_build_file(crate, {})
        library, tests = output.split("crate_unit_test(\n")
        self.assertNotIn('"CARGO_PKG_NAME"', library)
        self.assertEqual(tests.count('"CARGO_PKG_NAME": "example"'), 2)

    def test_target_outside_package_stays_inside_derived_package(self):
        project = self.crate([]).project
        package_dir = project.root / "crates/example"
        member = generate.Member(project=project, manifest={}, pkg={
            "name": "example", "version": "0.3.16", "edition": "2021",
            "manifest_path": str(package_dir / "Cargo.toml"),
            "dependencies": [],
            "targets": [
                {"name": "example", "kind": ["lib"], "crate_types": ["lib"],
                 "src_path": str(package_dir / "src/lib.rs")},
                {"name": "enum", "kind": ["example"], "crate_types": ["bin"],
                 "src_path": str(package_dir / "../../examples/enum.rs")},
            ],
        })
        derived = generate.derive_manifest(member, {}, set())
        manifest = tomllib.loads(derived.text)
        self.assertEqual(manifest["lib"]["path"], "src/lib.rs")
        self.assertEqual(manifest["example"][0]["path"], "__workspace__/examples/enum.rs")
        self.assertEqual(derived.children, {
            "src": package_dir / "src",
            "__workspace__/examples/enum.rs": project.root / "examples/enum.rs",
        })


if __name__ == "__main__":
    unittest.main()
