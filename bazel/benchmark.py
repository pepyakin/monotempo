"""CI-only, paired Cargo/Bazel measurements. See benchmark.md for interpretation."""

import argparse
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import statistics
import subprocess
import tempfile
import time

import tempo_scope


ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = {
    "alloy": ("alloy/crates/consensus", "alloy-consensus"),
    "tempo": ("tempo/crates/payload/builder", "tempo-payload-builder"),
}
FOUNDATION = "alloy-core/crates/primitives/src/lib.rs"


def features(build_file):
    match = re.search(r"^FEATURES = (\[.*?\])", build_file.read_text(), re.M | re.S)
    if not match:
        raise ValueError(f"No generated FEATURES in {build_file}")
    return ast.literal_eval(match[1])


def inventory(output):
    return sorted(re.findall(r"^([^\r\n]+): test\r?$", output, re.M))


def check_inventories(inventories):
    for kind in ("all", "ignored"):
        cargo, bazel = (inventories[tool][kind] for tool in ("cargo", "bazel"))
        if cargo != bazel or (kind == "all" and not cargo):
            raise ValueError(
                f"Test inventory mismatch ({kind}): "
                f"Cargo-only={sorted(set(cargo) - set(bazel))}, "
                f"Bazel-only={sorted(set(bazel) - set(cargo))}"
            )


@contextmanager
def edit_source(path, nonce):
    """An additive public API edit, not an mtime-only touch or a comment."""
    original = path.read_bytes()
    probe = (
        '\n/// Synthetic CI build-timing probe; never committed.\n'
        '#[doc(hidden)]\n#[inline(never)]\n'
        f'pub fn monotempo_build_timing_probe(value: u64) -> u64 {{ value.wrapping_add({nonce}) }}\n'
    ).encode()
    try:
        path.write_bytes(original + probe)
        yield
    finally:
        path.write_bytes(original)


def summarize(records):
    groups = {}
    for row in records:
        if row["measured"]:
            if row["exit_code"]:
                raise ValueError("Cannot summarize failed measurements")
            key = (row["scenario"], row["phase"], row["tool"])
            groups.setdefault(key, []).append(row["seconds"])
    lines = [
        "| Scenario | Phase | Cargo median [min, max] s | Bazel median [min, max] s | Cargo / Bazel |",
        "|---|---|---:|---:|---:|",
    ]
    for scenario, phase, _ in groups:
        if (scenario, phase, "cargo") not in groups or (scenario, phase, "bazel") not in groups:
            raise ValueError("Unpaired measurements")
    for scenario, phase in dict.fromkeys((s, p) for s, p, _ in groups):
        values = [groups[scenario, phase, tool] for tool in ("cargo", "bazel")]
        if len(values[0]) != len(values[1]):
            raise ValueError("Unbalanced measurements")
        cells = [f"{statistics.median(v):.3f} [{min(v):.3f}, {max(v):.3f}]" for v in values]
        ratio = statistics.median(values[0]) / statistics.median(values[1])
        lines.append(f"| {scenario} | {phase} | {cells[0]} | {cells[1]} | {ratio:.2f}× |")
    return "\n".join(lines) + "\n"


