#!/usr/bin/env python3
"""Generate Bazel BUILD files for every crate in the Cargo workspace.

Cargo manifests remain the source of truth. This script asks Cargo how it would
resolve the workspace (`cargo metadata`) and renders, for every workspace member,
a `BUILD.bazel` that calls the macros in `bazel/rust.bzl`:

* a `reth_library`/`reth_binary` per `[lib]`/`[[bin]]` target,
* a `reth_unit_test` for the crate's `#[cfg(test)]` tests,
* a `reth_integration_test` per `tests/*.rs` target,
* a `reth_build_script` when the crate has a `build.rs`.

External crates are referenced through the `@reth_crates` repository rendered
by crate_universe from the same manifests, so every dependency edge Bazel sees
is one Cargo resolved.

reth is one project of the monotempo monorepo, whose root is the Bazel
workspace; every label this script emits is therefore prefixed with `//reth/`.

Both this script and crate_universe read the Cargo workspace through the
*Bazel shadow workspace* in `bazel/cargo/` (see `render_shadow_manifest`): a
directory of symlinks into the real workspace in which the manifests listed in
`BAZEL_DISABLED_FEATURES` are replaced by copies that drop those features from
`default`. Cargo resolves features workspace-wide from every member's default
features, and crate_universe offers no way to subtract a feature, so this is
the only place where "what Bazel builds" can diverge from `cargo build`.

Usage (from anywhere):
    python3 reth/scripts/bazel/generate.py          # rewrite generated files
    python3 reth/scripts/bazel/generate.py --check  # exit 1 if any generated file is stale
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

# The reth Cargo workspace (this project) ...
REPO_ROOT = Path(__file__).resolve().parents[2]
# ... is a subdirectory of the monorepo, which is the Bazel workspace. Labels
# are workspace-relative, so every one the generator emits carries the prefix.
WORKSPACE_ROOT = next(p for p in REPO_ROOT.parents if (p / "MODULE.bazel").exists())
PREFIX = REPO_ROOT.relative_to(WORKSPACE_ROOT).as_posix()
# crate_universe repository holding reth's external crates (see reth.MODULE.bazel).
CRATES_REPO = "@reth_crates"

WORKSPACE_BZL = REPO_ROOT / "bazel" / "workspace.bzl"
SHADOW_ROOT = REPO_ROOT / "bazel" / "cargo"
BAZELIGNORE = WORKSPACE_ROOT / ".bazelignore"
GENERATOR = Path(__file__).resolve().relative_to(WORKSPACE_ROOT).as_posix()
BAZELIGNORE_BEGIN = f"# BEGIN GENERATED ({GENERATOR})\n"
BAZELIGNORE_END = "# END GENERATED\n"

HEADER = f"""# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 {GENERATOR}` after changing Cargo.toml.
"""


def label(package_path: str, target: str = "") -> str:
    """Workspace-absolute label of `target` in the reth-relative `package_path`."""
    pkg = f"{PREFIX}/{package_path}".strip("/")
    return f"//{pkg}:{target}" if target else f"//{pkg}"


# Cargo features that are enabled by default but are *not* built by Bazel.
#
# Bazel compiles every workspace crate with the feature set Cargo resolves for
# `cargo build --workspace`, except for the features listed here. Each entry
# must explain why the feature cannot (yet) be built hermetically. Keys are
# package names; the package's manifest is shadowed in `bazel/cargo/`.
BAZEL_DISABLED_FEATURES: dict[str, dict[str, str]] = {
    "reth": {
        # revmc -> llvm-sys: links LLVM 22 statically and needs `llvm-config`
        # from a system LLVM install. Needs a hermetic LLVM distribution with
        # static libraries before it can be enabled.
        "jit": "requires system LLVM (llvm-sys)",
        # rug -> gmp-mpfr-sys: builds GMP with autotools (m4, make) at build time.
        "gmp": "requires autotools GMP build (gmp-mpfr-sys)",
    },
}

# Per-crate build-script inputs and environment. Keys are crate names.
# Rendered verbatim as a Starlark expression.
BUILD_SCRIPT_DATA: dict[str, str] = {
    # build.rs compiles `libmdbx/mdbx.c` and runs bindgen over `libmdbx/mdbx.h`
    # with libclang from the hermetic LLVM toolchain.
    "reth-mdbx-sys": (
        'glob(["libmdbx/**"]) + [\n'
        f'        "{label("bazel", "clang_builtin_headers")}",\n'
        '        "@llvm_toolchain_llvm//:libclang",\n'
        "    ]"
    ),
}
BUILD_SCRIPT_ENV: dict[str, dict[str, str]] = {
    "reth-mdbx-sys": {
        "LIBCLANG_PATH": "$(execpath @llvm_toolchain_llvm//:libclang)",
        # The prebuilt libclang cannot find its own resource dir; see //reth/bazel:clang_builtin_headers.
        "BINDGEN_EXTRA_CLANG_ARGS": f"-resource-dir=$(execpath {label('bazel', 'clang_builtin_headers')})/..",
    },
    # vergen cannot see `.git` inside the sandbox (and stamping every build with
    # the current commit would defeat caching). Emit stable placeholder values.
    "reth-node-core": {
        "VERGEN_IDEMPOTENT": "1",
    },
}

# Extra compile-time inputs for targets that `include_*!` files from outside
# their own crate directory. Maps crate name -> {target -> labels}, where
# target is an integration test's name, or "crate" for the lib, binaries and
# unit tests (files inside the crate directory are globbed automatically).
E2E_GENESIS = f'"{label("crates/e2e-test-utils", "src/testsuite/assets/genesis.json")}"'
EXTRA_COMPILE_DATA: dict[str, dict[str, list[str]]] = {
    "example-exex-test": {"crate": [E2E_GENESIS]},
    "reth-engine-tree": {"e2e_testsuite": [E2E_GENESIS]},
    "reth-node-ethereum": {"e2e": [f'"{label("testing/prestate", "tx-selfdestruct-prestate.json")}"']},
    "reth-trie-db": {"proof": [f'"{label("crates/trie/trie", "testdata/proof-genesis.json")}"']},
}

# Files a crate must export for other packages (see EXTRA_COMPILE_DATA).
EXPORTED_FILES: dict[str, list[str]] = {
    "reth-e2e-test-utils": ["src/testsuite/assets/genesis.json"],
    "reth-trie": ["testdata/proof-genesis.json"],
}

# The crate every node-launching test is built on.
E2E_TEST_UTILS = "reth-e2e-test-utils"
E2E_TEST_UTILS_LABEL = label("crates/e2e-test-utils", "reth_e2e_test_utils")

# Tags applied to a crate's test targets. `manual` keeps a test out of `bazel test //...`.
TEST_TAGS: dict[str, list[str]] = {
    # Runs the ethereum/tests fixtures, which are a git submodule that is not
    # checked out by default; cargo CI excludes it as well.
    "ef-tests": ["manual"],
    # `tests/it/history.rs` downloads era1 files from era.ithaca.xyz.
    # `requires-network` lifts the sandbox's network block when it is run
    # explicitly; `manual` keeps the default test run hermetic.
    "reth-era-utils": ["manual", "requires-network"],
}

# `cfg(...)` expressions on platform-specific dependencies -> Bazel config settings.
CFG_TO_CONDITIONS: dict[str, list[str]] = {
    "cfg(unix)": ["@platforms//os:linux", "@platforms//os:macos"],
    'cfg(target_os = "linux")': ["@platforms//os:linux"],
    'cfg(target_os = "macos")': ["@platforms//os:macos"],
    "cfg(windows)": ["@platforms//os:windows"],
}


def crate_name_to_ident(name: str) -> str:
    return name.replace("-", "_")


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
    package_path: str  # repo-relative directory, e.g. "crates/storage/db"
    features: list[str]  # features enabled in the Bazel build
    declared_features: list[str]  # every feature in `[features]`, for `--check-cfg`
    workspace_lints: bool  # `[lints] workspace = true`
    targets: list[Target]
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

    @property
    def label(self) -> str:
        return label(self.package_path, self.lib_target)

    def target(self, kind: str) -> Target | None:
        return next((t for t in self.targets if t.kind == kind), None)


def load_root_manifest() -> dict:
    with open(REPO_ROOT / "Cargo.toml", "rb") as f:
        return tomllib.load(f)


def member_manifests(root: dict) -> list[Path]:
    manifests = []
    for member in root["workspace"]["members"]:
        path = REPO_ROOT / member.rstrip("/") / "Cargo.toml"
        if not path.exists():
            sys.exit(f"workspace member without Cargo.toml: {member}")
        manifests.append(path)
    return sorted(manifests)


# --- shadow workspace ----------------------------------------------------------


@dataclass
class ShadowWorkspace:
    """The Bazel view of the Cargo workspace, materialized under `bazel/cargo/`.

    `links` maps a path below `bazel/cargo/` to the relative symlink target that
    must be there; `manifests` maps the shadowed manifests to their rendered
    contents. Everything else in the directory is a symlink into the real tree,
    so the shadow has the same layout as the repository and Cargo reports the
    same repo-relative package paths for both.
    """

    links: dict[Path, str]
    manifests: dict[Path, str]

    @property
    def root_manifest(self) -> Path:
        return SHADOW_ROOT / "Cargo.toml"

    def manifest_labels(self) -> list[str]:
        rel = [p.relative_to(SHADOW_ROOT) for p in self.manifests]
        return [label("bazel/cargo", "Cargo.toml")] + [label("bazel/cargo", p.as_posix()) for p in sorted(rel)]


def relative_link(link: Path, target: Path) -> str:
    return os.path.relpath(target, link.parent)


def members_digest(manifests: list[Path]) -> str:
    """Hash of every member manifest, so that crate_universe notices member edits.

    crate_universe only re-resolves when a manifest it was given changes. It is
    given the shadow root and the shadowed manifests, not the ~140 members
    behind the symlinks, so the members' contents are folded into a shadowed
    manifest through this digest instead.
    """
    h = hashlib.sha256()
    for manifest in sorted(manifests):
        h.update(str(manifest.relative_to(REPO_ROOT)).encode())
        h.update(b"\0")
        h.update(manifest.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def render_shadow_manifest(real: Path, disabled: dict[str, str], digest: str) -> str:
    """Copy of `real` with `disabled` removed from `[features] default`."""
    text = real.read_text()
    manifest = tomllib.loads(text)
    name = manifest["package"]["name"]
    default = manifest.get("features", {}).get("default", [])
    unknown = set(disabled) - set(default)
    if unknown:
        sys.exit(f"{name}: BAZEL_DISABLED_FEATURES lists non-default features {sorted(unknown)}")

    # Edit the text rather than re-serializing the TOML so the shadow stays a
    # readable diff of the real manifest.
    m = re.search(r"^default = \[\n(.*?)^\]\n", text, re.S | re.M)
    if m is None:
        sys.exit(f"{name}: expected a multi-line `default = [...]` feature list in {real}")
    removed = {f'"{f}",' for f in disabled}
    kept = [line for line in m.group(1).splitlines(keepends=True) if line.strip() not in removed]
    text = text[: m.start(1)] + "".join(kept) + text[m.end(1) :]

    rel = real.relative_to(REPO_ROOT)
    reasons = "".join(f"#   - {f}: {why}\n" for f, why in disabled.items())
    header = (
        f"# GENERATED FILE - DO NOT EDIT. Regenerate with `python3 {GENERATOR}`.\n"
        "#\n"
        f"# Bazel-only shadow of `{rel}`. It is identical to the real manifest except\n"
        "# that these features are removed from `default`, because Bazel cannot\n"
        "# build them hermetically yet:\n"
        f"{reasons}"
        "#\n"
        f"# Cargo builds are unaffected; see {GENERATOR} (BAZEL_DISABLED_FEATURES).\n\n"
    )
    footer = (
        "\n# Digest of every workspace member manifest. crate_universe re-resolves the\n"
        "# external crate graph when this file changes, so a member edit that alters\n"
        "# feature resolution invalidates Cargo.Bazel.lock (see members_digest).\n"
        "[package.metadata.bazel]\n"
        f'workspace-members-digest = "{digest}"\n'
    )
    out = header + text + footer

    rendered = tomllib.loads(out)
    expected = [f for f in default if f not in disabled]
    if rendered["features"].get("default", []) != expected:
        sys.exit(f"{name}: failed to rewrite `default` features in shadow manifest")
    return out


def shadow_workspace(root: dict, manifests: list[Path]) -> ShadowWorkspace:
    packages = {}
    for manifest in manifests:
        with open(manifest, "rb") as f:
            packages[tomllib.load(f)["package"]["name"]] = manifest
    unknown_crates = set(BAZEL_DISABLED_FEATURES) - set(packages)
    if unknown_crates:
        sys.exit(f"BAZEL_DISABLED_FEATURES lists unknown crates {sorted(unknown_crates)}")

    shadowed = {packages[name].parent.relative_to(REPO_ROOT): name for name in BAZEL_DISABLED_FEATURES}
    digest = members_digest(manifests)

    links: dict[Path, str] = {}
    shadow_manifests: dict[Path, str] = {}

    def link(rel: Path) -> None:
        links[SHADOW_ROOT / rel] = relative_link(SHADOW_ROOT / rel, REPO_ROOT / rel)

    link(Path("Cargo.toml"))
    link(Path("Cargo.lock"))
    for member in root["workspace"]["members"]:
        rel = Path(member.rstrip("/"))
        if rel in shadowed:
            # Real sources, shadow manifest. BUILD.bazel is left out so Bazel
            # never sees a second copy of the package.
            for entry in sorted((REPO_ROOT / rel).iterdir()):
                if entry.name not in ("Cargo.toml", "BUILD.bazel"):
                    link(rel / entry.name)
            shadow_manifests[SHADOW_ROOT / rel / "Cargo.toml"] = render_shadow_manifest(
                REPO_ROOT / rel / "Cargo.toml", BAZEL_DISABLED_FEATURES[shadowed[rel]], digest
            )
            continue
        # Link the shortest prefix of the member path that contains no shadowed
        # member, e.g. `crates` for `crates/foo` but `bin/reth-bb` next to the
        # shadowed `bin/reth`.
        for depth in range(1, len(rel.parts) + 1):
            prefix = Path(*rel.parts[:depth])
            if not any(prefix in s.parents or prefix == s for s in shadowed):
                link(prefix)
                break
    return ShadowWorkspace(links=links, manifests=shadow_manifests)


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
    labels = [l.split(":", 1)[1] for l in shadow.manifest_labels()]
    return (
        HEADER
        + "\n"
        + f"# The Cargo workspace as seen by Bazel; see {GENERATOR}.\n"
        + f"exports_files({render_list([starlark_str(l) for l in labels], 0)})\n"
    )


def render_bazelignore(shadow: ShadowWorkspace) -> str:
    """Keep Bazel from discovering packages twice through the shadow symlinks."""
    text = BAZELIGNORE.read_text()
    begin = text.index(BAZELIGNORE_BEGIN) + len(BAZELIGNORE_BEGIN)
    end = text.index(BAZELIGNORE_END)
    dirs = sorted(
        str(link.relative_to(WORKSPACE_ROOT))
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
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def build_crates(meta: dict) -> dict[str, Crate]:
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
    # only get `@reth_crates//:<name>-<version>` aliases, never a bare `@reth_crates//:<name>`.
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
        crates[member] = Crate(
            name=pkg["name"],
            ident=crate_name_to_ident(pkg["name"]),
            package_path=str(Path(pkg["manifest_path"]).parent.relative_to(root)),
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
        )

    for member, crate in crates.items():
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
                    dep_label = f"{CRATES_REPO}//:{alias_name}-{dep_pkg['version']}"
                else:
                    dep_label = f"{CRATES_REPO}//:{alias_name}"
                lib_name = crate_name_to_ident(lib["name"])
            alias = dep["name"] if dep["name"] != lib_name else None
            for dk in dep["dep_kinds"]:
                cfg = dk.get("target")
                if cfg is not None and cfg not in CFG_TO_CONDITIONS:
                    sys.exit(f"{crate.name}: unsupported platform cfg {cfg!r} on {dep_pkg['name']}")
                rendered = Dep(label=dep_label, proc_macro=proc_macro, alias=alias, cfg=cfg)
                {None: crate.normal, "dev": crate.dev, "build": crate.build}[dk["kind"]].append(rendered)

    for crate in crates.values():
        crate.normal = sorted(set(crate.normal))
        crate.dev = sorted(set(crate.dev) - set(crate.normal))
        crate.build = sorted(set(crate.build))
    return crates


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


def render_build_file(crate: Crate) -> str:
    lib = crate.target("lib")
    bins = [t for t in crate.targets if t.kind == "bin"]
    tests = [t for t in crate.targets if t.kind == "test"]
    build_script = crate.target("custom-build")
    tags = TEST_TAGS.get(crate.name)
    # `#[test_fuzz]`-instrumented tests need extra runtime plumbing (see rust.bzl).
    test_fuzz = "True" if any(d.label == f"{CRATES_REPO}//:test-fuzz" for d in crate.dev) else None
    # Tests that launch nodes retain GiBs per node for the process lifetime
    # (see bazel/process_per_test.bzl); run them one process per test.
    launches_nodes = crate.name == E2E_TEST_UTILS or any(
        d.label == E2E_TEST_UTILS_LABEL for d in crate.normal + crate.dev
    )
    process_per_test = "True" if launches_nodes else None

    loads = ["reth_cargo_toml_env_vars"]
    if lib:
        loads.append("reth_library")
    if bins:
        loads.append("reth_binary")
    if lib or bins:
        loads.append("reth_unit_test")
    if tests:
        loads.append("reth_integration_test")
    if build_script:
        loads.append("reth_build_script")

    out = [HEADER, "\n"]
    out.append(f'load("{label("bazel", "rust.bzl")}", {", ".join(starlark_str(l) for l in sorted(loads))})\n\n')
    out.append(f"FEATURES = {render_list([starlark_str(f) for f in crate.features], 0)}\n\n")
    out.append(
        f"DECLARED_FEATURES = {render_list([starlark_str(f) for f in crate.declared_features], 0)}\n\n"
    )
    if crate.name in EXPORTED_FILES:
        out.append(f"exports_files({render_list([starlark_str(f) for f in EXPORTED_FILES[crate.name]], 0)})\n\n")
    out.append("reth_cargo_toml_env_vars()\n\n")

    build_script_label = None
    if build_script:
        build_script_label = f":{crate.ident}_build_script"
        env = BUILD_SCRIPT_ENV.get(crate.name, {})
        env_rendered = None
        if env:
            env_rendered = "{\n" + "".join(f"        {starlark_str(k)}: {starlark_str(v)},\n" for k, v in env.items()) + "    }"
        out.append(
            render_rule(
                "reth_build_script",
                [
                    ("name", starlark_str(f"{crate.ident}_build_script")),
                    ("crate_features", "FEATURES"),
                    ("deps", render_deps(crate.build, proc_macro=False)),
                    ("proc_macro_deps", render_deps(crate.build, proc_macro=True)),
                    ("aliases", render_aliases(crate.build)),
                    ("data", BUILD_SCRIPT_DATA.get(crate.name)),
                    ("build_script_env", env_rendered),
                ],
            )
        )
        out.append("\n")

    extra_compile_data = EXTRA_COMPILE_DATA.get(crate.name, {})
    normal_attrs = [
        ("crate_features", "FEATURES"),
        ("declared_features", "DECLARED_FEATURES"),
        ("workspace_lints", None if crate.workspace_lints else "False"),
        ("deps", render_deps(crate.normal, proc_macro=False)),
        ("proc_macro_deps", render_deps(crate.normal, proc_macro=True)),
        ("aliases", render_aliases(crate.normal)),
        ("compile_data", render_list(extra_compile_data["crate"]) if "crate" in extra_compile_data else None),
        ("build_script", starlark_str(build_script_label) if build_script_label else None),
    ]

    if lib:
        attrs = [("name", starlark_str(crate.lib_target))]
        if crate.lib_target != crate.ident:
            attrs.append(("crate_name", starlark_str(crate.ident)))
        if lib.src_path != "src/lib.rs":
            attrs.append(("crate_root", starlark_str(lib.src_path)))
        out.append(render_rule("reth_library", attrs + normal_attrs))
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
        out.append(render_rule("reth_binary", attrs + bin_attrs))
        out.append("\n")

    # Unit tests: the lib's `#[cfg(test)]` tests, or the bin's for bin-only crates.
    unit_test_crate = crate.lib_target if lib else (bins[0].name if bins else None)
    if unit_test_crate:
        out.append(
            render_rule(
                "reth_unit_test",
                [
                    ("name", starlark_str(f"{crate.ident}_test")),
                    ("crate", starlark_str(f":{unit_test_crate}")),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(crate.dev, proc_macro=False)),
                    ("proc_macro_deps", render_deps(crate.dev, proc_macro=True)),
                    ("aliases", render_aliases(crate.dev)),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_list([starlark_str(t) for t in tags]) if tags else None),
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
                "reth_integration_test",
                [
                    ("name", starlark_str(f"{crate.ident}_{crate_name_to_ident(t.name)}_test")),
                    ("crate_root", starlark_str(t.src_path)),
                    ("crate_features", "FEATURES"),
                    ("declared_features", "DECLARED_FEATURES"),
                    ("workspace_lints", None if crate.workspace_lints else "False"),
                    ("deps", render_deps(deps, proc_macro=False)),
                    ("proc_macro_deps", render_deps(deps, proc_macro=True)),
                    ("aliases", render_aliases(deps)),
                    ("compile_data", render_list(test_compile_data) if test_compile_data else None),
                    ("data", render_list(bin_data) if bin_data else None),
                    ("test_fuzz", test_fuzz),
                    ("process_per_test", process_per_test),
                    ("tags", render_list([starlark_str(t) for t in tags]) if tags else None),
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


def render_workspace_bzl(root: dict) -> str:
    """`bazel/workspace.bzl`: values every crate inherits from the root manifest."""
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
        '"""Workspace-wide values derived from the root Cargo.toml."""\n\n',
        f"WORKSPACE_VERSION = {starlark_str(pkg['version'])}\n\n",
        f"RUST_EDITION = {starlark_str(pkg['edition'])}\n\n",
        "# `[workspace.lints.*]`, each in Cargo's application order.\n",
        f"RUSTC_LINTS = {render_dict(lint_levels(rust_lints))}\n\n",
        f"RUSTC_CHECK_CFG = {render_dict_of_lists(check_cfg)}\n\n",
        f"CLIPPY_LINTS = {render_dict(lint_levels(lints.get('clippy', {})))}\n\n",
        f"RUSTDOC_LINTS = {render_dict(lint_levels(lints.get('rustdoc', {})))}\n",
    ]
    return "".join(lines)


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify generated files are up to date")
    args = parser.parse_args()

    root = load_root_manifest()
    manifests = member_manifests(root)
    shadow = shadow_workspace(root, manifests)

    # The shadow workspace must be in place before Cargo can be asked about it.
    stale_links = sync_shadow_links(shadow, args.check)
    outputs: dict[Path, str] = dict(shadow.manifests)
    outputs[SHADOW_ROOT / "BUILD.bazel"] = render_shadow_build_file(shadow)
    outputs[BAZELIGNORE] = render_bazelignore(shadow)
    stale = list(stale_links) + write_outputs(outputs, args.check)
    if args.check and stale:
        return report_stale(stale)

    meta = cargo_metadata(shadow)
    crates = build_crates(meta)

    outputs = {WORKSPACE_BZL: render_workspace_bzl(root)}
    for crate in crates.values():
        outputs[REPO_ROOT / crate.package_path / "BUILD.bazel"] = render_build_file(crate)
    stale += write_outputs(outputs, args.check)

    if args.check and stale:
        return report_stale(stale)
    if not args.check:
        print(f"{len(crates)} crates, {len(stale)} file(s) updated")
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
