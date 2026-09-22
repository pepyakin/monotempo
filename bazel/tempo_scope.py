"""Experimental Linux compilation units, using the existing source graph.

No Rust compilation happens here. Cargo's pinned, unstable --unit-graph supplies
root-scoped features and edges. A private rules_rust override adds target
variants in the same packages/repositories as the ordinary generated rules.
Neither tracked BUILD files nor the shared lockfile are rewritten.
"""

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import generate


ROOT = Path(__file__).resolve().parent.parent
TRIPLE = "x86_64-unknown-linux-gnu"
PACKAGE = "tempo-payload-builder"
MANIFEST = ROOT / "bazel/cargo/Cargo.toml"


@dataclass(frozen=True)
class Preset:
    path: str
    package: str
    binary: str | None = None
    default_features: bool = True

    def cargo_args(self):
        target = ["--bin", self.binary] if self.binary else ["--lib"]
        return ["--package", self.package, *target, *self.feature_args()]

    def feature_args(self):
        return [] if self.default_features else ["--no-default-features"]

    @property
    def verbs(self):
        return ("build",) if self.binary else ("build", "test")


PRESETS = {
    "tempo-payload-builder": Preset("tempo/crates/payload/builder", PACKAGE, default_features=False),
    "tempo-node": Preset("tempo/bin/tempo", "tempo", binary="tempo"),
    "alloy-consensus": Preset("alloy/crates/consensus", "alloy-consensus"),
}


def cargo_plan(verb, env, manifest, preset):
    command = [
        "cargo", verb, "--manifest-path", str(manifest), "--locked",
        *preset.cargo_args(),
        "--target", TRIPLE, "-Z", "unstable-options", "--unit-graph",
    ]
    # Enables Cargo's inspection API only; this environment is never used to
    # compile the benchmark. The toolchain is pinned by rust-toolchain.toml.
    return json.loads(subprocess.check_output(command, env=dict(env, RUSTC_BOOTSTRAP="1"), cwd=ROOT))


def dependency_tables(manifest):
    for platform, table in [("", manifest), *manifest.get("target", {}).items()]:
        for kind in ("dependencies", "dev-dependencies", "build-dependencies"):
            if kind in table:
                yield (re.sub(r"\s+", "", platform), kind), table[kind]


def restore_aliases(derived, original, workspace):
    """Undo only generator.colliding_renames, retaining versions and features."""
    renames = {}
    destinations = dict(dependency_tables(derived))
    for section, table in dependency_tables(original):
        dest = destinations.get(section, {})
        for alias, spec in table.items():
            if isinstance(spec, dict) and spec.get("workspace"):
                spec = workspace[alias]
            if isinstance(spec, dict) and spec.get("package", alias) != alias:
                package = spec["package"]
                if package in dest and alias not in dest:
                    dest[alias] = dict(dest.pop(package), package=package)
                    renames[package] = alias
    for name, entries in derived.get("features", {}).items():
        for package, alias in renames.items():
            entries = [f"dep:{alias}" if e == f"dep:{package}" else
                       alias + e[len(package):] if e.startswith((package + "/", package + "?/")) else e
                       for e in entries]
        derived["features"][name] = entries
    return bool(renames)


def cargo_workspace(destination, lock):
    """Relocate derived manifests, not sources; retain the exact shared lockfile.

    The normal derived manifests erase some extern renames solely to avoid
    crate-universe hub-alias collisions. Cargo needs those source-level names.
    """
    shutil.copytree(MANIFEST.parent, destination, symlinks=True)
    for source in MANIFEST.parent.rglob("*"):
        if source.is_symlink():
            target = destination / source.relative_to(MANIFEST.parent)
            target.unlink()
            target.symlink_to(source.resolve())
    for member in lock["workspace_members"].values():
        relative = Path(member).relative_to("bazel/cargo")
        path = destination / relative / "Cargo.toml"
        # Metadata derivation links targets only. Real Cargo build scripts also
        # need siblings such as reth-mdbx-sys/libmdbx and package README files.
        for source in (ROOT / relative).iterdir():
            target = path.parent / source.name
            if source.name not in ("target", ".git", "Cargo.lock") and not target.exists():
                target.symlink_to(source)
        derived = tomllib.loads(path.read_text())
        original = tomllib.loads((ROOT / relative / "Cargo.toml").read_text())
        project = tomllib.loads((ROOT / relative.parts[0] / "Cargo.toml").read_text())
        changed = restore_aliases(derived, original, project.get("workspace", {}).get("dependencies", {}))
        expected_names = {k: set(v) for k, v in dependency_tables(original) if v}
        actual_names = {k: set(v) for k, v in dependency_tables(derived) if v}
        if actual_names != expected_names:
            raise ValueError(f"Source dependency names changed in {relative}: {actual_names} != {expected_names}")
        if changed:
            # Arrays of tables can be emitted as top-level arrays of inline
            # tables. Emit scalars first so they do not fall inside a section.
            content = "".join(f"{generate.toml_key(k)} = {generate.toml_value(v)}\n"
                              for k, v in derived.items() if not isinstance(v, dict))
            content += "".join(generate.toml_table(generate.toml_key(k), v)
                               for k, v in derived.items() if isinstance(v, dict))
            path.write_text(content)
    shutil.copyfile(ROOT / "rust-toolchain.toml", destination / "rust-toolchain.toml")
    return destination / "Cargo.toml"


