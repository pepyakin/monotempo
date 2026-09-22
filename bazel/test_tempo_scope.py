"""Graph-generation and audit checks; never execute a Rust compiler."""

from copy import deepcopy
import unittest

import tempo_scope


def unit(name, *, host=False, features=(), kind="lib", mode="build", deps=()):
    return dict(pkg_id=name, target=dict(name=name.replace("-", "_"), kind=[kind]),
                platform=None if host else tempo_scope.TRIPLE, features=list(features), mode=mode,
                dependencies=[dict(index=i, extern_crate_name=n) for i, n in deps])


class ScopeTest(unittest.TestCase):
    def test_restore_aliases_keeps_versions_and_optional_feature_semantics(self):
        derived = {
            "dependencies": {"const-hex": {"version": "^1.19", "optional": True}},
            "target": {"cfg(unix)": {"build-dependencies": {"const-hex": {"version": "^1.19"}}}},
            "features": {"hex": ["dep:const-hex"], "std": ["const-hex?/std", "other/std"]},
        }
        original = {"dependencies": {"hex": {"workspace": True}}}
        workspace = {"hex": {"package": "const-hex", "version": "^1.18"}}
        self.assertTrue(tempo_scope.restore_aliases(derived, original, workspace))
        self.assertEqual(derived["dependencies"], {"hex": {"version": "^1.19", "optional": True, "package": "const-hex"}})
        self.assertEqual(derived["features"], {"hex": ["dep:hex"], "std": ["hex?/std", "other/std"]})
        self.assertEqual(derived["target"]["cfg(unix)"]["build-dependencies"],
                         {"hex": {"version": "^1.19", "package": "const-hex"}})
        self.assertFalse(tempo_scope.restore_aliases(derived, original, workspace))

    def fixture(self):
        names = [tempo_scope.PACKAGE, "shared", "derive", "native"]
        metadata = dict(packages=[dict(id=n, name=n, version="1.0.0") for n in names])
        lock = dict(crates={n + " 1.0.0": dict(library_target_name=n.replace("-", "_")) for n in names},
                    workspace_members={tempo_scope.PACKAGE + " 1.0.0": "bazel/cargo/tempo/crates/payload/builder"})
        graph = dict(version=1, roots=[0], units=[
            unit(tempo_scope.PACKAGE, deps=[(1, "renamed"), (2, "derive")]),
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
