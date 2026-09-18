#!/usr/bin/env python3
"""Generate Bazel BUILD files for every crate of every project's Cargo workspace.

Cargo manifests remain the source of truth. For each project (a directory of
the monorepo holding a Cargo workspace and a `bazel/project.toml`), this script
asks Cargo how it would resolve the workspace (`cargo metadata`) and renders,
for every workspace member, a `BUILD.bazel` that calls the macros in
`//bazel:rust.bzl`:

* a `crate_library`/`crate_proc_macro`/`crate_binary` per `[lib]`/`[[bin]]` target,
* a `crate_unit_test` for the crate's `#[cfg(test)]` tests,
* a `crate_integration_test` per `tests/*.rs` target,
* a `crate_build_script` when the crate has a `build.rs`.

External crates are referenced through the project's crate_universe
repository (`@<project>_crates`), rendered from the same manifests, so every
dependency edge Bazel sees is one Cargo resolved.

Both this script and crate_universe read a project's Cargo workspace through
its *Bazel shadow workspace* in `<project>/bazel/cargo/` (see
`shadow_workspace`): a directory of symlinks into the real workspace in which

* the root manifest is a copy that carries a digest of every member manifest
  (so that crate_universe notices member edits) and whose `path` dependencies
  pointing outside the project (`../alloy/...`) are rewritten to go through a
  symlink inside the shadow, where crate_universe can follow them;
* the manifests listed in the project's `disabled_features` are replaced by
  copies that drop those features from `default`. Cargo resolves features
  workspace-wide from every member's default features, and crate_universe
  offers no way to subtract a feature, so this is the only place where "what
  Bazel builds" can diverge from `cargo build`.

Usage (from anywhere):
    python3 bazel/generate.py          # rewrite generated files
    python3 bazel/generate.py --check  # exit 1 if any generated file is stale
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    crates_repo: str  # crate_universe repository, e.g. "@reth_crates"
    # Cargo features that are enabled by default but are *not* built by Bazel.
    # Keys are package names, values map feature -> reason. Each reason must
    # explain why the feature cannot (yet) be built hermetically. The package's
    # manifest is shadowed in `bazel/cargo/`.
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
        return self.root / "bazel" / "cargo"

    @property
    def workspace_bzl(self) -> Path:
        return self.root / "bazel" / "workspace.bzl"

    def label(self, package_path: str, target: str = "") -> str:
        """Workspace-absolute label of `target` in the project-relative `package_path`."""
        pkg = f"{self.prefix}/{package_path}".strip("/")
        return f"//{pkg}:{target}" if target else f"//{pkg}"

    @staticmethod
    def load(config_path: Path) -> Project:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
        root = config_path.parents[1]
        known = {
            "crates_repo",
            "disabled_features",
            "build_scripts",
            "compile_data",
            "exported_files",
            "process_per_test",
            "test_tags",
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
            crates_repo=cfg["crates_repo"],
            disabled_features=cfg.get("disabled_features", {}),
            build_scripts=build_scripts,
            compile_data=cfg.get("compile_data", {}),
            exported_files=cfg.get("exported_files", {}),
            process_per_test=cfg.get("process_per_test", []),
            test_tags=cfg.get("test_tags", {}),
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


def load_root_manifest(project: Project) -> dict:
    with open(project.root / "Cargo.toml", "rb") as f:
        return tomllib.load(f)


def member_dirs(project: Project, root: dict) -> list[Path]:
    """Project-relative directories of the workspace members, `members` globs expanded."""
    excluded = {Path(e.rstrip("/")) for e in root["workspace"].get("exclude", [])}
    dirs = []
    for member in root["workspace"]["members"]:
        matches = sorted(project.root.glob(member.rstrip("/"))) if "*" in member else [project.root / member.rstrip("/")]
        for path in matches:
            rel = path.relative_to(project.root)
            if rel in excluded or not (path / "Cargo.toml").exists():
                if "*" in member:
                    continue
                sys.exit(f"{project.name}: workspace member without Cargo.toml: {member}")
            dirs.append(rel)
    return sorted(set(dirs))


# --- shadow workspace ----------------------------------------------------------


@dataclass
class ShadowWorkspace:
    """The Bazel view of the Cargo workspace, materialized under `<project>/bazel/cargo/`.

    `links` maps a path below `bazel/cargo/` to the relative symlink target that
    must be there; `manifests` maps the shadowed manifests (including the root)
    to their rendered contents. Everything else in the directory is a symlink
    into the real tree, so the shadow has the same layout as the project and
    Cargo reports the same project-relative package paths for both.
    """

    project: Project
    links: dict[Path, str]
    manifests: dict[Path, str]

    @property
    def root_manifest(self) -> Path:
        return self.project.shadow_root / "Cargo.toml"

    def manifest_labels(self) -> list[str]:
        rel = sorted(p.relative_to(self.project.shadow_root).as_posix() for p in self.manifests)
        return [self.project.label("bazel/cargo", p) for p in rel]


def relative_link(link: Path, target: Path) -> str:
    return os.path.relpath(target, link.parent)


def members_digest(project: Project, manifests: list[Path]) -> str:
    """Hash of every member manifest, so that crate_universe notices member edits.

    crate_universe only re-resolves when a manifest it was given changes. It is
    given the shadow root and the shadowed manifests, not the members behind
    the symlinks, so the members' contents are folded into the shadow root
    manifest through this digest instead.
    """
    h = hashlib.sha256()
    for manifest in sorted(manifests):
        h.update(str(manifest.relative_to(project.root)).encode())
        h.update(b"\0")
        h.update(manifest.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def generated_manifest_header(project: Project, rel: Path, what: str) -> str:
    return (
        f"# GENERATED FILE - DO NOT EDIT. Regenerate with `python3 {GENERATOR}`.\n"
        "#\n"
        f"# Bazel-only shadow of `{project.prefix}/{rel.as_posix()}`. It is identical to the real\n"
        f"# manifest except that{what}"
    )


def render_shadow_root_manifest(project: Project, digest: str) -> tuple[str, dict[str, Path]]:
    """Copy of the root manifest with escaping `path`s rewritten and the members digest.

    Returns the rendered text and the symlinks it relies on: shadow-relative
    link name -> absolute target. A dependency on `../alloy/crates/x` becomes
    `alloy/crates/x` next to a link `alloy -> <workspace>/alloy`, so that Cargo
    (and crate_universe, which only sees the children of the shadow root) can
    reach it without leaving the shadow.
    """
    real = project.root / "Cargo.toml"
    text = real.read_text()
    original = tomllib.loads(text)

    links: dict[str, Path] = {}

    def rewrite(m: re.Match) -> str:
        path = m.group(1)
        if not path.startswith("../"):
            return m.group(0)
        target = (project.root / path).resolve()
        if WORKSPACE_ROOT not in target.parents:
            sys.exit(f"{project.name}: path dependency {path!r} leaves the monorepo")
        rel = target.relative_to(WORKSPACE_ROOT)
        link, rest = rel.parts[0], Path(*rel.parts[1:]).as_posix()
        links[link] = WORKSPACE_ROOT / link
        return f'path = "{link}/{rest}"' if rest else f'path = "{link}"'

    rewritten = re.sub(r'\bpath\s*=\s*"([^"]+)"', rewrite, text)

    what = "\n".join(
        [
            ":",
            "# * `path` dependencies on other projects of the monorepo go through the",
            "#   symlinks next to this file instead of `../`, so that crate_universe",
            "#   (which mirrors this directory) can follow them;",
            "# * it carries a digest of every workspace member manifest (below), so that",
            "#   crate_universe re-resolves the external crate graph when a member's",
            "#   Cargo.toml changes (it only watches the manifests it is given).",
            "#",
            f"# Cargo builds are unaffected; see {GENERATOR}.",
            "",
            "",
        ]
    )
    header = generated_manifest_header(project, Path("Cargo.toml"), what)
    section = "package" if "package" in original else "workspace"
    footer = (
        "\n# Digest of every workspace member manifest; see the header.\n"
        f"[{section}.metadata.bazel]\n"
        f'workspace-members-digest = "{digest}"\n'
    )
    out = header + rewritten + footer

    # The rewrite is textual; check against the parsed manifests that it
    # touched exactly the escaping paths and nothing else.
    rendered = tomllib.loads(out)
    del rendered[section]["metadata"]["bazel"]
    if not rendered[section]["metadata"]:
        del rendered[section]["metadata"]
    original_rest, original_paths = split_paths(original)
    rendered_rest, rendered_paths = split_paths(rendered)
    expected = {(p[len("../") :] if p.startswith("../") else p) for p in original_paths}
    if rendered_paths != expected or any(p.startswith("../") for p in expected):
        sys.exit(f"{project.name}: failed to rewrite path dependencies in the shadow root manifest")
    if rendered_rest != original_rest:
        sys.exit(f"{project.name}: shadow root manifest differs from the real one beyond `path` values")
    return out, links


def split_paths(manifest: dict) -> tuple[dict, set[str]]:
    """The manifest with every `path = ...` value blanked, and those values."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "path" and isinstance(v, str):
                    found.add(v)
                    out[k] = None
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(manifest), found


