"""Opt-in prototype: instantiate Cargo compilation units beside existing targets.

Copied into a private rules_rust override by tempo_scope.py. Normal builds never
load this file. Sources, native inputs, tools, and annotations come from the
existing generated rules; features and Rust edges come from Cargo's unit graph.
"""

load(":tempo_scope_data.bzl", "UNITS")

def emit(rule, kind, kwargs):
    rule(**kwargs)
    label = "@" + native.repository_name() + "//" + native.package_name() + ":" + kwargs["name"]
    for unit in UNITS.get(label, []):
        if unit["kind"] != kind:
            continue
        attrs = dict(kwargs)
        if kind == "rust_test" and attrs.get("crate"):
            # A standalone Cargo --lib test compilation, not the default
            # library's (globally unified) dependency graph plus test deps.
            original = native.existing_rule(str(attrs.pop("crate")).split(":")[-1])
            for key in ["srcs", "crate_root", "edition", "compile_data"]:
                if key in original:
                    attrs[key] = original[key]
        attrs.update(unit["attrs"])
        attrs["visibility"] = ["//visibility:public"]
        attrs["tags"] = ["manual", "tempo-scope"]
        rule(**attrs)
