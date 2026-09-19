#!/usr/bin/env python3
"""Generate Bazel BUILD files for every crate of every project's Cargo workspace.

Cargo manifests remain the source of truth. Each project (a directory of the
monorepo holding a Cargo workspace and a `bazel/project.toml`) keeps its own
`Cargo.toml`, `Cargo.lock` and `cargo` workflow. For Bazel, this script folds
all of them into one *Bazel Cargo workspace* in `bazel/cargo/` and renders,
for every workspace member, a `BUILD.bazel` that calls the macros in
`//bazel:rust.bzl`:

* a `crate_library`/`crate_proc_macro`/`crate_binary` per `[lib]`/`[[bin]]` target,
* a `crate_unit_test` for the crate's `#[cfg(test)]` tests,
* a `crate_integration_test` per `tests/*.rs` target,
* a `crate_build_script` when the crate has a `build.rs`.

External crates are referenced through the single crate_universe repository
`@crates`, rendered from the same Bazel Cargo workspace, so every dependency
edge Bazel sees is one Cargo resolved.

Why one workspace: a project depends on another (reth on alloy) by patching
the other's crates to its in-tree sources, and Bazel can only build that when
both share every external crate (a type from `@x//:alloy-primitives` is not a
type from `@y//:alloy-primitives`). One crate_universe repository requires one
Cargo resolution, hence one workspace. Cargo forbids nesting workspaces and the
projects' `[workspace.package]`/`[workspace.dependencies]` tables would clash,
so `bazel/cargo/<project>/<member>/Cargo.toml` is a *derived* manifest of each
member: what `cargo metadata` reports for it in its own project (inheritance
resolved), with the project's `disabled_features` dropped from `default`, the
sources reached through symlinks. `bazel/cargo/Cargo.toml` lists them all as
members and carries the union of the projects' `[patch]` sections;
`bazel/cargo/Cargo.lock` is the resolution of that workspace, kept in step with
the projects' lockfiles (every external crate version it picks must appear in a
project's `Cargo.lock`).

Usage (from anywhere):
    python3 bazel/generate.py          # rewrite generated files
    python3 bazel/generate.py --check  # exit 1 if any generated file is stale
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# The monorepo root is the Bazel workspace; labels are workspace-relative.
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BAZELIGNORE = WORKSPACE_ROOT / ".bazelignore"
GENERATOR = Path(__file__).resolve().relative_to(WORKSPACE_ROOT).as_posix()
BAZELIGNORE_BEGIN = f"# BEGIN GENERATED ({GENERATOR})\n"
BAZELIGNORE_END = "# END GENERATED\n"
RUST_BZL = "//bazel:rust.bzl"
# The Bazel Cargo workspace (see the module docstring) and its crate_universe repository.
SHADOW_ROOT = WORKSPACE_ROOT / "bazel" / "cargo"
CRATES_REPO = "@crates"

HEADER = f"""# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 {GENERATOR}` after changing Cargo.toml.
"""

# `cfg(...)` expressions on platform-specific dependencies -> Bazel config
# settings. `None` means the dependency applies on every configured platform
# (no `select()`), an empty list that it applies on none of them; no wasm or
# wasi platform is configured.
CFG_TO_CONDITIONS: dict[str, list[str] | None] = {
    "cfg(unix)": ["@platforms//os:linux", "@platforms//os:macos"],
    'cfg(target_os = "linux")': ["@platforms//os:linux"],
    'cfg(target_os = "macos")': ["@platforms//os:macos"],
    "cfg(windows)": ["@platforms//os:windows"],
    'cfg(target_family = "wasm")': [],
    'cfg(all(target_family = "wasm", target_os = "unknown"))': [],
    'cfg(not(target_family = "wasm"))': None,
    'cfg(not(all(target_family = "wasm", target_os = "unknown")))': None,
    'cfg(not(all(target_os = "wasi", target_env = "p1")))': None,
}


def crate_name_to_ident(name: str) -> str:
    return name.replace("-", "_")


# --- project configuration ---------------------------------------------------


@dataclass
class BuildScript:
    """Inputs and environment of a crate's `build.rs`; see `project.toml`."""

    data_globs: list[str]
    data: list[str]
    env: dict[str, str]


@dataclass
class Project:
    """One Cargo workspace of the monorepo, configured by `<root>/bazel/project.toml`.

    Every label in the configuration is written out in full (`//reth/...`).
    """

    root: Path  # absolute path of the project directory
    # Cargo features that are enabled by default but are *not* built by Bazel.
    # Keys are package names, values map feature -> reason. Each reason must
    # explain why the feature cannot (yet) be built hermetically. The feature
    # is dropped from `default` in the package's derived manifest.
    disabled_features: dict[str, dict[str, str]]
    build_scripts: dict[str, BuildScript]  # keyed by crate name
    # Extra compile-time inputs for targets that `include_*!` files from outside
    # their own crate directory: crate name -> {target -> labels}, where target
    # is an integration test's name, or "crate" for the lib, binaries and unit
    # tests (files inside the crate directory are globbed automatically).
    compile_data: dict[str, dict[str, list[str]]]
    # Files a crate must export for other packages (see compile_data).
    exported_files: dict[str, list[str]]
    # Crates whose tests, and whose dependants' tests, run one process per test.
    process_per_test: list[str]
    # Tags applied to a crate's test targets: crate name -> tags for every test
    # target of the crate, or -> {target -> tags} where target is an integration
    # test's name or "crate" for the unit tests. `manual` keeps a test out of
    # `bazel test //...`.
    test_tags: dict[str, list[str] | dict[str, list[str]]]
    # Named groups of fixture globs exported by a crate to other packages.
    filegroups: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # Runtime environment required by a crate's tests.
    test_env: dict[str, dict[str, str]] = field(default_factory=dict)
    # Bazel retry policy per integration test name, or "crate" for unit tests.
    test_flaky: dict[str, dict[str, bool]] = field(default_factory=dict)
    # Projects with specialized build/test policy can supply their own macros.
    rust_bzl: str | None = None
    cargo_test_names: bool = False

    def tags_for(self, crate: str, target: str) -> list[str] | None:
        tags = self.test_tags.get(crate)
        if isinstance(tags, dict):
            return tags.get(target)
        return tags

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def prefix(self) -> str:
        return self.root.relative_to(WORKSPACE_ROOT).as_posix()

    @property
    def shadow_root(self) -> Path:
        """Where the project's derived manifests live in the Bazel Cargo workspace."""
        return SHADOW_ROOT / self.name

    @property
    def workspace_bzl(self) -> Path:
        return self.root / "bazel" / "workspace.bzl"

    def label(self, package_path: str, target: str = "") -> str:
        """Workspace-absolute label of `target` in the project-relative `package_path`."""
        pkg = "/".join(part for part in (self.prefix, package_path) if part and part != ".")
        return f"//{pkg}:{target}" if target else f"//{pkg}"

    @staticmethod
    def load(config_path: Path) -> Project:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
        root = config_path.parents[1]
        known = {
            "disabled_features",
            "build_scripts",
            "compile_data",
            "exported_files",
            "process_per_test",
            "test_tags",
            "filegroups",
            "test_env",
            "test_flaky",
            "rust_bzl",
            "cargo_test_names",
        }
        unknown = set(cfg) - known
        if unknown:
            sys.exit(f"{config_path}: unknown keys {sorted(unknown)}")
        build_scripts = {}
        for crate, bs in cfg.get("build_scripts", {}).items():
            unknown = set(bs) - {"data_globs", "data", "env"}
            if unknown:
                sys.exit(f"{config_path}: build_scripts.{crate}: unknown keys {sorted(unknown)}")
            build_scripts[crate] = BuildScript(
                data_globs=bs.get("data_globs", []), data=bs.get("data", []), env=bs.get("env", {})
            )
        return Project(
            root=root,
            disabled_features=cfg.get("disabled_features", {}),
            build_scripts=build_scripts,
            compile_data=cfg.get("compile_data", {}),
            exported_files=cfg.get("exported_files", {}),
            process_per_test=cfg.get("process_per_test", []),
            test_tags=cfg.get("test_tags", {}),
            filegroups=cfg.get("filegroups", {}),
            test_env=cfg.get("test_env", {}),
            test_flaky=cfg.get("test_flaky", {}),
            rust_bzl=cfg.get("rust_bzl"),
            cargo_test_names=cfg.get("cargo_test_names", False),
        )


