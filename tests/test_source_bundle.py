"""Source snapshots cannot inherit trust or change the bundled identity."""

import copy
import hashlib
import json

import pytest
from astrbot_plugin_manager.source_bundle import portable_binding, validate_sources


@pytest.fixture
def snapshot():
    binding = portable_binding(
        {
            "kind": "market",
            "market_plugin_id": "Alice/demo",
            "repo_url": "https://github.com/alice/demo",
            "registry_url": None,
        }
    )
    return {
        "schema_version": 1,
        "identity": {"name": "demo", "author": "Alice"},
        "provenance": {
            "method": "market",
            "installed_at": None,
            "observed_at": "2026-10-11",
            "reported_installed_at": None,
            "source": copy.deepcopy(binding),
        },
        "source": binding,
    }


@pytest.mark.parametrize(
    "attack",
    [
        "identity",
        "validation",
        "unknown_field",
        "source_list",
        "unknown_source_field",
        "credentials",
        "download_query",
        "file_url",
        "false_id",
        "unknown_method",
        "wrong_schema",
        "invalid_source_type",
        "bad_timestamp",
        "bad_kind",
        "bad_registry",
    ],
)
def test_invalid_or_trust_claiming_source_snapshots_are_rejected(snapshot, attack):
    if attack == "identity":
        snapshot["identity"]["author"] = "Someone else"
    elif attack == "validation":
        snapshot["source"]["verification"] = "identity_matched"
    elif attack == "unknown_field":
        snapshot["download_url"] = "https://example.com/arbitrary.zip"
    elif attack == "source_list":
        snapshot["source"] = [copy.deepcopy(snapshot["source"])]
    elif attack == "unknown_source_field":
        snapshot["unexpected"] = "invalid"
    elif attack == "credentials":
        snapshot["source"]["repo_url"] = "https://user:secret@github.com/alice/demo"
    elif attack == "download_query":
        snapshot["source"]["repo_url"] += "?token=secret"
    elif attack == "file_url":
        snapshot["source"]["repo_url"] = "file:///private/repo"
    elif attack == "false_id":
        snapshot["source"]["id"] = "different"
    elif attack == "unknown_method":
        snapshot["provenance"]["method"] = {"method": "market"}
    elif attack == "wrong_schema":
        snapshot["schema_version"] = True
    elif attack == "invalid_source_type":
        snapshot["source"] = "invalid"
    elif attack == "bad_timestamp":
        snapshot["provenance"]["installed_at"] = {"time": "now"}
    elif attack == "bad_kind":
        snapshot["source"]["kind"] = []
    else:
        snapshot["source"]["registry_url"] = (
            "https://user:password@example.com/registry"
        )
    with pytest.raises(ValueError):
        validate_sources(snapshot, {"name": "demo", "author": "Alice"})


@pytest.mark.parametrize("kind", ["market", "repository"])
@pytest.mark.parametrize("branch", ["", "/tree/feature/release.git"])
def test_portable_github_sources_have_canonical_urls_and_ids(snapshot, kind, branch):
    original = {**snapshot["source"], "kind": kind}
    if kind == "repository":
        original["market_plugin_id"] = ""
    original["repo_url"] = "https://github.com/alice/demo.git" + branch
    binding = portable_binding(original)
    expected_repo = "https://github.com/alice/demo" + branch
    assert binding["repo_url"] == expected_repo
    assert binding == portable_binding({**original, "repo_url": expected_repo})
    legacy = {field: value for field, value in original.items() if field != "id"}
    legacy["id"] = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()[:24]
    snapshot["source"] = legacy
    snapshot["provenance"]["source"] = copy.deepcopy(legacy)
    assert validate_sources(snapshot, snapshot["identity"]) == snapshot
    assert portable_binding(snapshot["source"]) == binding
    snapshot["source"]["id"] = binding["id"]
    with pytest.raises(ValueError, match="来源标识"):
        validate_sources(snapshot, snapshot["identity"])


def test_portable_non_github_sources_preserve_git_suffix(snapshot):
    binding = portable_binding({**snapshot["source"], "repo_url": "https://example.com/alice/demo.git"})
    assert binding["repo_url"] == "https://example.com/alice/demo.git"
    snapshot["source"] = binding
    validate_sources(snapshot, snapshot["identity"])


def test_custom_market_reference_is_preserved_without_private_url_fields(snapshot):
    binding = portable_binding(
        {
            "kind": "market",
            "market_plugin_id": "Alice/demo",
            "repo_url": "",
            "registry_url": "https://user:secret@example.com/registry.json?token=secret#private",
        }
    )
    assert binding["registry_url"] == "https://example.com/registry.json"
    snapshot["source"] = binding
    validate_sources(snapshot, snapshot["identity"])
