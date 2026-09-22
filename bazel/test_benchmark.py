"""Test measurement bookkeeping without running Rust builds or benchmarks."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark


class BenchmarkTest(unittest.TestCase):
    def test_inventory_checks_names_not_just_counts_and_ignored_status(self):
        output = "INFO: tests\nmodule::b: test\nmodule::a: test\nnoise: benchmark\n"
        self.assertEqual(benchmark.inventory(output), ["module::a", "module::b"])
        good = {"all": ["a", "b"], "ignored": ["b"]}
        benchmark.check_inventories({"cargo": good, "bazel": good})
        for bad in ({"all": ["a", "c"], "ignored": ["b"]},
                    {"all": ["a", "b"], "ignored": []}):
            with self.assertRaises(ValueError):
                benchmark.check_inventories({"cargo": good, "bazel": bad})
        with self.assertRaises(ValueError):
            benchmark.check_inventories({tool: {"all": [], "ignored": []} for tool in ("cargo", "bazel")})

    def test_edit_changes_bytes_and_restores_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lib.rs"
            original = b"//! Preserve source bytes.\r\n"
            path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "failed"):
                with benchmark.edit_source(path, 17):
                    self.assertIn(b"value.wrapping_add(17)", path.read_bytes())
                    self.assertTrue(path.read_bytes().startswith(original))
                    raise RuntimeError("failed")
            self.assertEqual(path.read_bytes(), original)

    def test_summary_excludes_setup_and_rejects_failures_and_unpaired_data(self):
        rows = [dict(scenario="leaf", phase="build", tool=tool, seconds=seconds,
                     exit_code=0, measured=True)
                for tool, seconds in (("cargo", 2), ("bazel", 1), ("cargo", 8), ("bazel", 3),
                                      ("cargo", 9), ("bazel", 4))]
        rows.append(dict(rows[0], seconds=1000, measured=False))
        report = benchmark.summarize(rows)
        self.assertIn("8.000 [2.000, 9.000] | 3.000 [1.000, 4.000] | 2.67×", report)
        with self.assertRaises(ValueError):
            benchmark.summarize([dict(rows[0], exit_code=1), rows[1]])
        with self.assertRaises(ValueError):
            benchmark.summarize(rows[:1])
        with self.assertRaises(ValueError):
            benchmark.summarize(rows[:3])

    def test_commands_select_same_roots_and_separate_compilation_execution_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=Path(tmp) / "results", workload="tempo", mode="ci",
                                      jobs=4, test_threads=8)
            runner = benchmark.Benchmark(args, Path(tmp))
            runner.bazel_start = ["bazel", "--output_base=/private"]
            runner.bazel_flags = ["--config=ci"]
            calls = []
            runner.run = lambda command, **kwargs: calls.append((command, kwargs))
            for tool in ("cargo", "bazel"):
                runner.cycle(tool, "leaf")
            cargo = [command for command, _ in calls[:4]]
            bazel = [command for command, _ in calls[4:]]
            for command in cargo:
                self.assertIn("tempo-payload-builder", command)
                self.assertIn("--lib", command)
                self.assertNotIn("--workspace", command)
            self.assertIn("--no-run", cargo[1])
            self.assertNotIn("--no-run", cargo[2])
            self.assertEqual(cargo[2], cargo[3])
            self.assertIn("//tempo/crates/payload/builder:tempo_payload_builder", bazel[0])
            self.assertIn("//tempo/crates/payload/builder:tempo_payload_builder_test", bazel[1])
            self.assertIn("--cache_test_results=no", bazel[2])
            self.assertIn("--cache_test_results=yes", bazel[3])
            self.assertEqual(calls[0][1]["cwd"], benchmark.ROOT / "tempo")
            self.assertEqual(runner.env["CARGO_INCREMENTAL"], "0")
            self.assertEqual(runner.env["CARGO_PROFILE_TEST_CODEGEN_UNITS"], "4")

    def test_scoped_comparison_explicitly_uses_shared_workspace_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=Path(tmp) / "results", workload="tempo", mode="ci",
                                      jobs=8, test_threads=8, scoped=True)
            runner = benchmark.Benchmark(args, Path(tmp))
            calls = []
            runner.run = lambda command, **kwargs: calls.append((command, kwargs))
            runner.phase("cargo", "cold", "test_compile")
            command, options = calls[0]
            self.assertEqual(options["cwd"], benchmark.ROOT / "bazel/cargo")
            self.assertIn("--frozen", command)
            self.assertEqual(command[command.index("--target") + 1], "x86_64-unknown-linux-gnu")
            self.assertNotIn("RUSTC_BOOTSTRAP", runner.env)

    def test_scoped_alloy_uses_default_features_not_global_build_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=Path(tmp) / "results", workload="alloy", mode="ci",
                                      jobs=8, test_threads=8, scoped=True)
            runner = benchmark.Benchmark(args, Path(tmp))
            command = runner.cargo("test")
            self.assertIn("alloy-consensus", command)
            self.assertIn("--lib", command)
            self.assertNotIn("--features", command)
            self.assertNotIn("--no-default-features", command)
            self.assertEqual(runner.enabled, ["default"])
            self.assertEqual(runner.phases, ("build", "test_compile", "test_run", "test_cached"))

    def test_node_builds_binary_only_and_checks_offline_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=Path(tmp) / "results", workload="tempo-node", mode="ci",
                                      jobs=8, test_threads=8, scoped=True)
            runner = benchmark.Benchmark(args, Path(tmp))
            runner.bazel_start, runner.bazel_flags = ["bazel"], []
            runner.env["CARGO_TARGET_DIR"] = tmp + "/target"
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                return "Usage: tempo [OPTIONS]" if command[-1] == "--help" else "tempo 1.14.0"

            runner.run = run
            for tool in ("cargo", "bazel"):
                runner.cycle(tool, "cold")
                runner.smoke_binary(tool)
            cargo, bazel = calls[0][0], calls[3][0]
            self.assertEqual(cargo[cargo.index("--bin") + 1], "tempo")
            self.assertNotIn("--lib", cargo)
            self.assertNotIn("--no-default-features", cargo)
            self.assertEqual(bazel[1:3], ["build", "//tempo/bin/tempo:tempo"])
            self.assertEqual(len(calls), 6)
            self.assertTrue(calls[1][0][0].endswith("/x86_64-unknown-linux-gnu/debug/tempo"))
            self.assertEqual(calls[4][0][-3:], ["//tempo/bin/tempo:tempo", "--", "--help"])
            self.assertTrue(all(not k.get("measured", False) for _, k in (calls[1], calls[2], calls[4], calls[5])))
            runner.run = lambda *a, **k: "wrong executable"
            with self.assertRaisesRegex(ValueError, "--help"):
                runner.smoke_binary("cargo")

    def test_node_trial_edits_main_and_never_schedules_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leaf, foundation = root / "tempo/bin/tempo/src/main.rs", root / benchmark.FOUNDATION
            for path in (leaf, foundation):
                path.parent.mkdir(parents=True)
                path.write_text("//! Original.\n")
            original = leaf.read_bytes()
            args = argparse.Namespace(output=root / "results", workload="tempo-node", mode="ci",
                                      jobs=8, test_threads=8, scoped=True)
            runner = benchmark.Benchmark(args, root)
            runner.prepare = lambda repetition: None
            smoke, phases = [], []
            runner.smoke_binary = smoke.append

            def phase(tool, scenario, phase, measured=True, extra=()):
                phases.append((tool, scenario, phase, measured))
                self.assertEqual(phase, "build")
                self.assertEqual(leaf.read_bytes() != original, scenario == "leaf")
                self.assertEqual(foundation.read_bytes() != original, scenario == "foundation")

            runner.phase = phase
            with patch("benchmark.ROOT", root):
                runner.trial(1)
            self.assertEqual(smoke, ["cargo", "bazel"])
            self.assertEqual(sum(p[3] for p in phases), 8)
            self.assertEqual(leaf.read_bytes(), original)
            self.assertEqual(foundation.read_bytes(), original)
            self.assertFalse((runner.output / "inventories-1.json").exists())

    def test_trials_alternate_tools_and_edits_start_from_restored_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leaf = root / "alloy/crates/consensus/src/lib.rs"
            foundation = root / benchmark.FOUNDATION
            for path in (leaf, foundation):
                path.parent.mkdir(parents=True)
                path.write_text("//! Original.\n")
            original = leaf.read_bytes()
            args = argparse.Namespace(output=root / "results", workload="alloy", mode="ci",
                                      jobs=4, test_threads=8)
            runner = benchmark.Benchmark(args, root)
            runner.bazel_start = ["bazel"]
            runner.bazel_flags = []
            runner.prepare = lambda repetition: None
            commands = []
            runner.run = lambda command, **kwargs: commands.append(command)
            phases = []

            def phase(tool, scenario, phase, measured=True, extra=()):
                phases.append((tool, scenario, phase, measured))
                self.assertEqual(leaf.read_bytes() != original, scenario == "leaf")
                self.assertEqual(foundation.read_bytes() != original, scenario == "foundation")
                if phase == "test_list":
                    return "" if "--ignored" in extra else "one::test: test\n"
                return ""

            runner.phase = phase
            with patch("benchmark.ROOT", root):
                for repetition, first in ((1, "cargo"), (2, "bazel")):
                    phases.clear()
                    runner.trial(repetition)
                    self.assertEqual(phases[0][:3], (first, "cold", "build"))
                    self.assertEqual(sum(p[3] for p in phases), 32)
                    self.assertTrue(all(not p[3] for p in phases if p[1] == "restore"))
                    self.assertEqual(commands[-1], ["bazel", "shutdown"])

    def test_failure_is_recorded_before_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=Path(tmp) / "results", workload="alloy", mode="dev",
                                      jobs=8, test_threads=8)
            runner = benchmark.Benchmark(args, Path(tmp))
            with patch("benchmark.subprocess.run") as run:
                run.return_value.returncode = 7
                with self.assertRaises(RuntimeError):
                    runner.run(["fake-compiler"], tool="cargo", measured=True)
            row = json.loads((runner.output / "samples.jsonl").read_text())
            self.assertEqual(row["exit_code"], 7)
            self.assertTrue(row["measured"])
            self.assertEqual(runner.env["CARGO_INCREMENTAL"], "1")


if __name__ == "__main__":
    unittest.main()
