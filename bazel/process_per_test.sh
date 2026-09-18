#!/usr/bin/env bash
# Runs every test of the libtest binary {BINARY} in its own process, like
# `cargo nextest` does. See process_per_test.bzl for why.
#
# Extra arguments (`bazel test ... --test_arg=<filter>`) are forwarded to
# both the listing and every per-test invocation, so filters and flags such
# as `--nocapture` behave as they would for the plain test binary.
# `TEST_PROCESSES` bounds how many test processes run concurrently.
set -euo pipefail

binary="$TEST_SRCDIR/$TEST_WORKSPACE/{BINARY}"
processes="${TEST_PROCESSES:-4}"

if [[ "${1:-}" == "--run-one" ]]; then
    name="$2"
    shift 2
    log="$(mktemp "${TEST_TMPDIR:-/tmp}/test-output.XXXXXX")"
    if "$binary" --exact "$name" "$@" >"$log" 2>&1; then
        echo "test $name ... ok"
        status=0
    else
        echo "test $name ... FAILED"
        echo "---- $name output ----"
        cat "$log"
        echo "---- end of $name output ----"
        status=1
    fi
    rm -f "$log"
    exit "$status"
fi

# `--list --format terse` prints one `<name>: test` line per test.
mapfile -t tests < <("$binary" --list --format terse "$@" | sed -n 's/: test$//p')
echo "running ${#tests[@]} tests, up to $processes at a time"
if [[ ${#tests[@]} -eq 0 ]]; then
    exit 0
fi

# `xargs -P` fans the tests out and exits non-zero if any invocation failed.
if printf '%s\0' "${tests[@]}" |
    xargs -0 -P "$processes" -I{} "$0" --run-one {} "$@"; then
    echo "test result: ok. ${#tests[@]} passed"
else
    echo "test result: FAILED"
    exit 1
fi