class Scope:
    def __init__(self, metadata, lock):
        self.packages = {p["id"]: p for p in metadata["packages"]}
        self.lock = lock
        self.variants = defaultdict(dict)
        self.expected = {}

    def template(self, unit):
        pkg = self.packages[unit["pkg_id"]]
        key = f"{pkg['name']} {pkg['version']}"
        crate = self.lock["crates"][key]
        local = key in self.lock["workspace_members"]
        if local:
            path = self.lock["workspace_members"][key].removeprefix("bazel/cargo/")
            prefix = f"@@//{path}:"
        else:
            repo = f"{pkg['name']}-{pkg['version']}".replace("+", "-")
            prefix = f"@@rules_rust++crate+crates__{repo}//:"
        if unit["mode"] == "run-custom-build":
            name = pkg["name"].replace("-", "_") + "_build_script" if local else "_bs"
            kind = "cargo_build_script"
        elif unit["mode"] == "test":
            if not local or unit["target"]["kind"] != ["lib"]:
                raise ValueError("Only workspace library unit tests are supported")
            name = generate.crate_name_to_ident(pkg["name"]) + "_test"
            kind = "rust_test"
        elif unit["target"]["kind"] == ["bin"]:
            if not local:
                raise ValueError("Only workspace binaries are supported")
            name = unit["target"]["name"]
            kind = "rust_binary"
        else:
            name = generate.crate_name_to_ident(pkg["name"]) if local else crate["library_target_name"]
            # Match generate.Crate.lib_target: a same-name binary owns the
            # short label, while its library keeps the Rust name at <name>_lib.
            if local and any(t["kind"] == ["bin"] and t["name"] == name for t in pkg["targets"]):
                name += "_lib"
            kind = "rust_proc_macro" if "proc-macro" in unit["target"]["kind"] else "rust_library"
        return prefix + name, kind, pkg

    def add(self, graph):
        if graph["version"] != 1 or len(graph["roots"]) != 1:
            raise ValueError("Expected one Cargo unit-graph v1 root")
        units = graph["units"]
        labels = {}
        active = set()

        def visit(index):
            if index in labels:
                return labels[index]
            if index in active:
                raise ValueError("Compilation-unit cycle")
            active.add(index)
            unit = units[index]
            if unit["platform"] not in (None, TRIPLE):
                raise ValueError("The prototype only supports Linux x86_64")
            if unit["target"]["kind"] == ["custom-build"] and unit["mode"] != "run-custom-build":
                raise ValueError("Build-script compilation must be reached through its run unit")
            template, kind, pkg = self.template(unit)
            attrs = {"crate_features": unit["features"], "deps": [], "proc_macro_deps": [], "aliases": {}}
            externs = {}
            dependencies = unit["dependencies"]
            if kind == "cargo_build_script":
                compiled = [d for d in dependencies if units[d["index"]]["mode"] == "build"
                            and units[d["index"]]["pkg_id"] == unit["pkg_id"]]
                if len(compiled) != 1:
                    raise ValueError("Expected one build-script executable")
                script = units[compiled[0]["index"]]
                if script["features"] != unit["features"]:
                    raise ValueError("Different build-script compile/run features are unsupported")
                attrs["link_deps"] = []
                for dep in dependencies:
                    if dep in compiled:
                        continue
                    # Cargo's run unit depends on other build-script runs for
                    # DEP_<LINKS>_*. rules_rust takes their owning libraries.
                    owners = [i for i, u in enumerate(units)
                              if u["pkg_id"] == units[dep["index"]]["pkg_id"]
                              and u["platform"] == units[dep["index"]]["platform"]
                              and "lib" in u["target"]["kind"]
                              and any(d["index"] == dep["index"] for d in u["dependencies"])]
                    if len(owners) != 1:
                        raise ValueError("Ambiguous native-library build-script owner")
                    attrs["link_deps"].append(visit(owners[0]))
                dependencies = script["dependencies"]
                attrs["pkg_name"] = pkg["name"]
            for dep in dependencies:
                other = units[dep["index"]]
                label = visit(dep["index"])
                attr = "proc_macro_deps" if "proc-macro" in other["target"]["kind"] else "deps"
                attrs[attr].append(label)
                if other["mode"] != "run-custom-build":
                    externs[dep["extern_crate_name"]] = label
                if other["mode"] != "run-custom-build" and dep["extern_crate_name"] != other["target"]["name"]:
                    aliases = "proc_macro_aliases" if attr == "proc_macro_deps" else "aliases"
                    attrs.setdefault(aliases, {})[label] = dep["extern_crate_name"]
            attrs["crate_name"] = unit["target"]["name"].replace("-", "_")
            identity = dict(template=template, kind=kind, host=unit["platform"] is None, attrs=attrs)
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
            name = template.rsplit(":", 1)[1] + "__scope_" + digest
            label = template.rsplit(":", 1)[0] + ":" + name
            attrs["name"] = name
            if kind == "rust_binary":
                # Keep argv[0]/CLI help unchanged without colliding with the
                # ordinary binary or other scoped variants in this package.
                attrs["binary_name"] = name + "/" + unit["target"]["name"]
            self.variants[template][label] = dict(kind=kind, attrs=attrs)
            # cargo_build_script produces a host rust_binary named <name>_.
            compiler_label = label + "_" if kind == "cargo_build_script" else label
            self.expected[compiler_label] = dict(
                package=f"{pkg['name']} {pkg['version']}", features=unit["features"],
                host=kind == "cargo_build_script" or unit["platform"] is None,
                kind=kind, deps=attrs["deps"], proc_macro_deps=attrs["proc_macro_deps"],
                externs=externs,
            )
            labels[index] = label
            active.remove(index)
            return label

        return visit(graph["roots"][0])