def render_shadow_member_manifest(project: Project, real: Path, disabled: dict[str, str]) -> str:
    """Copy of `real` with `disabled` removed from `[features] default`."""
    text = real.read_text()
    manifest = tomllib.loads(text)
    name = manifest["package"]["name"]
    default = manifest.get("features", {}).get("default", [])
    unknown = set(disabled) - set(default)
    if unknown:
        sys.exit(f"{name}: disabled_features lists non-default features {sorted(unknown)}")

    # Edit the text rather than re-serializing the TOML so the shadow stays a
    # readable diff of the real manifest.
    m = re.search(r"^default = \[\n(.*?)^\]\n", text, re.S | re.M)
    if m is None:
        sys.exit(f"{name}: expected a multi-line `default = [...]` feature list in {real}")
    removed = {f'"{f}",' for f in disabled}
    kept = [line for line in m.group(1).splitlines(keepends=True) if line.strip() not in removed]
    text = text[: m.start(1)] + "".join(kept) + text[m.end(1) :]

    reasons = "".join(f"#   - {f}: {why}\n" for f, why in disabled.items())
    what = (
        " these features are removed from `default`, because\n"
        "# Bazel cannot build them hermetically yet:\n"
        f"{reasons}"
        "#\n"
        f"# Cargo builds are unaffected; see {project.prefix}/bazel/project.toml (disabled_features).\n\n"
    )
    out = generated_manifest_header(project, real.relative_to(project.root), what) + text

    rendered = tomllib.loads(out)
    expected = [f for f in default if f not in disabled]
    if rendered["features"].get("default", []) != expected:
        sys.exit(f"{name}: failed to rewrite `default` features in shadow manifest")
    return out


