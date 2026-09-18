"""Runs each test of a libtest binary in its own process.

`bazel test` runs a `rust_test` as one process for all of its `#[test]`s.
Tests that launch full nodes (in reth, everything built on `reth-e2e-test-utils`)
retain a few GiB per launched node until the process exits, so a binary
with dozens of such tests grows to tens of GiB and gets OOM-killed on
ordinary machines. Cargo CI does not see this because `cargo nextest`
runs one process per test; this rule does the same.
"""

def _process_per_test_impl(ctx):
    test = ctx.attr.test
    binary = ctx.executable.test
    runner = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.expand_template(
        template = ctx.file._runner,
        output = runner,
        substitutions = {"{BINARY}": binary.short_path},
        is_executable = True,
    )
    providers = [
        DefaultInfo(
            executable = runner,
            runfiles = ctx.runfiles(files = [binary]).merge(test[DefaultInfo].default_runfiles),
        ),
    ]
    if RunEnvironmentInfo in test:
        # The wrapped `rust_test`'s `env` (e.g. `RUST_TEST_THREADS`, insta
        # settings); already `$(rootpath)`-expanded, and the wrapper runs
        # from the same runfiles root, so it applies unchanged.
        providers.append(test[RunEnvironmentInfo])
    return providers

process_per_test = rule(
    implementation = _process_per_test_impl,
    test = True,
    doc = "Wraps a `rust_test` so that each of its tests runs in a separate process.",
    attrs = {
        "test": attr.label(
            doc = "The `rust_test` whose tests to run.",
            executable = True,
            cfg = "target",
            mandatory = True,
        ),
        "_runner": attr.label(
            default = "//bazel:process_per_test.sh",
            allow_single_file = True,
        ),
    },
)