def patch_macro_aliases(override):
    """Keep renamed proc macros in exec config, only in the private override.

    rules_rust 0.74's aliases attr is target-configured. collect_deps already
    understands Target-keyed aliases; supply macro Targets through an exec attr.
    """
    replacements = [
        ("rust/private/rust.bzl", '_COMMON_ATTRS = {',
         '_COMMON_ATTRS = {\n    "proc_macro_aliases": attr.label_keyed_string_dict(cfg = "exec"),', 1),
        ("rust/private/rust.bzl", "ctx.attr.aliases",
         "dict(ctx.attr.aliases.items() + ctx.attr.proc_macro_aliases.items())", 4),
        # Build-script binaries are already generated with their scoped attrs.
        # Bypass our public binary wrapper: Starlark forbids recursive emit().
        ("cargo/private/cargo_build_script_wrapper.bzl", 'load("//rust:defs.bzl", "rust_binary")',
         'load("//rust/private:rust.bzl", "rust_binary")', 1),
        ("cargo/private/cargo_build_script_wrapper.bzl", "        proc_macro_deps = [],",
         "        proc_macro_deps = [],\n        proc_macro_aliases = {},", 1),
        ("cargo/private/cargo_build_script_wrapper.bzl", "        proc_macro_deps = proc_macro_deps,",
         "        proc_macro_deps = proc_macro_deps,\n        proc_macro_aliases = proc_macro_aliases,", 1),
    ]
    for relative, old, new, count in replacements:
        path = override / relative
        content = path.read_text()
        if content.count(old) != count:
            raise ValueError(f"Unsupported rules_rust macro-alias layout: {relative}: {old}")
        path.write_text(content.replace(old, new))


