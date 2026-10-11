"""Source snapshots cannot inherit trust or change the bundled identity."""

import copy

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
        "bindings": [binding],
        "active_binding_id": binding["id"],
    }


@pytest.mark.parametrize(
    "attack",
    [
        "identity",
        "validation",
        "unknown_field",
        "duplicate",
        "wrong_active",
        "credentials",
        "download_query",
        "file_url",
        "false_id",
        "unknown_method",
        "wrong_schema",
        "too_many",
        "bad_timestamp",
        "bad_kind",
        "bad_registry",
    ],
)
def test_invalid_or_trust_claiming_source_snapshots_are_rejected(snapshot, attack):
    if attack == "identity":
        snapshot["identity"]["author"] = "Someone else"
    elif attack == "validation":
        snapshot["bindings"][0]["verification"] = "identity_matched"
    elif attack == "unknown_field":
        snapshot["download_url"] = "https://example.com/arbitrary.zip"
    elif attack == "duplicate":
        snapshot["bindings"].append(copy.deepcopy(snapshot["bindings"][0]))
    elif attack == "wrong_active":
        snapshot["active_binding_id"] = "not-in-bindings"
    elif attack == "credentials":
        snapshot["bindings"][0]["repo_url"] = (
            "https://user:secret@github.com/alice/demo"
        )
    elif attack == "download_query":
        snapshot["bindings"][0]["repo_url"] += "?token=secret"
    elif attack == "file_url":
        snapshot["bindings"][0]["repo_url"] = "file:///private/repo"
    elif attack == "false_id":
        snapshot["bindings"][0]["id"] = "different"
    elif attack == "unknown_method":
        snapshot["provenance"]["method"] = {"method": "market"}
    elif attack == "wrong_schema":
        snapshot["schema_version"] = True
    elif attack == "too_many":
        snapshot["bindings"] *= 257
    elif attack == "bad_timestamp":
        snapshot["provenance"]["installed_at"] = {"time": "now"}
    elif attack == "bad_kind":
        snapshot["bindings"][0]["kind"] = []
    else:
        snapshot["bindings"][0]["registry_url"] = (
            "https://user:password@example.com/registry"
        )
    with pytest.raises(ValueError):
        validate_sources(snapshot, {"name": "demo", "author": "Alice"})


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
    snapshot["bindings"] = [binding]
    snapshot["active_binding_id"] = binding["id"]
    validate_sources(snapshot, snapshot["identity"])