def discover_projects() -> list[Project]:
    configs = sorted(WORKSPACE_ROOT.glob("*/bazel/project.toml"))
    if not configs:
        sys.exit(f"no <project>/bazel/project.toml found under {WORKSPACE_ROOT}")
    return [Project.load(c) for c in configs]


# --- cargo workspace ---------------------------------------------------------


@dataclass(frozen=True, order=True)
class Dep:
    """A resolved dependency edge as rendered in a BUILD file."""

    label: str
    proc_macro: bool
    # `extern crate` name if it differs from the target crate's own name.
    alias: str | None
    # `cfg(...)` string for platform-specific deps, None when unconditional.
    cfg: str | None


@dataclass
class Target:
    kind: str
    name: str
    src_path: str
    required_features: list[str]


@dataclass
class Crate:
    project: Project
    name: str
    ident: str
    package_path: str  # project-relative directory, e.g. "crates/storage/db"
    features: list[str]  # features enabled in the Bazel build
    declared_features: list[str]  # every feature in `[features]`, for `--check-cfg`
    workspace_lints: bool  # `[lints] workspace = true`
    targets: list[Target]
    label: str  # label of the `[lib]` target
    normal: list[Dep] = field(default_factory=list)
    dev: list[Dep] = field(default_factory=list)
    build: list[Dep] = field(default_factory=list)
    version: str | None = None  # override when not inherited from the workspace

    @property
    def lib_target(self) -> str:
        """Bazel name of the `[lib]` target.

        Cargo lets a package's lib and bin share a name (`reth`); Bazel target
        names must be unique, so the library steps aside and keeps `crate_name`.
        """
        if any(t.kind == "bin" and t.name == self.ident for t in self.targets):
            return f"{self.ident}_lib"
        return self.ident

    def target(self, kind: str) -> Target | None:
        return next((t for t in self.targets if t.kind == kind), None)

    @property
    def lib(self) -> Target | None:
        """The `[lib]` target, whether an ordinary library or a proc macro."""
        return self.target("lib") or self.target("proc-macro")

    @property
    def is_proc_macro(self) -> bool:
        return self.target("proc-macro") is not None


def load_root_manifest(project: Project) -> dict:
    with open(project.root / "Cargo.toml", "rb") as f:
        return tomllib.load(f)


def workspace_table(root: dict) -> dict:
    """The `[workspace]` table of a project's root manifest.

    A single-crate project (`revm-inspectors`) has no `[workspace]`; its
    `[package]` and `[lints]` then play the role of `[workspace.package]` and
    `[workspace.lints]` for what the generator derives from them.
    """
    if "workspace" in root:
        return root["workspace"]
    return {"package": root["package"], "lints": root.get("lints", {})}


def cargo_metadata(manifest: Path, *, locked: bool) -> subprocess.CompletedProcess:
    cmd = ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(manifest)]
    if locked:
        cmd.append("--locked")
    return subprocess.run(cmd, cwd=manifest.parent, capture_output=True, text=True)


def project_metadata(project: Project) -> dict:
    """`cargo metadata` of the project's own workspace, which must be in step with its lockfile."""
    result = cargo_metadata(project.root / "Cargo.toml", locked=True)
    if result.returncode != 0:
        sys.exit(f"{project.name}: cargo metadata failed:\n{result.stderr}")
    return json.loads(result.stdout)


# --- the Bazel Cargo workspace -------------------------------------------------


@dataclass
class Member:
    """A workspace member of a project, as Cargo describes it in that project."""

    project: Project
    pkg: dict  # the `packages[]` entry of the project's `cargo metadata`
    manifest: dict  # the real manifest, parsed

    @property
    def name(self) -> str:
        return self.pkg["name"]

    @property
    def real_dir(self) -> Path:
        return Path(self.pkg["manifest_path"]).parent

    @property
    def package_path(self) -> str:
        return self.real_dir.relative_to(self.project.root).as_posix()

    @property
    def shadow_dir(self) -> Path:
        return self.project.shadow_root / self.package_path


def collect_members(project: Project, meta: dict) -> list[Member]:
    packages = {p["id"]: p for p in meta["packages"]}
    members = []
    for member_id in meta["workspace_members"]:
        pkg = packages[member_id]
        with open(pkg["manifest_path"], "rb") as f:
            manifest = tomllib.load(f)
        members.append(Member(project=project, pkg=pkg, manifest=manifest))
    unknown = set(project.disabled_features) - {m.name for m in members}
    if unknown:
        sys.exit(f"{project.name}: disabled_features lists unknown crates {sorted(unknown)}")
    return sorted(members, key=lambda m: m.package_path)


# TOML emission, limited to what a derived manifest needs.


def toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # a TOML basic string accepts JSON's escapes
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot emit {value!r} as TOML")


def toml_table(header: str, table: dict) -> str:
    lines = []
    for key, value in table.items():
        rendered = toml_value(value)
        if isinstance(value, list) and len(rendered) > 100:
            rendered = "[\n" + "".join(f"    {toml_value(v)},\n" for v in value) + "]"
        lines.append(f"{toml_key(key)} = {rendered}\n")
    return f"[{header}]\n{''.join(lines)}\n"


def relative_link(link: Path, target: Path) -> str:
    return os.path.relpath(target, link.parent)


def git_source(source: str) -> dict:
    """`git+https://host/repo?rev=abc` -> `{git: ..., rev: ...}` as a manifest dependency spec."""
    url, _, query = source[len("git+") :].partition("?")
    spec = {"git": url}
    if query:
        key, _, value = query.partition("=")
        if key not in ("rev", "branch", "tag"):
            sys.exit(f"unsupported git dependency source {source!r}")
        spec[key] = value
    return spec


@dataclass
class DerivedManifest:
    text: str
    # Derived-package paths mapped to their real sources (`src`, `build.rs`,
    # `tests`, ...), including targets outside the package directory.
    children: dict[str, Path]


