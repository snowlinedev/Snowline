"""SDK-own unit tests for the vendored contract constants + version check."""

from __future__ import annotations

import pytest

from snowline_plugin_sdk import contract


def test_event_type_literals():
    assert contract.EVENT_DECISION_RECORDED == "decision.recorded"
    assert contract.EVENT_DECISION_SUPERSEDED == "decision.superseded"


def test_event_types_frozenset():
    # The full write-surface vocabulary (replication-continuity §4, #79):
    # decisions + shadow graph + artifacts (the spec/plan/reference docs) +
    # the graduation provenance stamp — pinned as LITERALS so a registry edit
    # in only one package can't slip past its own suite.
    assert contract.EVENT_TYPES == frozenset(
        {
            "decision.recorded",
            "decision.superseded",
            "shadow.branch_created",
            "shadow.branch_archived",
            "shadow.notes_set",
            "shadow.node_added",
            "shadow.citation_added",
            "shadow.conversation_appended",
            "shadow.graduated",
            "artifact.registered",
            "artifact.revised",
            "artifact.resolved",
            "artifact.maturity_set",
            "artifact.governs_set",
        }
    )
    assert isinstance(contract.EVENT_TYPES, frozenset)


def test_contract_version():
    # 2 = the stream envelope (replication-continuity §3.2, #77): epoch,
    # emit-time seq, peer_seen — a breaking addition over v1.
    assert contract.CONTRACT_VERSION == 2


def test_check_contract_version_none_is_accepted():
    contract.check_contract_version(None)  # pre-versioning → defaults to 1


def test_check_contract_version_at_or_below_is_accepted():
    contract.check_contract_version(contract.CONTRACT_VERSION)
    contract.check_contract_version(contract.CONTRACT_VERSION - 1)


def test_check_contract_version_newer_is_rejected():
    with pytest.raises(contract.IncompatibleContractVersion):
        contract.check_contract_version(contract.CONTRACT_VERSION + 1)
