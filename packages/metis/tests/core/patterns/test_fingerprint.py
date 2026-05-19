"""Fingerprint construction tests.

Cover: deterministic structure features, stable signatures across re-runs,
intent regex hits, log-bucket boundaries, and the helper that derives
extensions/buckets from a flat path list.
"""

from __future__ import annotations

from metis.core.patterns.fingerprint import (
    FingerprintInputs,
    FingerprintKind,
    build_structural_features,
    compute_fingerprint,
    derive_fingerprint_inputs,
    structural_signature,
)


def _inputs(**overrides) -> FingerprintInputs:
    base = dict(
        user_message_text="hello world",
        workspace_path="/tmp/ws",
        estimated_input_tokens=500,
        has_images=False,
        has_tool_calls_in_history=False,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )
    base.update(overrides)
    return FingerprintInputs(**base)


def test_structural_features_are_sorted_and_deduped() -> None:
    inputs = _inputs(
        file_extensions=(".PY", ".py", ".ts"),
        tool_names=("shell", "shell", "read_file"),
    )
    features = build_structural_features(inputs)
    # Note: lowercase normalization happens at the derive-helper layer; the
    # struct-level dedup just enforces order + uniqueness.
    assert features.tool_names == ("read_file", "shell")
    assert tuple(sorted(set(features.file_extensions))) == features.file_extensions


def test_structural_signature_is_stable_across_calls() -> None:
    inputs = _inputs()
    sig_a = structural_signature(build_structural_features(inputs))
    sig_b = structural_signature(build_structural_features(inputs))
    assert sig_a == sig_b


def test_structural_signature_differs_on_workspace_change() -> None:
    a = build_structural_features(_inputs(workspace_path="/tmp/a"))
    b = build_structural_features(_inputs(workspace_path="/tmp/b"))
    assert structural_signature(a) != structural_signature(b)


def test_token_bucket_boundaries() -> None:
    assert (
        build_structural_features(_inputs(estimated_input_tokens=0)).estimated_input_tokens_bucket
        == 0
    )
    assert (
        build_structural_features(_inputs(estimated_input_tokens=999)).estimated_input_tokens_bucket
        == 0
    )
    assert (
        build_structural_features(
            _inputs(estimated_input_tokens=1_000)
        ).estimated_input_tokens_bucket
        == 1
    )
    assert (
        build_structural_features(
            _inputs(estimated_input_tokens=10_000)
        ).estimated_input_tokens_bucket
        == 2
    )
    assert (
        build_structural_features(
            _inputs(estimated_input_tokens=100_000)
        ).estimated_input_tokens_bucket
        == 3
    )


def test_intent_tags_pick_up_keywords() -> None:
    features = build_structural_features(
        _inputs(user_message_text="please refactor and add tests for this function")
    )
    assert "refactor" in features.intent_tags
    assert "test" in features.intent_tags


def test_intent_tags_empty_when_no_match() -> None:
    features = build_structural_features(_inputs(user_message_text="just a question about the api"))
    assert features.intent_tags == ()


def test_compute_fingerprint_returns_fresh_ids_with_stable_signature() -> None:
    inputs = _inputs()
    fp_a = compute_fingerprint(inputs)
    fp_b = compute_fingerprint(inputs)
    assert fp_a.id != fp_b.id  # ULIDs are fresh
    assert fp_a.kind == FingerprintKind.STRUCTURAL
    assert structural_signature(fp_a.structural) == structural_signature(fp_b.structural)


def test_derive_fingerprint_inputs_splits_paths_and_extensions() -> None:
    inputs = derive_fingerprint_inputs(
        user_message_text="hi",
        workspace_path="/tmp/ws",
        estimated_input_tokens=100,
        has_images=False,
        has_tool_calls_in_history=False,
        files_touched=("src/auth/login.py", "tests/test_login.py", "docs/README.md"),
        tool_names=("read_file", "edit_file"),
        side_effect_classes=("read", "write"),
    )
    assert inputs.file_extensions == (".md", ".py")
    assert inputs.file_path_buckets == ("docs", "src", "tests")
    assert inputs.tool_names == ("edit_file", "read_file")
    assert inputs.side_effect_classes == ("read", "write")


def test_workload_id_defaults_to_none() -> None:
    """Back-compat: callers that don't set workload_id get None throughout."""
    inputs = _inputs()
    assert inputs.workload_id is None
    features = build_structural_features(inputs)
    assert features.workload_id is None


def test_workload_id_flows_through_inputs_to_features() -> None:
    inputs = _inputs(workload_id="fix-a-bug-small")
    features = build_structural_features(inputs)
    assert features.workload_id == "fix-a-bug-small"


def test_structural_signature_differs_on_workload_id_change() -> None:
    """Setting workload_id changes the dedup key — same-shape turns from
    different workloads do not collapse into one row."""
    a = build_structural_features(_inputs(workload_id="A"))
    b = build_structural_features(_inputs(workload_id="B"))
    assert structural_signature(a) != structural_signature(b)


def test_derive_fingerprint_inputs_forwards_workload_id() -> None:
    inputs = derive_fingerprint_inputs(
        user_message_text="hi",
        workspace_path="/tmp/ws",
        estimated_input_tokens=100,
        has_images=False,
        has_tool_calls_in_history=False,
        workload_id="regex-with-edge-cases",
    )
    assert inputs.workload_id == "regex-with-edge-cases"
