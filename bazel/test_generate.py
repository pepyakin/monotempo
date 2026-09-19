"""Regression checks for Cargo target kinds used by the monorepo projects."""

import tomllib
import unittest

import generate


class RenderTargetsTest(unittest.TestCase):
    def test_tempo_uses_shared_workspace_and_local_dependencies(self):
        self.assertIn("tempo", {p.name for p in generate.discover_projects()})
        for path in ("tempo/Cargo.lock", "bazel/cargo/Cargo.lock"):
            with self.subTest(lockfile=path):
                lock = tomllib.loads((generate.WORKSPACE_ROOT / path).read_text())
                packages = {p["name"]: p for p in lock["package"]}
                for name in ("reth-node-builder", "reth-primitives-traits", "alloy",
                             "alloy-primitives", "alloy-sol-types", "alloy-evm",
                             "alloy-rlp", "alloy-trie", "alloy-chains", "alloy-hardforks",
                             "alloy-eip2930", "alloy-eip7702", "alloy-eip7928", "revm-inspectors"):
                    self.assertIn(name, packages)
                    self.assertNotIn("source", packages[name])
                external = [
                    p["name"] for p in lock["package"] if p.get("source") and
                    p["name"].startswith(("reth", "alloy", "syn-solidity", "revm-inspectors"))
                ]
                self.assertEqual(external, [])

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
            normal=[generate.Dep("//reth/crates/node/api:reth_node_api", False, None, None)],
        )

    def test_proc_macro_uses_host_rule_and_preserves_dependency_names(self):
        crate = self.crate([generate.Target("proc-macro", "example", "src/lib.rs", [])])
        crate.project = generate.Project.load(generate.WORKSPACE_ROOT / "tempo/bazel/project.toml")
        output = generate.render_build_file(crate, {})
        self.assertIn('load("//tempo/bazel:rust.bzl",', output)
        self.assertIn("crate_proc_macro(\n", output)
        self.assertIn("crate_unit_test(\n", output)
        self.assertIn('"//reth/crates/node/api:reth_node_api"', output)
        self.assertNotIn("crate_library(\n", output)

    def test_cargo_integration_name_is_not_bazel_target_or_source_directory(self):
        crate = self.crate([
            generate.Target("test", "storage", "tests/storage_tests/main.rs", []),
        ])
        output = generate.render_build_file(crate, {}, cargo_test_names=True)
        self.assertIn('name = "example_storage_test"', output)
        self.assertIn('crate_name = "storage"', output)
        self.assertNotIn('crate_name =', generate.render_build_file(crate, {}))
        crate.project = generate.Project.load(generate.WORKSPACE_ROOT / "tempo/bazel/project.toml")
        self.assertIn('crate_name = "storage"', generate.render_build_file(crate, {}))

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

    def test_flaky_is_scoped_to_the_named_test_target(self):
        crate = self.crate([
            generate.Target("lib", "example", "src/lib.rs", []),
            generate.Target("test", "integration", "tests/integration.rs", []),
        ])
        for target in ("crate", "integration", "absent"):
            with self.subTest(target=target):
                crate.project.test_flaky = {"example": {target: True}}
                output = generate.render_build_file(crate, {})
                library, tests = output.split("crate_unit_test(\n")
                unit, integration = tests.split("crate_integration_test(\n")
                self.assertNotIn("flaky =", library)
                self.assertEqual("flaky = True" in unit, target == "crate")
                self.assertEqual("flaky = True" in integration, target == "integration")
        crate.project.test_flaky = {"example": {"crate": False}}
        self.assertNotIn("flaky =", generate.render_build_file(crate, {}))

    def test_ci_exceptions_do_not_disable_other_test_targets(self):
        tempo = generate.Project.load(generate.WORKSPACE_ROOT / "tempo/bazel/project.toml")
        self.assertEqual(tempo.tags_for("tempo", "crate"), ["manual", "requires-network"])
        self.assertEqual(tempo.tags_for("tempo-node", "it"), ["manual"])
        self.assertEqual(tempo.tags_for("tempo-e2e", "crate"), ["manual"])
        self.assertIsNone(tempo.tags_for("tempo-node", "crate"))
        self.assertIsNone(tempo.tags_for("tempo-node", "other"))
        reth = generate.Project.load(generate.WORKSPACE_ROOT / "reth/bazel/project.toml")
        self.assertEqual(reth.test_flaky, {
            "reth-engine-tree": {"crate": True},
            "reth-trie-common": {"crate": True},
        })
        for name in reth.test_flaky:
            self.assertIsNone(reth.tags_for(name, "crate"))

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

    def test_dependency_renames_preserve_public_features(self):
        project = self.crate([]).project
        dep = {
            "name": "const-hex", "rename": "hex", "req": "^1", "source": None,
            "features": [], "optional": True, "uses_default_features": False,
            "kind": None, "target": None,
        }
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                features = {"std": ["hex/std"], "serde": ["hex?/serde"],
                            "hex-compat": ["hex/hex"], "other": ["hex2/std"]}
                if explicit:
                    features["enabled"] = ["dep:hex"]
                else:
                    features["default"] = ["hex"]
                member = generate.Member(project=project, manifest={"features": features}, pkg={
                    "name": "example", "version": "1.0.0", "edition": "2021",
                    "manifest_path": str(project.root / "crates/example/Cargo.toml"),
                    "dependencies": [dep, dict(dep, target='cfg(unix)')], "targets": [],
                })
                derived = generate.derive_manifest(member, {}, {("const-hex", "hex")})
                manifest = tomllib.loads(derived.text)
                expected = {"std": ["const-hex/std"], "serde": ["const-hex?/serde"],
                            "hex-compat": ["const-hex/hex"], "other": ["hex2/std"]}
                if explicit:
                    expected["enabled"] = ["dep:const-hex"]
                else:
                    expected.update({"default": ["hex"], "hex": ["dep:const-hex"]})
                self.assertEqual(manifest["features"], expected)
                self.assertEqual(list(manifest["dependencies"]), ["const-hex"])
                self.assertEqual(features["std"], ["hex/std"])


if __name__ == "__main__":
    unittest.main()