def colliding_renames(members: list[Member]) -> set[tuple[str, str]]:
    """`(package, rename)` pairs whose rename is another external package's extern name.

    crate_universe names its hub aliases after the rename (`@crates//:criterion`
    for `criterion = { package = "codspeed-criterion-compat" }`) and refuses to
    render when two different packages claim the same alias name anywhere in
    the workspace. Such dependencies go into the derived manifests under their
    real package name; the BUILD files restore the rename with `aliases`.
    """
    packages_by_extern: dict[str, set[str]] = defaultdict(set)
    for member in members:
        for dep in member.pkg["dependencies"]:
            if dep.get("path"):
                continue  # workspace members get no hub alias
            packages_by_extern[dep["rename"] or dep["name"]].add(dep["name"])
    return {
        (name, extern)
        for extern, names in packages_by_extern.items()
        if len(names) > 1
        for name in names
        if name != extern
    }


def derive_manifest(member: Member, by_dir: dict[Path, Member], unrenamed: set[tuple[str, str]]) -> DerivedManifest:
    """The member's manifest as Cargo sees it in its project, made self-contained.

    Package fields and dependency specs come from `cargo metadata` and thus
    have `workspace = true` inheritance resolved. Targets are listed
    explicitly (auto-discovery off) so the derived package has exactly the
    project's targets. `[features]` is copied from the real manifest because
    `cargo metadata` adds the implicit features of optional dependencies, and
    spelling those out would change how `dep/feature` entries behave.
    Dependencies in `unrenamed` (see colliding_renames) lose their rename.
    """
    pkg, project = member.pkg, member.project
    children: dict[str, Path] = {}

    def rel(path: str) -> str:
        source = Path(path).resolve()
        if not source.is_relative_to(member.real_dir):
            # RLP's example is ../../examples/enum.rs. Never symlink `..`:
            # that is the derived package's parent directory, not an input.
            p = Path("__workspace__") / source.relative_to(project.root)
            children[p.as_posix()] = source
            return p.as_posix()
        p = source.relative_to(member.real_dir)
        children[p.parts[0]] = member.real_dir / p.parts[0]
        return p.as_posix()

    package = {
        "name": pkg["name"],
        "version": pkg["version"],
        "edition": pkg["edition"],
    }
    if pkg.get("rust_version"):
        package["rust-version"] = pkg["rust_version"]
    if pkg.get("links"):
        package["links"] = pkg["links"]
    build_script = next((t for t in pkg["targets"] if t["kind"] == ["custom-build"]), None)
    package["build"] = rel(build_script["src_path"]) if build_script else False
    for auto in ("autolib", "autobins", "autoexamples", "autotests", "autobenches"):
        package[auto] = False

    out = [
        f"# GENERATED FILE - DO NOT EDIT. Regenerate with `python3 {GENERATOR}`.\n",
        "#\n",
        f"# Bazel-only derivation of `{project.prefix}/{member.package_path}/Cargo.toml`: the same\n",
        f"# package as `cargo metadata` describes it in the {project.name} workspace, with\n",
        "# workspace inheritance resolved. Cargo builds are unaffected.\n",
    ]
    disabled = project.disabled_features.get(pkg["name"], {})
    if disabled:
        out += [
            "#\n",
            "# These default features are left out, because Bazel cannot build them\n",
            "# hermetically yet (see bazel/project.toml, disabled_features):\n",
        ] + [f"#   - {feature}: {why}\n" for feature, why in disabled.items()]
    out.append("\n")
    out.append(toml_table("package", package))

    for t in pkg["targets"]:
        (kind,) = t["kind"]
        if kind == "custom-build":
            continue
        target = {"name": t["name"], "path": rel(t["src_path"])}
        if kind == "proc-macro":
            target["proc-macro"] = True
            header = "lib"
        elif kind == "lib":
            if t["crate_types"] != ["lib"]:
                target["crate-type"] = t["crate_types"]
            header = "lib"
        elif kind in ("bin", "test", "bench", "example"):
            if kind == "example" and t["crate_types"] != ["bin"]:
                target["crate-type"] = t["crate_types"]
            header = f"[{kind}]"
        else:
            sys.exit(f"{pkg['name']}: unsupported target kind {kind!r}")
        if t.get("required-features"):
            target["required-features"] = t["required-features"]
        out.append(toml_table(header, target))

    features = dict(member.manifest.get("features", {}))
    if disabled:
        default = features.get("default", [])
        unknown = set(disabled) - set(default)
        if unknown:
            sys.exit(f"{pkg['name']}: disabled_features lists non-default features {sorted(unknown)}")
        features["default"] = [f for f in default if f not in disabled]
    original_feature_values = list(itertools.chain.from_iterable(features.values()))
    for dep in pkg["dependencies"]:
        name, rename = dep["name"], dep["rename"]
        if (name, rename) not in unrenamed:
            continue
        # Preserve the public feature names while rewriting dependency references.
        # An optional dependency's implicit feature needs an explicit alias now.
        if dep["optional"] and rename not in features and f"dep:{rename}" not in original_feature_values:
            features[rename] = [f"dep:{name}"]
        for feature, entries in features.items():
            features[feature] = [
                f"dep:{name}" if entry == f"dep:{rename}" else
                name + entry[len(rename):] if entry.startswith((f"{rename}/", f"{rename}?/")) else
                entry for entry in entries
            ]
    if features:
        out.append(toml_table("features", features))

    tables: dict[str, dict[str, dict]] = defaultdict(dict)
    for dep in pkg["dependencies"]:
        if dep.get("registry"):
            sys.exit(f"{pkg['name']}: dependency {dep['name']} uses an alternative registry; unsupported")
        spec: dict = {}
        rename = dep["rename"]
        if (dep["name"], rename) in unrenamed:
            rename = None
        if rename:
            spec["package"] = dep["name"]
        if dep.get("path"):
            target_member = by_dir.get(Path(dep["path"]))
            if target_member is None:
                sys.exit(f"{pkg['name']}: path dependency {dep['name']} at {dep['path']} is not a workspace member of any project")
            spec["path"] = relative_link(member.shadow_dir / "Cargo.toml", target_member.shadow_dir)
        elif dep["source"] and dep["source"].startswith("git+"):
            spec.update(git_source(dep["source"]))
        else:
            spec["version"] = dep["req"]
        if dep["features"]:
            spec["features"] = dep["features"]
        if dep["optional"]:
            spec["optional"] = True
        if not dep["uses_default_features"]:
            spec["default-features"] = False
        table = {None: "dependencies", "dev": "dev-dependencies", "build": "build-dependencies"}[dep["kind"]]
        if dep["target"]:
            table = f"target.'{dep['target']}'.{table}"
        tables[table][rename or dep["name"]] = spec
    for table in sorted(tables, key=lambda t: (t.startswith("target."), t)):
        out.append(toml_table(table, tables[table]))

    return DerivedManifest(text="".join(out).rstrip("\n") + "\n", children=children)