def prepare(rules_root, destination, env, preset_name="tempo-payload-builder"):
    destination.mkdir(parents=True, exist_ok=False)
    lock = json.loads((ROOT / "Cargo.Bazel.lock").read_text())
    manifest = cargo_workspace(destination / "cargo", lock)
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--locked", "--format-version=1", "--manifest-path", str(manifest)],
        env=env, cwd=ROOT,
    ))
    scope = Scope(metadata, lock)
    preset = PRESETS[preset_name]
    roots = {}
    for verb in preset.verbs:
        graph = cargo_plan(verb, env, manifest, preset)
        (destination / f"cargo-{verb}-units.json").write_text(json.dumps(graph, indent=2) + "\n")
        roots[verb] = scope.add(graph)
    override = destination / "rules_rust"
    shutil.copytree(rules_root, override, symlinks=True)
    patch_macro_aliases(override)
    private = override / "rust/private"
    shutil.copyfile(ROOT / "bazel/tempo_scope.bzl", private / "tempo_scope.bzl")
    # JSON's strings/lists/dicts are Starlark-compatible; the table has no
    # boolean/null literals. Do not serialize the audit-only host flags here.
    table = {key: list(value.values()) for key, value in sorted(scope.variants.items())}
    (private / "tempo_scope_data.bzl").write_text("UNITS = " + json.dumps(table, indent=2, sort_keys=True) + "\n")
    for relative, kinds in (
        ("rust/defs.bzl", ("rust_library", "rust_proc_macro", "rust_test", "rust_binary")),
        ("cargo/defs.bzl", ("cargo_build_script",)),
    ):
        path = override / relative
        content = path.read_text()
        content = content.replace('load(', 'load("//rust/private:tempo_scope.bzl", _scope_emit = "emit")\n\nload(', 1)
        for kind in kinds:
            original = f"{kind} = _{kind}"
            if content.count(original) != 1:
                raise ValueError(f"Unsupported rules_rust wrapper: {kind}")
            content = content.replace(original, f'def {kind}(**kwargs):\n    _scope_emit(_{kind}, "{kind}", kwargs)')
        path.write_text(content)
    if (manifest.parent / "Cargo.lock").read_bytes() != (MANIFEST.parent / "Cargo.lock").read_bytes():
        raise ValueError("Scoped Cargo changed the shared lockfile")
    result = dict(preset=preset_name, roots=roots, expected=scope.expected,
                  override=str(override), cargo_workspace=str(manifest.parent))
    (destination / "scope.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def audit(actions, scope):
    """Check actual compiler features, extern edges, and execution contexts.

    Reject any escape back into the global application graph. Build-system
    support tools in @rules_rust itself are deliberately outside Cargo's graph.
    """
    targets = {str(t["id"]): t["label"] for t in actions["targets"]}
    configs = {str(c["id"]): c.get("isTool", False) for c in actions["configuration"]}
    fragments = {str(f["id"]): f for f in actions["pathFragments"]}

    def path(fragment):
        part = fragments[str(fragment)]
        return (path(part["parentId"]) + "/" if part.get("parentId") else "") + part["label"]

    artifacts = {str(a["id"]): path(a["pathFragmentId"]) for a in actions["artifacts"]}

    def label_of(action):
        label = targets[str(action["targetId"])]
        return "@@" + label if label.startswith("//") else label

    outputs = {artifacts[str(output)]: label_of(action) for action in actions["actions"]
               for output in action.get("outputIds", [])}
    seen = set()
    for action in actions["actions"]:
        if action["mnemonic"] not in ("Rustc", "RustcMetadata"):
            continue
        label = label_of(action)
        if not (label.startswith("@@//") or label.startswith("@@rules_rust++crate+")):
            continue
        if label not in scope["expected"]:
            raise ValueError(f"Unscoped compiler action: {label}")
        expected = scope["expected"][label]
        args = action["arguments"]
        features = []
        externs = {}
        for i, arg in enumerate(args):
            cfg = args[i + 1] if arg == "--cfg" else arg.removeprefix("--cfg=") if arg.startswith("--cfg=") else ""
            if cfg.startswith("feature="):
                features.append(json.loads(cfg.removeprefix("feature=")))
            extern = args[i + 1] if arg == "--extern" else arg.removeprefix("--extern=") if arg.startswith("--extern=") else ""
            # proc_macro is supplied by the Rust sysroot, not a Cargo package.
            if extern and extern != "proc_macro":
                name, output = extern.split("=", 1)
                externs[name.removeprefix("force:")] = outputs.get(output, output)
        if sorted(features) != expected["features"]:
            raise ValueError(f"Feature mismatch: {label}: {features} != {expected['features']}")
        if configs[str(action["configurationId"])] != expected["host"]:
            raise ValueError(f"Host/target mismatch: {label}")
        if externs != expected["externs"]:
            raise ValueError(f"Dependency-edge mismatch: {label}: {externs} != {expected['externs']}")
        seen.add(label)
    missing = set(scope["expected"]) - seen
    if missing:
        raise ValueError(f"Missing scoped compiler actions: {sorted(missing)}")
    return f"Verified {len(seen)} scoped compiler targets: exact Cargo features, extern edges and host/target contexts; no global application edges."


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=PRESETS, default="tempo-payload-builder")
    args = parser.parse_args()
    scope = prepare(args.rules_root, args.output, os.environ, args.scope)
    print(json.dumps(dict(roots=scope["roots"], override=scope["override"]), indent=2))


if __name__ == "__main__":
    main()