def shadow_workspace(project: Project, root: dict, members: list[Path]) -> ShadowWorkspace:
    shadow_root = project.shadow_root
    packages = {}
    for member in members:
        with open(project.root / member / "Cargo.toml", "rb") as f:
            packages[tomllib.load(f)["package"]["name"]] = member
    unknown_crates = set(project.disabled_features) - set(packages)
    if unknown_crates:
        sys.exit(f"{project.name}: disabled_features lists unknown crates {sorted(unknown_crates)}")
    shadowed = {packages[name]: name for name in project.disabled_features}

    links: dict[Path, str] = {}
    manifests: dict[Path, str] = {}

    def link(rel: Path, target: Path | None = None) -> None:
        links[shadow_root / rel] = relative_link(shadow_root / rel, target or project.root / rel)

    digest = members_digest(project, [project.root / m / "Cargo.toml" for m in members])
    manifests[shadow_root / "Cargo.toml"], external = render_shadow_root_manifest(project, digest)
    link(Path("Cargo.lock"))
    for name, target in external.items():
        if any(Path(name) == Path(m.parts[0]) for m in members):
            sys.exit(f"{project.name}: path dependency link {name!r} collides with a workspace member directory")
        link(Path(name), target)

    for member in members:
        if member in shadowed:
            # Real sources, shadow manifest. BUILD.bazel is left out so Bazel
            # never sees a second copy of the package.
            for entry in sorted((project.root / member).iterdir()):
                if entry.name not in ("Cargo.toml", "BUILD.bazel"):
                    link(member / entry.name)
            manifests[shadow_root / member / "Cargo.toml"] = render_shadow_member_manifest(
                project, project.root / member / "Cargo.toml", project.disabled_features[shadowed[member]]
            )
            continue
        # Link the shortest prefix of the member path that contains no shadowed
        # member, e.g. `crates` for `crates/foo` but `bin/reth-bb` next to the
        # shadowed `bin/reth`.
        for depth in range(1, len(member.parts) + 1):
            prefix = Path(*member.parts[:depth])
            if not any(prefix in s.parents or prefix == s for s in shadowed):
                link(prefix)
                break
    return ShadowWorkspace(project=project, links=links, manifests=manifests)


def sync_shadow_links(shadow: ShadowWorkspace, check: bool) -> list[Path]:
    """Create/replace the shadow symlinks; return the ones that were wrong."""
    stale = []
    for link, target in shadow.links.items():
        if link.is_symlink() and os.readlink(link) == target:
            continue
        stale.append(link)
        if check:
            continue
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                sys.exit(f"{link} is a real directory, expected a symlink; remove it by hand")
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return stale


def render_shadow_build_file(shadow: ShadowWorkspace) -> str:
    files = [l.split(":", 1)[1] for l in shadow.manifest_labels()] + ["Cargo.lock"]
    return (
        HEADER
        + "\n"
        + f"# The Cargo workspace as seen by Bazel; see {GENERATOR}.\n"
        + f"exports_files({render_list([starlark_str(f) for f in sorted(files)], 0)})\n"
    )