def render_shadow_root_manifest(projects: list[Project], roots: dict[str, dict], members: list[Member], digest: str) -> str:
    by_dir = {m.real_dir: m for m in members}
    patches: dict[str, dict[str, dict]] = defaultdict(dict)
    for project in projects:
        for registry, entries in roots[project.name].get("patch", {}).items():
            for name, spec in entries.items():
                spec = dict(spec)
                if "path" in spec:
                    target = by_dir.get((project.root / spec["path"]).resolve())
                    if target is None:
                        sys.exit(f"{project.name}: [patch.{registry}] {name} points outside the projects' workspace members")
                    spec["path"] = target.shadow_dir.relative_to(SHADOW_ROOT).as_posix()
                previous = patches[registry].get(name)
                if previous is not None and previous != spec:
                    sys.exit(f"[patch.{registry}] {name}: {project.name} disagrees with another project")
                patches[registry][name] = spec

    # Resolver 3 only changes how `rust-version` steers version selection, and
    # the Bazel workspace never selects versions (its lock is seeded from the
    # projects'); the highest resolver in use is right for every member.
    resolvers = {workspace_table(roots[p.name]).get("resolver", "2") for p in projects}
    if not resolvers <= {"2", "3"}:
        sys.exit(f"unsupported Cargo resolver among projects: {sorted(resolvers)}")

    out = [
        f"# GENERATED FILE - DO NOT EDIT. Regenerate with `python3 {GENERATOR}`.\n",
        "#\n",
        "# The Cargo workspace Bazel builds: every crate of every project, so that\n",
        "# crate_universe resolves one set of external crates for all of them and\n",
        "# projects can depend on each other's sources. Members are derived manifests\n",
        f"# (see {GENERATOR}); this workspace is never used by `cargo build`.\n",
        "\n",
        toml_table(
            "workspace",
            {"resolver": max(resolvers), "members": [m.shadow_dir.relative_to(SHADOW_ROOT).as_posix() for m in members]},
        ),
    ]
    for registry in sorted(patches):
        out.append(f"# Union of the projects' `[patch.{registry}]` sections.\n")
        out.append(toml_table(f"patch.{registry}", patches[registry]))
    out += [
        "# Digest of every member manifest. crate_universe only re-resolves when a\n",
        "# manifest it was given changes, and it is given this one alone.\n",
        toml_table("workspace.metadata.bazel", {"members-digest": digest}),
    ]
    return "".join(out).rstrip("\n") + "\n"


@dataclass
class ShadowWorkspace:
    """The Bazel Cargo workspace in `bazel/cargo/`, as it must be on disk.

    `manifests` maps every generated manifest (root and members) to its
    contents, `links` every symlink to its relative target.
    """

    manifests: dict[Path, str]
    links: dict[Path, str]

    @property
    def root_manifest(self) -> Path:
        return SHADOW_ROOT / "Cargo.toml"


def shadow_workspace(projects: list[Project], roots: dict[str, dict], members: list[Member]) -> ShadowWorkspace:
    by_dir = {m.real_dir: m for m in members}
    unrenamed = colliding_renames(members)
    manifests: dict[Path, str] = {}
    links: dict[Path, str] = {}
    digest = hashlib.sha256()
    for member in members:
        derived = derive_manifest(member, by_dir, unrenamed)
        manifests[member.shadow_dir / "Cargo.toml"] = derived.text
        digest.update(member.shadow_dir.relative_to(SHADOW_ROOT).as_posix().encode() + b"\0")
        digest.update(derived.text.encode() + b"\0")
        for child, source in derived.children.items():
            link = member.shadow_dir / child
            links[link] = relative_link(link, source)
    manifests[SHADOW_ROOT / "Cargo.toml"] = render_shadow_root_manifest(projects, roots, members, digest.hexdigest())
    return ShadowWorkspace(manifests=manifests, links=links)


def sync_shadow_tree(projects: list[Project], shadow: ShadowWorkspace, check: bool) -> list[Path]:
    """Make the shadow's symlinks match; drop anything else under the projects' shadow roots.

    Returns the paths that were wrong (created, replaced or removed).
    """
    stale = []
    for link, target in shadow.links.items():
        if link.is_symlink() and os.readlink(link) == target:
            continue
        stale.append(link)
        if check:
            continue
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

    expected = set(shadow.manifests) | set(shadow.links)
    for project in projects:
        if not project.shadow_root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(project.shadow_root, topdown=False):
            dir = Path(dirpath)
            for name in filenames + [d for d in dirnames if (dir / d).is_symlink()]:
                path = dir / name
                if path not in expected:
                    stale.append(path)
                    if not check:
                        path.unlink()
            if not check and dir != project.shadow_root and not any(dir.iterdir()):
                dir.rmdir()
    return stale


def render_shadow_build_file() -> str:
    return (
        HEADER
        + "\n"
        + f"# The Cargo workspace as seen by Bazel; see {GENERATOR}.\n"
        + 'exports_files([\n    "Cargo.lock",\n    "Cargo.toml",\n])\n'
    )


def render_bazelignore(projects: list[Project]) -> str:
    """Keep Bazel from discovering the projects' packages twice through the shadow symlinks."""
    text = BAZELIGNORE.read_text()
    begin = text.index(BAZELIGNORE_BEGIN) + len(BAZELIGNORE_BEGIN)
    end = text.index(BAZELIGNORE_END)
    dirs = sorted(p.shadow_root.relative_to(WORKSPACE_ROOT).as_posix() for p in projects)
    return text[:begin] + "".join(f"{d}\n" for d in dirs) + text[end:]


# The Bazel workspace's lockfile.


def lock_packages(lock: Path) -> list[dict]:
    with open(lock, "rb") as f:
        return tomllib.load(f).get("package", [])


def seed_lockfile(projects: list[Project], lock: Path) -> None:
    """Start the Bazel workspace's lockfile from the projects' locked versions.

    Cargo keeps whatever a lockfile already pins, so seeding it with the union
    of the projects' entries makes the workspace resolve to the versions the
    projects use wherever possible. Cargo drops the entries nothing needs.
    """
    packages: dict[tuple[str, str, str], dict] = {}
    for project in projects:
        for pkg in lock_packages(project.root / "Cargo.lock"):
            if pkg.get("source"):
                packages.setdefault((pkg["name"], pkg["version"], pkg["source"]), pkg)
    out = ["# This file is automatically @generated by Cargo.\n# It is not intended for manual editing.\n", "version = 4\n\n"]
    for _, pkg in sorted(packages.items()):
        out.append("[[package]]\n")
        for key in ("name", "version", "source", "checksum"):
            if key in pkg:
                out.append(f"{key} = {toml_value(pkg[key])}\n")
        out.append("\n")
    lock.write_text("".join(out))


def check_lockfile_alignment(projects: list[Project], lock: Path) -> None:
    """Every external crate version Bazel builds must be one some project's Cargo builds."""
    pinned: dict[tuple[str, str], list[str]] = defaultdict(list)
    for project in projects:
        for pkg in lock_packages(project.root / "Cargo.lock"):
            if pkg.get("source"):
                pinned[(pkg["name"], pkg["source"])].append(pkg["version"])
    problems = []
    for pkg in lock_packages(lock):
        if not pkg.get("source"):
            continue
        versions = pinned.get((pkg["name"], pkg["source"]))
        if versions is None:
            problems.append(f"  {pkg['name']} {pkg['version']}: not in any project's Cargo.lock")
        elif pkg["version"] not in versions:
            problems.append(f"  {pkg['name']} {pkg['version']}: projects pin {', '.join(sorted(set(versions)))}")
    if problems:
        rel = lock.relative_to(WORKSPACE_ROOT)
        sys.exit(
            f"{rel} pins external crates no project pins; align it with\n"
            f"  cargo update --manifest-path {rel.parent}/Cargo.toml -p <name>@<version> --precise <version>\n"
            + "\n".join(problems)
        )


