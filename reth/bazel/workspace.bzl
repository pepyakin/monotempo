# GENERATED FILE - DO NOT EDIT.
#
# Regenerate with `python3 reth/scripts/bazel/generate.py` after changing Cargo.toml.

"""Workspace-wide values derived from the root Cargo.toml."""

WORKSPACE_VERSION = "2.5.2"

RUST_EDITION = "2024"

# `[workspace.lints.*]`, each in Cargo's application order.
RUSTC_LINTS = {
    "rust_2018_idioms": "deny",
    "missing_debug_implementations": "warn",
    "missing_docs": "warn",
    "unreachable_pub": "warn",
    "unused_must_use": "deny",
    "rust_2024_incompatible_pat": "warn",
    "unexpected_cfgs": "warn",
}

RUSTC_CHECK_CFG = {
    "docsrs": [],
    "test": [],
    "tokio_unstable": [],
}

CLIPPY_LINTS = {
    "borrow_as_ptr": "warn",
    "branches_sharing_code": "warn",
    "clear_with_drain": "warn",
    "cloned_instead_of_copied": "warn",
    "collection_is_never_read": "warn",
    "dbg_macro": "warn",
    "derive_partial_eq_without_eq": "warn",
    "doc_markdown": "warn",
    "empty_line_after_doc_comments": "warn",
    "empty_line_after_outer_attr": "warn",
    "enum_glob_use": "warn",
    "equatable_if_let": "warn",
    "explicit_into_iter_loop": "warn",
    "explicit_iter_loop": "warn",
    "flat_map_option": "warn",
    "if_not_else": "warn",
    "if_then_some_else_none": "warn",
    "implicit_clone": "warn",
    "imprecise_flops": "warn",
    "iter_on_empty_collections": "warn",
    "iter_on_single_items": "warn",
    "iter_with_drain": "warn",
    "iter_without_into_iter": "warn",
    "large_stack_frames": "warn",
    "manual_assert": "warn",
    "manual_clamp": "warn",
    "manual_is_variant_and": "warn",
    "manual_string_new": "warn",
    "match_same_arms": "warn",
    "missing_const_for_fn": "warn",
    "mutex_integer": "warn",
    "naive_bytecount": "warn",
    "needless_bitwise_bool": "warn",
    "needless_continue": "warn",
    "needless_for_each": "warn",
    "needless_pass_by_ref_mut": "warn",
    "nonstandard_macro_braces": "warn",
    "option_as_ref_cloned": "warn",
    "or_fun_call": "warn",
    "path_buf_push_overwrite": "warn",
    "read_zero_byte_vec": "warn",
    "result_large_err": "allow",
    "redundant_clone": "warn",
    "redundant_else": "warn",
    "redundant_field_names": "allow",
    "single_char_pattern": "warn",
    "string_lit_as_bytes": "warn",
    "string_lit_chars_any": "warn",
    "suboptimal_flops": "warn",
    "suspicious_operation_groupings": "warn",
    "trailing_empty_array": "warn",
    "trait_duplication_in_bounds": "warn",
    "transmute_undefined_repr": "warn",
    "trivial_regex": "warn",
    "tuple_array_conversions": "warn",
    "type_repetition_in_bounds": "warn",
    "uninhabited_references": "warn",
    "unnecessary_self_imports": "warn",
    "unnecessary_struct_initialization": "warn",
    "unnested_or_patterns": "warn",
    "unused_peekable": "warn",
    "unused_rounding": "warn",
    "use_self": "warn",
    "useless_let_if_seq": "warn",
    "while_float": "warn",
    "zero_sized_map_values": "warn",
    "as_ptr_cast_mut": "allow",
    "cognitive_complexity": "allow",
    "debug_assert_with_mut_call": "allow",
    "fallible_impl_from": "allow",
    "future_not_send": "allow",
    "needless_collect": "allow",
    "non_send_fields_in_send_ty": "allow",
    "redundant_pub_crate": "allow",
    "significant_drop_in_scrutinee": "allow",
    "significant_drop_tightening": "allow",
    "too_long_first_doc_paragraph": "allow",
}

RUSTDOC_LINTS = {
    "all": "warn",
}