def render_bazelignore(shadows: list[ShadowWorkspace]) -> str:
    """Keep Bazel from discovering packages twice through the shadow symlinks."""
    text = BAZELIGNORE.read_text()
    begin = text.index(BAZELIGNORE_BEGIN) + len(BAZELIGNORE_BEGIN)
    end = text.index(BAZELIGNORE_END)
    dirs = sorted(
        str(link.relative_to(WORKSPACE_ROOT))
        for shadow in shadows
        for link, target in shadow.links.items()
        if (link.parent / target).is_dir()
    )
    return text[:begin] + "".join(f"{d}\n" for d in dirs) + text[end:]


def cargo_metadata(shadow: ShadowWorkspace) -> dict:
    cmd = [
        "cargo",
        "metadata",
        "--format-version",
        "1",
        "--locked",
        "--manifest-path",
        str(shadow.root_manifest),
    ]
    result = subprocess.run(cmd, cwd=shadow.project.root, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def build_crates(project: Project, meta: dict) -> dict[str, Crate]:
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    members = set(meta["workspace_members"])
    root = Path(meta["workspace_root"])

    def lib_target(pkg: dict) -> dict | None:
        for t in pkg["targets"]:
            if any(k in ("lib", "rlib", "proc-macro") for k in t["kind"]):
                return t
        return None

    # External crates that workspace members depend on at more than one version
    # only get `@<repo>//:<name>-<version>` aliases, never a bare `@<repo>//:<name>`.
    external_versions: dict[str, set[str]] = defaultdict(set)
    for member in members:
        for dep in nodes[member]["deps"]:
            if dep["pkg"] not in members:
                pkg = packages[dep["pkg"]]
                external_versions[pkg["name"]].add(pkg["version"])

    crates: dict[str, Crate] = {}
    for member in members:
        pkg = packages[member]
        with open(pkg["manifest_path"], "rb") as f:
            manifest = tomllib.load(f)
        package_path = Path(pkg["manifest_path"]).parent.relative_to(root).as_posix()
        crate = Crate(
            name=pkg["name"],
            ident=crate_name_to_ident(pkg["name"]),
            package_path=package_path,
            features=sorted(nodes[member]["features"]),
            declared_features=sorted(pkg["features"]),
            workspace_lints=bool(manifest.get("lints", {}).get("workspace", False)),
            targets=[
                Target(
                    kind=t["kind"][0],
                    name=t["name"],
                    src_path=str(Path(t["src_path"]).relative_to(Path(pkg["manifest_path"]).parent)),
                    required_features=t.get("required-features", []),
                )
                for t in pkg["targets"]
            ],
            label="",
        )
        crate.label = project.label(package_path, crate.lib_target)
        crates[member] = crate

    for member, crate in crates.items():
        activated = activated_optional_deps(packages[member], crate.features)
        for dep in nodes[member]["deps"]:
            if dep["pkg"] == member:
                # A crate listing itself in `[dev-dependencies]` to enable extra
                # features for its tests; unified features already cover that,
                # and the test targets link the crate explicitly.
                continue
            dep_pkg = packages[dep["pkg"]]
            lib = lib_target(dep_pkg)
            if lib is None:
                continue  # binary-only dependency (artifact deps are not used)
            proc_macro = "proc-macro" in lib["kind"]
            if dep["pkg"] in members:
                dep_label = crates[dep["pkg"]].label
                lib_name = crates[dep["pkg"]].ident
            else:
                # crate_universe names the alias after the `package = "..."`
                # rename when there is one (`criterion`, not
                # `codspeed-criterion-compat`), and suffixes the version when a
                # member depends on several versions of the crate.
                renames = {
                    crate_name_to_ident(d["rename"]): d["rename"]
                    for d in packages[member]["dependencies"]
                    if d["name"] == dep_pkg["name"] and d["rename"]
                }
                alias_name = renames.get(dep["name"], dep_pkg["name"])
                if len(external_versions[dep_pkg["name"]]) > 1:
                    dep_label = f"{project.crates_repo}//:{alias_name}-{dep_pkg['version']}"
                else:
                    dep_label = f"{project.crates_repo}//:{alias_name}"
                lib_name = crate_name_to_ident(lib["name"])
            alias = dep["name"] if dep["name"] != lib_name else None
            for dk in dep["dep_kinds"]:
                if is_disabled_optional_dep(packages[member], dep["name"], dk["kind"], activated):
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


def render_build_file(project: Project, crate: Crate, crates: dict[str, Crate]) -> str:
    lib = crate.lib
    lib_rule = "crate_proc_macro" if lib and lib.kind == "proc-macro" else "crate_library"
    bins = [t for t in crate.targets if t.kind == "bin"]
    tests = [t for t in crate.targets if t.kind == "test"]
    build_script = crate.target("custom-build")
    # `#[test_fuzz]`-instrumented tests need extra runtime plumbing (see rust.bzl).
    test_fuzz = "True" if any(d.label == f"{project.crates_repo}//:test-fuzz" for d in crate.dev) else None
    # Tests that launch nodes retain GiBs per node for the process lifetime
    # (see bazel/process_per_test.bzl); run them one process per test.
    per_process_labels = {c.label for c in crates.values() if c.name in project.process_per_test}
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
    out.append(f'load("{RUST_BZL}", {", ".join(starlark_str(l) for l in sorted(loads))})\n')
    out.append(f'load("{project.label("bazel", "workspace.bzl")}", "WORKSPACE")\n\n')
    out.append(f"FEATURES = {render_list([starlark_str(f) for f in crate.features], 0)}\n\n")
    out.append(
        f"DECLARED_FEATURES = {render_list([starlark_str(f) for f in crate.declared_features], 0)}\n\n"
    )
    if crate.name in project.exported_files:
        exported = project.exported_files[crate.name]
        out.append(f"exports_files({render_list([starlark_str(f) for f in exported], 0)})\n\n")
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
                    ("crate", starlark_str(f":{unit_test_crate}")),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(crate.dev, proc_macro=False)),
                    ("proc_macro_deps", render_deps(crate.dev, proc_macro=True)),
                    ("aliases", render_aliases(crate.dev)),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_tags(project.tags_for(crate.name, "crate"))),
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
                    ("workspace", "WORKSPACE"),
                    ("crate_root", starlark_str(t.src_path)),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(deps, proc_macro=False)),
                    ("proc_macro_deps", render_deps(deps, proc_macro=True)),
                    ("aliases", render_aliases(deps)),
                    ("compile_data", render_list([starlark_str(l) for l in test_compile_data]) if test_compile_data else None),
                    ("data", render_list(bin_data) if bin_data else None),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_tags(project.tags_for(crate.name, t.name))),
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
    pkg = root["workspace"]["package"]
    lints = root["workspace"].get("lints", {})
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
        f"    manifest = {starlark_str(project.label('', 'Cargo.toml'))},\n",
        f"    lints = {starlark_str(project.label('bazel', 'lints'))},\n",
        f"    edition = {starlark_str(pkg['edition'])},\n",
        f"    version = {starlark_str(pkg['version'])},\n",
        ")\n\n",
        "# `[workspace.lints.*]`, each in Cargo's application order.\n",
        f"RUSTC_LINTS = {render_dict(lint_levels(rust_lints))}\n\n",
        f"RUSTC_CHECK_CFG = {render_dict_of_lists(check_cfg)}\n\n",
        f"CLIPPY_LINTS = {render_dict(lint_levels(lints.get('clippy', {})))}\n\n",
        f"RUSTDOC_LINTS = {render_dict(lint_levels(lints.get('rustdoc', {})))}\n",
    ]
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
    shadows = {p.name: shadow_workspace(p, roots[p.name], member_dirs(p, roots[p.name])) for p in projects}

    # The shadow workspaces must be in place before Cargo can be asked about them.
    stale: list[Path] = []
    outputs: dict[Path, str] = {}
    for shadow in shadows.values():
        stale += sync_shadow_links(shadow, args.check)
        outputs.update(shadow.manifests)
        outputs[shadow.project.shadow_root / "BUILD.bazel"] = render_shadow_build_file(shadow)
    outputs[BAZELIGNORE] = render_bazelignore(list(shadows.values()))
    stale += write_outputs(outputs, args.check)
    if args.check and stale:
        return report_stale(stale)

    outputs = {}
    total = 0
    for project in projects:
        crates = build_crates(project, cargo_metadata(shadows[project.name]))
        total += len(crates)
        outputs[project.workspace_bzl] = render_workspace_bzl(project, roots[project.name])
        outputs[project.root / "bazel" / "BUILD.bazel"] = render_project_build_file(project)
        for crate in crates.values():
            outputs[project.root / crate.package_path / "BUILD.bazel"] = render_build_file(project, crate, crates)
    stale += write_outputs(outputs, args.check)

    if args.check and stale:
        return report_stale(stale)
    if not args.check:
        print(f"{len(projects)} project(s), {total} crates, {len(stale)} file(s) updated")
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