def bazel_workspace_metadata(projects: list[Project], shadow: ShadowWorkspace, check: bool) -> tuple[dict, bool]:
    """`cargo metadata` of the Bazel Cargo workspace; returns it and whether the lockfile changed."""
    lock = SHADOW_ROOT / "Cargo.lock"
    if not lock.exists():
        if check:
            return {}, True
        seed_lockfile(projects, lock)
    before = lock.read_text()
    result = cargo_metadata(shadow.root_manifest, locked=check)
    if result.returncode != 0:
        if check and "--locked" in result.stderr:
            return {}, True
        sys.exit(f"cargo metadata failed for {shadow.root_manifest.relative_to(WORKSPACE_ROOT)}:\n{result.stderr}")
    check_lockfile_alignment(projects, lock)
    return json.loads(result.stdout), lock.read_text() != before


def build_crates(projects: list[Project], members: list[Member], meta: dict, *, derived: bool = True) -> dict[str, Crate]:
    """Crates of the Bazel workspace with their resolved features and dependency edges."""
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    member_ids = set(meta["workspace_members"])
    by_shadow_dir = {m.shadow_dir if derived else m.real_dir: m for m in members}
    versions = {p.name: workspace_table(load_root_manifest(p))["package"].get("version") for p in projects}

    def lib_target(pkg: dict) -> dict | None:
        for t in pkg["targets"]:
            if any(k in ("lib", "rlib", "proc-macro") for k in t["kind"]):
                return t
        return None

    # External crates that workspace members depend on at more than one version
    # only get `@crates//:<name>-<version>` aliases, never a bare `@crates//:<name>`.
    external_versions: dict[str, set[str]] = defaultdict(set)
    for member_id in member_ids:
        for dep in nodes[member_id]["deps"]:
            if dep["pkg"] not in member_ids:
                pkg = packages[dep["pkg"]]
                external_versions[pkg["name"]].add(pkg["version"])

    # Renames the derived manifests dropped (see colliding_renames), by member:
    # external package name -> extern crate name the sources expect.
    unrenamed = colliding_renames(members) if derived else set()
    dropped_renames: dict[str, dict[str, str]] = {}

    crates: dict[str, Crate] = {}
    for member_id in member_ids:
        pkg = packages[member_id]
        shadow_dir = Path(pkg["manifest_path"]).parent
        member = by_shadow_dir.get(shadow_dir)
        if member is None:
            sys.exit(f"{pkg['name']}: workspace member {shadow_dir} was not derived from any project")
        dropped_renames[member_id] = {
            d["name"]: crate_name_to_ident(d["rename"]) for d in member.pkg["dependencies"] if (d["name"], d["rename"]) in unrenamed
        }
        crate = Crate(
            project=member.project,
            name=pkg["name"],
            version=pkg["version"] if pkg["version"] != versions[member.project.name] else None,
            ident=crate_name_to_ident(pkg["name"]),
            package_path=member.package_path,
            features=sorted(nodes[member_id]["features"]),
            declared_features=sorted(pkg["features"]),
            # A single-crate project's own `[lints]` are its project lints (see workspace_table).
            workspace_lints=bool(member.manifest.get("lints", {}).get("workspace", False))
            or (member.real_dir == member.project.root and "lints" in member.manifest),
            targets=[
                Target(
                    kind=t["kind"][0],
                    name=t["name"],
                    src_path=Path(t["src_path"]).relative_to(shadow_dir).as_posix(),
                    required_features=t.get("required-features", []),
                )
                for t in pkg["targets"]
            ],
            label="",
        )
        crate.label = member.project.label(member.package_path, crate.lib_target)
        crates[member_id] = crate

    for member_id, crate in crates.items():
        activated = activated_optional_deps(packages[member_id], crate.features)
        for dep in nodes[member_id]["deps"]:
            if dep["pkg"] == member_id:
                # A crate listing itself in `[dev-dependencies]` to enable extra
                # features for its tests; unified features already cover that,
                # and the test targets link the crate explicitly.
                continue
            dep_pkg = packages[dep["pkg"]]
            lib = lib_target(dep_pkg)
            if lib is None:
                continue  # binary-only dependency (artifact deps are not used)
            proc_macro = "proc-macro" in lib["kind"]
            if dep["pkg"] in member_ids:
                dep_label = crates[dep["pkg"]].label
                lib_name = crates[dep["pkg"]].ident
            else:
                # crate_universe names the alias after the `package = "..."`
                # rename when there is one (`criterion`, not
                # `codspeed-criterion-compat`), and suffixes the version when a
                # member depends on several versions of the crate.
                renames = {
                    crate_name_to_ident(d["rename"]): d["rename"]
                    for d in packages[member_id]["dependencies"]
                    if d["name"] == dep_pkg["name"] and d["rename"]
                }
                alias_name = renames.get(dep["name"], dep_pkg["name"])
                if len(external_versions[dep_pkg["name"]]) > 1:
                    dep_label = f"{CRATES_REPO}//:{alias_name}-{dep_pkg['version']}"
                else:
                    dep_label = f"{CRATES_REPO}//:{alias_name}"
                lib_name = crate_name_to_ident(lib["name"])
            extern_name = dropped_renames[member_id].get(dep_pkg["name"], dep["name"])
            alias = extern_name if extern_name != lib_name else None
            for dk in dep["dep_kinds"]:
                if is_disabled_optional_dep(packages[member_id], dep["name"], dk["kind"], activated):
                    # `cargo metadata` lists an optional dependency whenever it is
                    # in the lockfile, even if no enabled feature turns it on
                    # (weak `dep?/feature` entries put it there). Cargo would
                    # not build it, so neither does Bazel.
                    continue
                cfg = dk.get("target")
                if cfg is not None:
                    if cfg not in CFG_TO_CONDITIONS:
                        sys.exit(f"{crate.name}: unsupported platform cfg {cfg!r} on {dep_pkg['name']}")
                    conditions = CFG_TO_CONDITIONS[cfg]
                    if conditions == []:
                        continue
                    if conditions is None:
                        cfg = None
                rendered = Dep(label=dep_label, proc_macro=proc_macro, alias=alias, cfg=cfg)
                {None: crate.normal, "dev": crate.dev, "build": crate.build}[dk["kind"]].append(rendered)

    for crate in crates.values():
        crate.normal = sorted(set(crate.normal))
        crate.dev = sorted(set(crate.dev) - set(crate.normal))
        crate.build = sorted(set(crate.build))
    return crates


