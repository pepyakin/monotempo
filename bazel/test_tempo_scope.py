"""Graph-generation and audit checks; never execute a Rust compiler."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import tempo_scope


def unit(name, *, host=False, features=(), kind="lib", mode="build", deps=()):
    return dict(pkg_id=name, target=dict(name=name.replace("-", "_"), kind=[kind]),
                platform=None if host else tempo_scope.TRIPLE, features=list(features), mode=mode,
                dependencies=[dict(index=i, extern_crate_name=n) for i, n in deps])


class ScopeTest(unittest.TestCase):
    def test_presets_select_binary_or_library_and_keep_defaults_explicit(self):
        expected = {
            "tempo-node": ["--package", "tempo", "--bin", "tempo"],
            "alloy-consensus": ["--package", "alloy-consensus", "--lib"],
            "tempo-payload-builder": ["--package", "tempo-payload-builder", "--lib", "--no-default-features"],
        }
        for name, args in expected.items():
            preset = tempo_scope.PRESETS[name]
            self.assertEqual(preset.cargo_args(), args)
            self.assertEqual(preset.verbs, ("build",) if name == "tempo-node" else ("build", "test"))
            with patch("tempo_scope.subprocess.check_output", return_value=b'{"version": 1}') as run:
                tempo_scope.cargo_plan("build", {}, Path("/tmp/Cargo.toml"), preset)
                command = run.call_args.args[0]
                self.assertEqual(command[5:-5], args)
                self.assertEqual(command[-5:], ["--target", tempo_scope.TRIPLE, "-Z", "unstable-options", "--unit-graph"])
                self.assertEqual(run.call_args.kwargs["env"], {"RUSTC_BOOTSTRAP": "1"})

    def test_restore_aliases_keeps_versions_and_optional_feature_semantics(self):
        derived = {
            "dependencies": {"const-hex": {"version": "^1.19", "optional": True}},
            "target": {"cfg(unix)": {"build-dependencies": {"const-hex": {"version": "^1.19"}}}},
            "features": {"hex": ["dep:const-hex"], "std": ["const-hex?/std", "other/std"]},
        }
        original = {"dependencies": {"hex": {"workspace": True}},
                    "target": {"cfg( unix )": {"build-dependencies": {"hex": {"workspace": True}}}}}
        workspace = {"hex": {"package": "const-hex", "version": "^1.18"}}
        self.assertTrue(tempo_scope.restore_aliases(derived, original, workspace))
        self.assertEqual(derived["dependencies"], {"hex": {"version": "^1.19", "optional": True, "package": "const-hex"}})
        self.assertEqual(derived["features"], {"hex": ["dep:hex"], "std": ["hex?/std", "other/std"]})
        self.assertEqual(derived["target"]["cfg(unix)"]["build-dependencies"],
                         {"hex": {"version": "^1.19", "package": "const-hex"}})
        self.assertFalse(tempo_scope.restore_aliases(derived, original, workspace))

    def test_dev_alias_does_not_rename_a_normal_dependency_at_another_version(self):
        derived = {
            "dependencies": {"rand": {"version": "^0.9"}},
            "dev-dependencies": {"rand_08": {"package": "rand", "version": "^0.8"}},
            "target": {"cfg(windows)": {"dependencies": {"rand": {"version": "^0.9"}}}},
        }
        original = {"dependencies": {"rand": {"workspace": True}},
                    "dev-dependencies": {"rand_08": {"workspace": True}}}
        before = deepcopy(derived)
        self.assertFalse(tempo_scope.restore_aliases(derived, original, {
            "rand": "0.9", "rand_08": {"package": "rand", "version": "0.8"},
        }))
        self.assertEqual(derived, before)

    def fixture(self, package=tempo_scope.PACKAGE):
        names = [package, "shared", "derive", "native"]
        metadata = dict(packages=[dict(id=n, name=n, version="1.0.0", targets=[]) for n in names])
        lock = dict(crates={n + " 1.0.0": dict(library_target_name=n.replace("-", "_")) for n in names},
                    workspace_members={package + " 1.0.0": "bazel/cargo/tempo/crates/payload/builder"})
        graph = dict(version=1, roots=[0], units=[
            unit(package, deps=[(1, "renamed"), (2, "derive")]),
            unit("shared", features=["rc"]),
            unit("derive", host=True, kind="proc-macro", deps=[(3, "shared")]),
            unit("shared", host=True, features=["derive"]),
        ])
        return tempo_scope.Scope(metadata, lock), graph

    def test_host_target_features_and_renames_do_not_leak(self):
        scope, graph = self.fixture()
        root = scope.add(graph)
        expected = scope.expected[root]
        target = expected["externs"]["renamed"]
        macro = expected["externs"]["derive"]
        host = scope.expected[macro]["externs"]["shared"]
        self.assertNotEqual(target, host)
        self.assertEqual(scope.expected[target]["features"], ["rc"])
        self.assertEqual(scope.expected[host]["features"], ["derive"])
        self.assertFalse(scope.expected[target]["host"])
        self.assertTrue(scope.expected[host]["host"])
        self.assertEqual(expected["proc_macro_deps"], [macro])
        root_attrs = next(iter(scope.variants["@@//tempo/crates/payload/builder:tempo_payload_builder"].values()))["attrs"]
        # An unnecessary proc-macro alias would instantiate it in target config.
        self.assertEqual(root_attrs["aliases"], {target: "renamed"})

    def test_transitive_feature_changes_change_ancestors_not_unrelated_units(self):
        scope, graph = self.fixture()
        first = scope.add(graph)
        first_macro = scope.expected[first]["externs"]["derive"]
        graph["units"][1]["features"] = ["std"]
        second = scope.add(graph)
        self.assertNotEqual(first, second)
        self.assertEqual(scope.expected[second]["externs"]["derive"], first_macro)
        self.assertEqual(scope.add(graph), second)

    def test_renamed_macro_uses_exec_alias_not_target_alias(self):
        scope, graph = self.fixture()
        graph["units"][0]["dependencies"][1]["extern_crate_name"] = "derive_compat"
        root = scope.add(graph)
        macro = scope.expected[root]["externs"]["derive_compat"]
        attrs = next(iter(scope.variants["@@//tempo/crates/payload/builder:tempo_payload_builder"].values()))["attrs"]
        self.assertEqual(attrs["proc_macro_aliases"], {macro: "derive_compat"})
        self.assertNotIn(macro, attrs["aliases"])
        self.assertTrue(scope.expected[macro]["host"])

    def test_build_script_uses_build_deps_and_native_link_owner(self):
        scope, graph = self.fixture()
        graph["units"] += [
            unit(tempo_scope.PACKAGE, kind="custom-build", mode="run-custom-build", deps=[(5, "build_script_build"), (7, "native")]),
            unit(tempo_scope.PACKAGE, host=True, kind="custom-build", deps=[(3, "shared")]),
            unit("native", deps=[(7, "build_script_build")]),
            unit("native", kind="custom-build", mode="run-custom-build", deps=[(8, "build_script_build")]),
            unit("native", host=True, kind="custom-build"),
        ]
        graph["units"][0]["dependencies"] += [dict(index=4, extern_crate_name="build_script_build")]
        root = scope.add(graph)
        script = next(d for d in scope.expected[root]["deps"] if "build_script__scope_" in d)
        self.assertTrue(scope.expected[script + "_"]["host"])
        attrs = next(iter(scope.variants["@@//tempo/crates/payload/builder:tempo_payload_builder_build_script"].values()))["attrs"]
        self.assertEqual(attrs["link_deps"][0].split(":")[-1].split("__scope_")[0], "native")
        self.assertEqual(set(scope.expected[script + "_"]["externs"]), {"shared"})

    def test_test_scope_is_distinct_from_library_scope(self):
        scope, graph = self.fixture()
        library = scope.add(graph)
        graph["units"][0]["mode"] = "test"
        graph["units"][0]["dependencies"].append(dict(index=4, extern_crate_name="native"))
        graph["units"].append(unit("native"))
        test = scope.add(graph)
        self.assertNotIn("native", scope.expected[library]["externs"])
        self.assertIn("native", scope.expected[test]["externs"])
        self.assertEqual(scope.expected[test]["kind"], "rust_test")

    def test_binary_uses_binary_template_and_keeps_same_package_library(self):
        scope, graph = self.fixture("cli")
        binary = unit("cli", kind="bin", deps=[(0, "cli")])
        scope.packages["cli"]["targets"] = [graph["units"][0]["target"], binary["target"]]
        graph["units"].append(binary)
        graph["roots"] = [4]
        root = scope.add(graph)
        self.assertIn(":cli__scope_", root)
        self.assertEqual(scope.expected[root]["kind"], "rust_binary")
        library = scope.expected[root]["externs"]["cli"]
        self.assertIn(":cli_lib__scope_", library)
        self.assertEqual(scope.expected[library]["kind"], "rust_library")
        graph["units"][4]["mode"] = "test"
        with self.assertRaisesRegex(ValueError, "library unit tests"):
            scope.add(graph)

    def test_other_project_tests_share_identical_dependency_units(self):
        scope, graph = self.fixture()
        first = scope.add(graph)
        scope.packages["alloy-consensus"] = dict(id="alloy-consensus", name="alloy-consensus", version="1.0.0", targets=[])
        scope.lock["crates"]["alloy-consensus 1.0.0"] = dict(library_target_name="alloy_consensus")
        scope.lock["workspace_members"]["alloy-consensus 1.0.0"] = "bazel/cargo/alloy/crates/consensus"
        graph["units"][0] = unit("alloy-consensus", mode="test", deps=[(1, "renamed"), (2, "derive")])
        second = scope.add(graph)
        self.assertIn("//alloy/crates/consensus:alloy_consensus_test__scope_", second)
        self.assertEqual(scope.expected[second]["kind"], "rust_test")
        self.assertEqual(scope.expected[first]["externs"], scope.expected[second]["externs"])

    def test_audit_rejects_extra_features_wrong_edges_host_and_global_escape(self):
        scope = dict(expected={
            "@@//pkg:root__scope_a": dict(features=["std"], host=False, externs={"dep": "@@//pkg:dep__scope_b"}),
            "@@//pkg:dep__scope_b": dict(features=[], host=True, externs={}),
        })
        actions = dict(
            targets=[dict(id=1, label="//pkg:root__scope_a"), dict(id=2, label="//pkg:dep__scope_b")],
            configuration=[dict(id=1), dict(id=2, isTool=True)],
            pathFragments=[dict(id=1, label="dep.rlib")], artifacts=[dict(id=1, pathFragmentId=1)],
            actions=[
                dict(targetId=1, configurationId=1, mnemonic="Rustc", arguments=['--cfg=feature="std"', '--extern=dep=dep.rlib']),
                dict(targetId=2, configurationId=2, mnemonic="Rustc", arguments=[], outputIds=[1]),
            ],
        )
        self.assertIn("Verified 2", tempo_scope.audit(actions, scope))
        for change, message in (
            (lambda a: a["actions"][0]["arguments"].append('--cfg=feature="extra"'), "Feature mismatch"),
            (lambda a: a["actions"][0]["arguments"].pop(), "Dependency-edge mismatch"),
            (lambda a: a["actions"][1].update(configurationId=1), "Host/target mismatch"),
            (lambda a: a["targets"][1].update(label="//pkg:global"), "mismatch|Unscoped"),
            (lambda a: a.update(actions=[]), "Missing scoped"),
        ):
            with self.subTest(message=message):
                bad = deepcopy(actions)
                change(bad)
                with self.assertRaisesRegex(ValueError, message):
                    tempo_scope.audit(bad, scope)


if __name__ == "__main__":
    unittest.main()