class Benchmark:
    def __init__(self, args, scratch):
        self.args = args
        self.scratch = scratch
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.crate_path, self.package = WORKLOADS[args.workload]
        self.label = f"//{self.crate_path}:{self.package.replace('-', '_')}"
        self.test_label = self.label + "_test"
        self.scoped = getattr(args, "scoped", False)
        self.cargo_cwd = ROOT / ("bazel/cargo" if self.scoped else args.workload)
        self.enabled = features(ROOT / self.crate_path / "BUILD.bazel")
        if self.scoped and (args.workload != "tempo" or self.enabled):
            raise ValueError("The prototype requires the featureless Tempo benchmark root")
        self.rows = []
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(("CARGO_", "RUSTFLAGS", "RUSTDOCFLAGS")) or key in (
                "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTUP_TOOLCHAIN", "RUSTC_BOOTSTRAP",
                "RUST_TEST_THREADS", "CFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "AR",
                "CARGO_ENCODED_RUSTFLAGS", "LIBCLANG_PATH", "BINDGEN_EXTRA_CLANG_ARGS",
            ):
                self.env.pop(key)
        self.env.update({
            "CARGO_HOME": str(scratch / "cargo-home"),
            "CARGO_INCREMENTAL": "1" if args.mode == "dev" else "0",
            "CARGO_PROFILE_DEV_DEBUG": "0",
            "CARGO_PROFILE_DEV_OPT_LEVEL": "0",
            "CARGO_PROFILE_DEV_CODEGEN_UNITS": "4",
            "CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "off",
            "CARGO_PROFILE_TEST_DEBUG": "0",
            "CARGO_PROFILE_TEST_OPT_LEVEL": "0",
            "CARGO_PROFILE_TEST_CODEGEN_UNITS": "4",
            "CARGO_PROFILE_TEST_SPLIT_DEBUGINFO": "off",
            "RUST_TEST_THREADS": str(args.test_threads),
            "INSTA_UPDATE": "no",
            "VERGEN_IDEMPOTENT": "1",
            "PROPTEST_RNG_SEED": "123456789",
            "CARGO_TERM_COLOR": "never",
        })

    def run(self, command, *, tool="setup", scenario="setup", phase="setup", measured=False, cwd=ROOT):
        index = len(self.rows)
        stem = f"{index:03}-{tool}-{scenario}-{phase}"
        log = self.output / f"{stem}.log"
        print(f"[{stem}] {shlex.join(command)}", flush=True)
        start = time.perf_counter()
        with log.open("w") as stream:
            result = subprocess.run(command, cwd=cwd, env=self.env, stdout=stream, stderr=subprocess.STDOUT)
        row = dict(
            tool=tool, scenario=scenario, phase=phase, measured=measured,
            repetition=getattr(self, "repetition", 0), seconds=time.perf_counter() - start,
            exit_code=result.returncode, command=command, cwd=str(cwd), log=log.name,
        )
        self.rows.append(row)
        with (self.output / "samples.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        print(f"[{stem}] exit={result.returncode}, wall={row['seconds']:.3f}s", flush=True)
        if result.returncode:
            print(log.read_text()[-12000:], flush=True)
            raise RuntimeError(f"Command failed; see {log.name}")
        return log.read_text()

    def cargo(self, verb):
        command = [
            "cargo", verb, "--verbose", "--verbose", "--frozen", "--package", self.package, "--lib",
            "--no-default-features", "--features", ",".join(self.enabled),
            "--jobs", str(self.args.jobs),
        ]
        if self.scoped:
            command += ["--target", tempo_scope.TRIPLE]
        return command

    def bazel(self, verb):
        return self.bazel_start + [verb] + self.bazel_flags

    def phase(self, tool, scenario, phase, measured=True, extra=()):
        if tool == "cargo":
            command = self.cargo("build" if phase == "build" else "test")
            if phase == "test_compile":
                command += ["--no-run", "--message-format=json-render-diagnostics"]
            elif phase == "build":
                command += ["--message-format=json-render-diagnostics"]
            else:
                command += ["--", *extra]
            cwd = self.cargo_cwd
        else:
            command = self.bazel("build" if phase in ("build", "test_compile") else "test")
            command += [self.label if phase == "build" else self.test_label]
            if phase not in ("build", "test_compile"):
                command += ["--cache_test_results=yes" if phase == "test_cached" else "--cache_test_results=no"]
                command += [f"--test_arg={arg}" for arg in extra]
            # Preserve action/cache information without parsing console timing summaries.
            event_file = self.output / f"{len(self.rows):03}-bep.json"
            command += [f"--build_event_json_file={event_file}"]
            cwd = ROOT
        return self.run(command, tool=tool, scenario=scenario, phase=phase, measured=measured, cwd=cwd)

    def cycle(self, tool, scenario, measured=True):
        for phase in ("build", "test_compile", "test_run", "test_cached"):
            self.phase(tool, scenario, phase, measured)

    def prepare(self, repetition):
        self.repetition = repetition
        # The previous repetition's scoped labels belong to its private override.
        self.label = f"//{self.crate_path}:{self.package.replace('-', '_')}"
        self.test_label = self.label + "_test"
        self.cargo_cwd = ROOT / ("bazel/cargo" if self.scoped else self.args.workload)
        trial = self.scratch / f"trial-{repetition}"
        trial.mkdir()
        self.env["CARGO_TARGET_DIR"] = str(trial / "cargo-target")
        output_base = trial / "bazel"
        self.bazel_start = [
            "bazel", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
            f"--bazelrc={ROOT / '.bazelrc'}", f"--output_base={output_base}",
        ]
        self.bazel_flags = [
            "--config=ci", f"--jobs={self.args.jobs}", f"--local_resources=cpu={self.args.jobs}",
            "--disk_cache=", "--remote_cache=", "--noremote_accept_cached",
            f"--repository_cache={self.scratch / 'bazel-downloads'}",
            "--color=no", "--curses=no", "--symlink_prefix=/",
            "--@rules_rust//rust/settings:codegen_units=4",
            "--@rules_rust//rust/settings:extra_rustc_flags=-Copt-level=0,-Cdebuginfo=0",
            f"--test_env=RUST_TEST_THREADS={self.args.test_threads}",
            "--test_env=PROPTEST_RNG_SEED=123456789", "--local_test_jobs=1", "--test_output=all",
        ]
        if self.args.mode == "dev":
            incremental = trial / "incremental"
            incremental.mkdir()
            self.bazel_flags += [
                "--config=dev", f"--sandbox_add_mount_pair={incremental}",
                f"--@rules_rust//rust/settings:per_crate_rustc_flag=//@-Cincremental={incremental}",
                # Keep codegen units comparable, rather than dev's usual 256.
                "--@rules_rust//rust/settings:per_crate_rustc_flag=//@-Ccodegen-units=4",
            ]
        # Downloads, toolchain extraction, and server startup are outside compile timings.
        self.run(self.bazel_start + ["fetch", "--lockfile_mode=error",
                 f"--repository_cache={self.scratch / 'bazel-downloads'}", self.test_label])
        clangs = list(output_base.glob("external/*llvm*/bin/clang"))
        if len(clangs) != 1:
            raise RuntimeError(f"Expected one fetched LLVM toolchain, found {clangs}")
        llvm = clangs[0].parent.parent
        self.env.update({
            "CC": str(llvm / "bin/clang"), "CXX": str(llvm / "bin/clang++"),
            "AR": str(llvm / "bin/llvm-ar"), "LIBCLANG_PATH": str(llvm / "lib"),
            "BINDGEN_EXTRA_CLANG_ARGS": f"-isystem {llvm / 'lib/clang/20/include'}",
            "RUSTFLAGS": f"-C linker={llvm / 'bin/clang'} -C link-arg=-fuse-ld=lld",
        })
        self.run([str(llvm / "bin/clang"), "--version"])
        self.run(["cargo", "fetch", "--locked"], cwd=self.cargo_cwd)
        query_flags = ["--lockfile_mode=error"]
        if self.scoped:
            scope_dir = trial / "scope"
            self.run(["python3", str(ROOT / "bazel/tempo_scope.py"),
                      "--rules-root", str(output_base / "external/rules_rust+"),
                      "--output", str(scope_dir)])
            scope = json.loads((scope_dir / "scope.json").read_text())
            self.cargo_cwd = Path(scope["cargo_workspace"])
            for name in ("scope.json", "cargo-build-units.json", "cargo-test-units.json"):
                (self.output / f"scope-{repetition}-{name}").write_bytes((scope_dir / name).read_bytes())
            self.bazel_flags += ["--override_repository=rules_rust+=" + scope["override"]]
            query_flags += ["--override_repository=rules_rust+=" + scope["override"]]
            self.label, self.test_label = (scope["roots"][verb] for verb in ("build", "test"))
            query = 'mnemonic("Rustc.*", deps(set(' + self.label + " " + self.test_label + ")))"
            # Keep stderr separate: merged progress messages are not JSON.
            action_file = self.output / f"scope-{repetition}-actions.json"
            self.run(self.bazel("aquery") + ["--output=jsonproto", "--output_file=" + str(action_file), query])
            verdict = tempo_scope.audit(json.loads(action_file.read_text()), scope)
            (self.output / f"scope-{repetition}-audit.txt").write_text(verdict + "\n")
            print(verdict, flush=True)
        self.run(["cargo", "tree", "--frozen", "-p", self.package, "--no-default-features",
                  "--features", ",".join(self.enabled), "-e", "features"], cwd=self.cargo_cwd)
        self.run(self.bazel_start + ["query", *query_flags, "--output=build", f"deps({self.test_label})"])

    def trial(self, repetition):
        inventories = {}
        order = ("cargo", "bazel") if repetition % 2 else ("bazel", "cargo")
        try:
            self.prepare(repetition)
            for tool in order:
                self.phase(tool, "cold", "build")
                self.phase(tool, "cold", "test_compile")
                if tool == "bazel":
                    self.run(self.bazel("aquery") + ["--output=textproto",
                             f'mnemonic("Rustc.*", deps({self.test_label}))'])
                inventories[tool] = {}
                for kind, extra in (("all", []), ("ignored", ["--ignored"])):
                    listing = self.phase(tool, "inventory", "test_list", False,
                                         ["--list", "--format=terse", *extra])
                    inventories[tool][kind] = inventory(listing)
                self.phase(tool, "cold", "test_run")
                self.phase(tool, "cold", "test_cached")
                self.cycle(tool, "noop")
                for scenario, source in (("leaf", self.crate_path + "/src/lib.rs"), ("foundation", FOUNDATION)):
                    with edit_source(ROOT / source, repetition):
                        self.cycle(tool, scenario)
                    # Restore and re-prime the original baseline before the next independent edit.
                    self.cycle(tool, "restore", measured=False)
            (self.output / f"inventories-{repetition}.json").write_text(json.dumps(inventories, indent=2) + "\n")
            check_inventories(inventories)
        finally:
            if hasattr(self, "bazel_start"):
                self.run(self.bazel_start + ["shutdown"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument("--mode", choices=("ci", "dev"), default="ci")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--test-threads", type=int, default=8)
    parser.add_argument("--scoped", action="store_true", help="Experimental Tempo graph; Cargo uses the shared lockfile")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Measurements must run in GitHub Actions, not on a local machine or orb")
    if args.jobs < 1 or args.test_threads < 1:
        parser.error("Job and test-thread budgets must be positive")
    if args.scoped and args.workload != "tempo":
        parser.error("The scoped prototype only supports the Tempo workload")
    if (ROOT / "user.bazelrc").exists():
        parser.error("Remove user.bazelrc from this disposable CI checkout first")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT):
        parser.error("Measurements require a clean tracked checkout")
    cpus = sorted(os.sched_getaffinity(0))
    if args.jobs > len(cpus):
        parser.error(f"Requested {args.jobs} CPUs but runner affinity allows only {len(cpus)}")
    # Both compilers and the new private Bazel server inherit the same hard CPU limit.
    os.sched_setaffinity(0, cpus[:args.jobs])
    with tempfile.TemporaryDirectory(prefix="build-stopwatch-", dir=os.environ["RUNNER_TEMP"]) as scratch:
        benchmark = Benchmark(args, Path(scratch))
        metadata = {
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "workload": args.workload, "mode": args.mode, "jobs": args.jobs,
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "test_threads": args.test_threads, "repetitions": args.repetitions,
            "root_features": benchmark.enabled,
            "run_url": f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
            "comparison": ("experimental scoped graph, shared Cargo workspace/lockfile; NOT project-local Cargo"
                           if args.scoped else "matched root targets/features; project-local Cargo vs globally unified Bazel dependencies"),
        }
        (benchmark.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        for command in (["rustc", "-vV"], ["cargo", "--version"], ["bazel", "--version"],
                        ["lscpu"], ["free", "-b"], ["df", "-B1", str(scratch)], ["uname", "-a"]):
            benchmark.run(command)
        for repetition in range(1, args.repetitions + 1):
            # Each pair owns fresh build outputs; remove them before allocating the next pair.
            with tempfile.TemporaryDirectory(prefix="pair-", dir=scratch) as pair:
                benchmark.scratch = Path(pair)
                benchmark.trial(repetition)
        summary = (
            f"## {args.workload}: {args.mode}, {args.repetitions} paired repetitions\n\n"
            + ("Experimental scoped graph against Cargo using the shared lockfile and a temporary manifest view, NOT Tempo's project-local Cargo baseline. "
               "Compiler features, extern edges and host/target contexts were audited. Build-script settings, patches and compiler flags can still differ.\n\n"
               if args.scoped else "Test names and ignored-test sets match. Dependency features are not identical; "
               "these are configured-workflow timings, not isolated build-tool speedups.\n\n")
            + summarize(benchmark.rows)
            + "\nCold = empty compiled outputs, warm downloads/OS cache. test_compile follows build; "
            "test_run forces execution; test_cached allows Bazel result reuse but Cargo reruns. "
            "See benchmark.md and artifacts for scope, commands, flags and feature inventories.\n"
        )
        (benchmark.output / "summary.md").write_text(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
                stream.write(summary)
        print(summary)


if __name__ == "__main__":
    main()