@dataclass
class MemberDeps:
    """Dependencies of one external crate on workspace members, by annotation attribute."""

    name: str
    version: str
    attrs: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def external_member_deps(crates: dict[str, Crate], meta: dict) -> list[MemberDeps]:
    """External crates whose resolved dependencies include workspace members.

    Happens whenever `[patch.crates-io]` redirects a crate that external crates
    also depend on to the tree (crates.io `alloy-evm` -> in-tree
    `alloy-consensus`). crate_universe never renders an edge to a workspace
    member, so those edges are added back as `crate.annotation(deps = ...)`.
    """
    packages = {p["id"]: p for p in meta["packages"]}
    member_ids = set(meta["workspace_members"])
    attr_for = {
        (None, False): "deps",
        (None, True): "proc_macro_deps",
        ("build", False): "build_script_deps",
        ("build", True): "build_script_proc_macro_deps",
    }
    result = []
    for node in meta["resolve"]["nodes"]:
        if node["id"] in member_ids:
            continue
        pkg = packages[node["id"]]
        activated = activated_optional_deps(pkg, node["features"])
        found = MemberDeps(name=pkg["name"], version=pkg["version"])
        for dep in node["deps"]:
            if dep["pkg"] not in member_ids:
                continue
            member = crates[dep["pkg"]]
            if dep["name"] != member.ident:
                sys.exit(
                    f"{pkg['name']} {pkg['version']}: depends on workspace member {member.name} under the"
                    f" name {dep['name']!r}; crate annotations cannot express renamed dependencies"
                )
            for dk in dep["dep_kinds"]:
                if dk["kind"] == "dev":
                    continue  # external crates' tests are never built
                if is_disabled_optional_dep(pkg, dep["name"], dk["kind"], activated):
                    continue
                # Annotation attributes are unconditional; a platform-specific
                # edge is added on every platform, which only builds the member
                # once more than Cargo would. `@@//` names the main repository
                # from inside the crate repositories, which have no mapping for it.
                found.attrs[attr_for[(dk["kind"], member.is_proc_macro)]].append(f"@@{member.label}")
        if found.attrs:
            found.attrs = {k: sorted(set(v)) for k, v in sorted(found.attrs.items())}
            result.append(found)
    return sorted(result, key=lambda m: (m.name, m.version))


def activated_optional_deps(pkg: dict, enabled_features: list[str]) -> set[str]:
    """Names (`extern crate` form) of the optional dependencies `enabled_features` turn on.

    A feature turns an optional dependency `x` on through `dep:x` or `x/feat`
    (but not the weak `x?/feat`); an optional dependency that no feature names
    with `dep:` gets an implicit feature of its own name.
    """
    features = pkg["features"]
    explicit = {e[len("dep:") :] for entries in features.values() for e in entries if e.startswith("dep:")}
    activated = set()
    for feature in enabled_features:
        for entry in features.get(feature, []):
            if entry.startswith("dep:"):
                activated.add(entry[len("dep:") :])
            elif "/" in entry and not entry.split("/", 1)[0].endswith("?"):
                activated.add(entry.split("/", 1)[0])
        if feature not in explicit:
            activated.add(feature)  # implicit feature of an optional dependency, if one is so named
    return {crate_name_to_ident(name) for name in activated}


def is_disabled_optional_dep(pkg: dict, dep_name: str, kind: str | None, activated: set[str]) -> bool:
    """Whether every `[*dependencies]` entry of `kind` for `dep_name` is optional and off."""
    decls = [
        d for d in pkg["dependencies"] if d["kind"] == kind and crate_name_to_ident(d["rename"] or d["name"]) == dep_name
    ]
    return bool(decls) and all(d["optional"] for d in decls) and dep_name not in activated


# --- rendering ---------------------------------------------------------------


def starlark_str(s: str) -> str:
    return json.dumps(s)


def render_list(items: list[str], indent: int = 4) -> str:
    if not items:
        return "[]"
    pad = " " * indent
    inner = "".join(f"{pad}    {item},\n" for item in items)
    return f"[\n{inner}{pad}]"


def render_deps(deps: list[Dep], *, proc_macro: bool, indent: int = 4) -> str | None:
    """Render a dependency list, wrapping platform-specific deps in `select()`."""
    deps = [d for d in deps if d.proc_macro == proc_macro]
    if not deps:
        return None
    unconditional = sorted({d.label for d in deps if d.cfg is None})
    by_cfg: dict[str, set[str]] = defaultdict(set)
    for d in deps:
        if d.cfg is not None and d.label not in unconditional:
            by_cfg[d.cfg].add(d.label)

    out = render_list([starlark_str(l) for l in unconditional], indent)
    pad = " " * indent
    for cfg in sorted(by_cfg):
        labels = "[" + ", ".join(starlark_str(l) for l in sorted(by_cfg[cfg])) + "]"
        branches = "".join(f"{pad}    {starlark_str(c)}: {labels},\n" for c in CFG_TO_CONDITIONS[cfg])
        out += f" + select({{\n{branches}{pad}    \"//conditions:default\": [],\n{pad}}})"
    return out


def render_aliases(deps: list[Dep], indent: int = 4) -> str | None:
    aliases = sorted({(d.label, d.alias) for d in deps if d.alias})
    if not aliases:
        return None
    pad = " " * indent
    inner = "".join(f"{pad}    {starlark_str(l)}: {starlark_str(a)},\n" for l, a in aliases)
    return f"{{\n{inner}{pad}}}"


def render_rule(rule: str, attrs: list[tuple[str, str | None]]) -> str:
    body = "".join(f"    {k} = {v},\n" for k, v in attrs if v is not None)
    return f"{rule}(\n{body})\n"


def render_tags(tags: list[str] | None) -> str | None:
    return render_list([starlark_str(t) for t in tags]) if tags else None


def render_build_script_data(bs: BuildScript) -> str | None:
    if not bs.data_globs and not bs.data:
        return None
    parts = []
    if bs.data_globs:
        parts.append(f"glob({render_list([starlark_str(g) for g in bs.data_globs], 4)})")
    if bs.data:
        parts.append(render_list([starlark_str(l) for l in bs.data], 4))
    return " + ".join(parts)


def render_build_file(crate: Crate, crates: dict[str, Crate], *, cargo_test_names: bool = False) -> str:
    # Tempo's snapshots require Cargo's module names. Keep other projects'
    # existing Bazel test names until they opt into the same convention.
    project = crate.project
    cargo_test_names = cargo_test_names or project.cargo_test_names
    lib = crate.lib
    lib_rule = "crate_proc_macro" if lib and lib.kind == "proc-macro" else "crate_library"
    bins = [t for t in crate.targets if t.kind == "bin" and set(t.required_features) <= set(crate.features)]
    tests = [t for t in crate.targets if t.kind == "test"]
    build_script = crate.target("custom-build")
    # `#[test_fuzz]`-instrumented tests need extra runtime plumbing (see rust.bzl).
    test_fuzz = "True" if any(d.label == f"{CRATES_REPO}//:test-fuzz" for d in crate.dev) else None
    # Tests that launch nodes retain GiBs per node for the process lifetime
    # (see bazel/process_per_test.bzl); run them one process per test.
    per_process_labels = {c.label for c in crates.values() if c.name in c.project.process_per_test}
    launches_nodes = crate.name in project.process_per_test or any(
        d.label in per_process_labels for d in crate.normal + crate.dev
    )
    process_per_test = "True" if launches_nodes else None

    loads = ["crate_cargo_toml_env_vars"]
    if lib:
        loads.append(lib_rule)
    if bins:
        loads.append("crate_binary")
    if lib or bins:
        loads.append("crate_unit_test")
    if tests:
        loads.append("crate_integration_test")
    if build_script:
        loads.append("crate_build_script")

    out = [HEADER, "\n"]
    out.append(f'load("{project.rust_bzl or RUST_BZL}", {", ".join(starlark_str(l) for l in sorted(loads))})\n')
    out.append(f'load("{project.label("bazel", "workspace.bzl")}", "WORKSPACE")\n\n')
    out.append(f"FEATURES = {render_list([starlark_str(f) for f in crate.features], 0)}\n\n")
    out.append(
        f"DECLARED_FEATURES = {render_list([starlark_str(f) for f in crate.declared_features], 0)}\n\n"
    )
    if crate.name in project.exported_files:
        exported = project.exported_files[crate.name]
        out.append(f"exports_files({render_list([starlark_str(f) for f in exported], 0)})\n\n")
    for name, patterns in project.filegroups.get(crate.name, {}).items():
        out.append(render_rule("filegroup", [
            ("name", starlark_str(name)),
            ("srcs", f"glob({render_list([starlark_str(p) for p in patterns])})"),
            ("visibility", '["//visibility:public"]'),
        ]) + "\n")
    out.append("crate_cargo_toml_env_vars(WORKSPACE)\n\n")

    build_script_label = None
    if build_script:
        build_script_label = f":{crate.ident}_build_script"
        bs = project.build_scripts.get(crate.name, BuildScript([], [], {}))
        env_rendered = None
        if bs.env:
            env_rendered = "{\n" + "".join(f"        {starlark_str(k)}: {starlark_str(v)},\n" for k, v in bs.env.items()) + "    }"
        out.append(
            render_rule(
                "crate_build_script",
                [
                    ("name", starlark_str(f"{crate.ident}_build_script")),
                    ("workspace", "WORKSPACE"),
                    ("version", starlark_str(crate.version) if crate.version else None),
                    ("crate_features", "FEATURES"),
                    ("deps", render_deps(crate.build, proc_macro=False)),
                    ("proc_macro_deps", render_deps(crate.build, proc_macro=True)),
                    ("aliases", render_aliases(crate.build)),
                    ("data", render_build_script_data(bs)),
                    ("build_script_env", env_rendered),
                ],
            )
        )
        out.append("\n")

    extra_compile_data = project.compile_data.get(crate.name, {})
    normal_attrs = [
        ("workspace", "WORKSPACE"),
        ("version", starlark_str(crate.version) if crate.version else None),
        ("crate_features", "FEATURES"),
        ("declared_features", "DECLARED_FEATURES"),
        ("workspace_lints", None if crate.workspace_lints else "False"),
        ("deps", render_deps(crate.normal, proc_macro=False)),
        ("proc_macro_deps", render_deps(crate.normal, proc_macro=True)),
        ("aliases", render_aliases(crate.normal)),
        ("compile_data", render_list([starlark_str(l) for l in extra_compile_data["crate"]]) if "crate" in extra_compile_data else None),
        ("build_script", starlark_str(build_script_label) if build_script_label else None),
    ]

    if lib:
        attrs = [("name", starlark_str(crate.lib_target))]
        if crate.lib_target != crate.ident:
            attrs.append(("crate_name", starlark_str(crate.ident)))
        if lib.src_path != "src/lib.rs":
            attrs.append(("crate_root", starlark_str(lib.src_path)))
        out.append(render_rule(lib_rule, attrs + normal_attrs))
        out.append("\n")

    for b in bins:
        attrs = [("name", starlark_str(b.name))]
        if b.src_path != "src/main.rs":
            attrs.append(("crate_root", starlark_str(b.src_path)))
        bin_attrs = list(normal_attrs)
        if lib:
            # A binary in a lib+bin package links against the package's library.
            bin_deps = crate.normal + [Dep(f":{crate.lib_target}", False, None, None)]
            bin_attrs = [
                ("deps", render_deps(bin_deps, proc_macro=False)) if k == "deps" else (k, v) for k, v in bin_attrs
            ]
        out.append(render_rule("crate_binary", attrs + bin_attrs))
        out.append("\n")

    # Unit tests: the lib's `#[cfg(test)]` tests, or the bin's for bin-only crates.
    unit_test_crate = crate.lib_target if lib else (bins[0].name if bins else None)
    if unit_test_crate:
        out.append(
            render_rule(
                "crate_unit_test",
                [
                    ("name", starlark_str(f"{crate.ident}_test")),
                    ("workspace", "WORKSPACE"),
                    ("version", starlark_str(crate.version) if crate.version else None),
                    ("crate", starlark_str(f":{unit_test_crate}")),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(crate.dev, proc_macro=False)),
                    ("proc_macro_deps", render_deps(crate.dev, proc_macro=True)),
                    ("aliases", render_aliases(crate.dev)),
                    ("data", render_list([starlark_str(l) for l in extra_compile_data["crate"]]) if "crate" in extra_compile_data else None),
                    ("env", render_dict(list(project.test_env[crate.name].items()), 4) if crate.name in project.test_env else None),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_tags(project.tags_for(crate.name, "crate"))),
                    ("flaky", "True" if project.test_flaky.get(crate.name, {}).get("crate") else None),
                ],
            )
        )
        out.append("\n")

    for t in tests:
        if not set(t.required_features) <= set(crate.features):
            continue
        deps = crate.normal + crate.dev
        if lib:
            deps = deps + [Dep(f":{crate.lib_target}", False, None, None)]
        test_compile_data = extra_compile_data.get(t.name)
        # Like cargo, expose the package's binaries as `CARGO_BIN_EXE_<name>`
        # (rules_rust derives those from `bin` targets in `data`).
        bin_data = [starlark_str(f":{b.name}") for b in bins]
        out.append(
            render_rule(
                "crate_integration_test",
                [
                    ("name", starlark_str(f"{crate.ident}_{crate_name_to_ident(t.name)}_test")),
                    ("crate_name", starlark_str(crate_name_to_ident(t.name)) if cargo_test_names else None),
                    ("workspace", "WORKSPACE"),
                    ("version", starlark_str(crate.version) if crate.version else None),
                    ("crate_root", starlark_str(t.src_path)),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(deps, proc_macro=False)),
                    ("proc_macro_deps", render_deps(deps, proc_macro=True)),
                    ("aliases", render_aliases(deps)),
                    ("compile_data", render_list([starlark_str(l) for l in test_compile_data]) if test_compile_data else None),
                    ("data", render_list(bin_data) if bin_data else None),
                    ("env", render_dict(list(project.test_env[crate.name].items()), 4) if crate.name in project.test_env else None),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_tags(project.tags_for(crate.name, t.name))),
                    ("flaky", "True" if project.test_flaky.get(crate.name, {}).get(t.name) else None),
                ],
            )
        )
        out.append("\n")

    return "".join(out).rstrip("\n") + "\n"


def render_dict(items: list[tuple[str, str]], indent: int = 0) -> str:
    if not items:
        return "{}"
    pad = " " * indent
    inner = "".join(f"{pad}    {starlark_str(k)}: {starlark_str(v)},\n" for k, v in items)
    return f"{{\n{inner}{pad}}}"


def render_dict_of_lists(items: list[tuple[str, list[str]]], indent: int = 0) -> str:
    if not items:
        return "{}"
    pad = " " * indent
    inner = "".join(
        f"{pad}    {starlark_str(k)}: [{', '.join(starlark_str(x) for x in v)}],\n" for k, v in items
    )
    return f"{{\n{inner}{pad}}}"


def lint_levels(table: dict) -> list[tuple[str, str]]:
    """`[workspace.lints.<tool>]` as `(lint, level)` pairs in Cargo's application order.

    Cargo applies lower priorities first so that later entries override them;
    ties keep manifest order.
    """
    entries = []
    for index, (name, value) in enumerate(table.items()):
        if isinstance(value, dict):
            level, priority = value["level"], value.get("priority", 0)
        else:
            level, priority = value, 0
        entries.append((priority, index, name, level))
    return [(name, level) for _, _, name, level in sorted(entries)]


def render_workspace_bzl(project: Project, root: dict) -> str:
    """`<project>/bazel/workspace.bzl`: values every crate inherits from the root manifest."""
    workspace = workspace_table(root)
    pkg = workspace["package"]
    lints = workspace.get("lints", {})
    rust_lints = lints.get("rust", {})
    # cfgs rustc must not warn about: the ones cargo always declares plus
    # `unexpected_cfgs = { check-cfg = [...] }`. Features are declared per crate.
    check_cfg: list[tuple[str, list[str]]] = [("docsrs", []), ("test", [])]
    for value in rust_lints.values():
        if isinstance(value, dict):
            for spec in value.get("check-cfg", []):
                m = re.fullmatch(r"cfg\((\w+)(?:,\s*values\((.*)\))?\)", spec)
                if not m:
                    raise SystemExit(f"unsupported check-cfg spec: {spec}")
                values = [v.strip().strip('"') for v in m.group(2).split(",")] if m.group(2) else []
                check_cfg.append((m.group(1), values))

    lines = [
        HEADER,
        "\n",
        f'"""Workspace-wide values of the {project.name} project, derived from its root Cargo.toml."""\n\n',
        "# What every crate of the project inherits; see //bazel:rust.bzl.\n",
        "WORKSPACE = struct(\n",
        f"    name = {starlark_str(project.name)},\n",
        # A single-crate project's root manifest is the crate's own; None then.
        f"    manifest = {starlark_str(project.label('', 'Cargo.toml')) if 'workspace' in root else 'None'},\n",
        f"    lints = {starlark_str(project.label('bazel', 'lints'))},\n",
        f"    edition = {starlark_str(pkg['edition'])},\n",
        f"    version = {starlark_str(pkg['version']) if 'version' in pkg else 'None'},\n",
        ")\n\n",
        "# `[workspace.lints.*]`, each in Cargo's application order.\n",
        f"RUSTC_LINTS = {render_dict(lint_levels(rust_lints))}\n\n",
        f"RUSTC_CHECK_CFG = {render_dict_of_lists(check_cfg)}\n\n",
        f"CLIPPY_LINTS = {render_dict(lint_levels(lints.get('clippy', {})))}\n\n",
        f"RUSTDOC_LINTS = {render_dict(lint_levels(lints.get('rustdoc', {})))}\n",
    ]
    return "".join(lines)


MEMBER_DEPS_MODULE = SHADOW_ROOT / "member_deps.MODULE.bazel"


def render_member_deps_module(member_deps: list[MemberDeps]) -> str:
    """`bazel/cargo/member_deps.MODULE.bazel`: edges from external crates back into the tree."""
    lines = [
        HEADER,
        "\n",
        '"""Dependencies of external crates on workspace members (included from //MODULE.bazel).\n',
        "\n",
        "A `[patch.crates-io]` entry redirects every use of a crate to the tree, including\n",
        "uses by external crates. crate_universe leaves dependencies on workspace members\n",
        "out of the crates it renders, so they are added back here, one annotation per\n",
        "external crate, with the members' Bazel labels.\n",
        '"""\n',
        "\n",
        'crate = use_extension("@rules_rust//crate_universe:extensions.bzl", "crate")\n',
    ]
    for m in member_deps:
        lines.append("\n")
        lines.append("crate.annotation(\n")
        for attr, labels in m.attrs.items():
            lines.append(f"    {attr} = {render_list([starlark_str(l) for l in labels], indent=4)},\n")
        lines.append(f"    crate = {starlark_str(m.name)},\n")
        lines.append(f'    repositories = [{starlark_str(CRATES_REPO.lstrip("@"))}],\n')
        lines.append(f"    version = {starlark_str('=' + m.version)},\n")
        lines.append(")\n")
    return "".join(lines)


def render_project_build_file(project: Project) -> str:
    """`<project>/bazel/BUILD.bazel`: the lint config every crate of the project uses."""
    return (
        HEADER
        + "\n"
        + 'load("@rules_rust//rust:defs.bzl", "rust_lint_config")\n'
        + f'load("{project.label("bazel", "workspace.bzl")}", "CLIPPY_LINTS", "RUSTC_CHECK_CFG", "RUSTC_LINTS", "RUSTDOC_LINTS")\n\n'
        + 'package(default_visibility = ["//visibility:public"])\n\n'
        + f"# `[workspace.lints]` from {project.prefix}/Cargo.toml, applied to every crate with `[lints] workspace = true`.\n"
        + "rust_lint_config(\n"
        + '    name = "lints",\n'
        + "    clippy = CLIPPY_LINTS,\n"
        + "    rustc = RUSTC_LINTS,\n"
        + "    rustc_check_cfg = RUSTC_CHECK_CFG,\n"
        + "    rustdoc = RUSTDOC_LINTS,\n"
        + ")\n"
    )


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify generated files are up to date")
    args = parser.parse_args()

    projects = discover_projects()
    roots = {p.name: load_root_manifest(p) for p in projects}
    members = [m for p in projects for m in collect_members(p, project_metadata(p))]
    shadow = shadow_workspace(projects, roots, members)

    # The Bazel Cargo workspace must be on disk before Cargo can be asked about it.
    stale = sync_shadow_tree(projects, shadow, args.check)
    outputs = dict(shadow.manifests)
    outputs[SHADOW_ROOT / "BUILD.bazel"] = render_shadow_build_file()
    outputs[BAZELIGNORE] = render_bazelignore(projects)
    stale += write_outputs(outputs, args.check)
    if args.check and stale:
        return report_stale(stale)

    meta, lock_changed = bazel_workspace_metadata(projects, shadow, args.check)
    if lock_changed:
        stale.append(SHADOW_ROOT / "Cargo.lock")
        if args.check:
            return report_stale(stale)

    crates = build_crates(projects, members, meta)
    outputs = {MEMBER_DEPS_MODULE: render_member_deps_module(external_member_deps(crates, meta))}
    for project in projects:
        outputs[project.workspace_bzl] = render_workspace_bzl(project, roots[project.name])
        outputs[project.root / "bazel" / "BUILD.bazel"] = render_project_build_file(project)
    for crate in crates.values():
        outputs[crate.project.root / crate.package_path / "BUILD.bazel"] = render_build_file(crate, crates)
    stale += write_outputs(outputs, args.check)

    if args.check and stale:
        return report_stale(stale)
    if not args.check:
        print(f"{len(projects)} project(s), {len(crates)} crates, {len(stale)} file(s) updated")
    return 0


def write_outputs(outputs: dict[Path, str], check: bool) -> list[Path]:
    stale = []
    for path, content in sorted(outputs.items()):
        current = path.read_text() if path.exists() else None
        if current == content:
            continue
        stale.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return stale


def report_stale(stale: list[Path]) -> int:
    print(f"Generated Bazel files are out of date; run {GENERATOR}:", file=sys.stderr)
    for path in stale:
        print(f"  {path.relative_to(WORKSPACE_ROOT)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
