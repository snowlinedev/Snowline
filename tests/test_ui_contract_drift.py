"""Producer↔consumer contract guard for the UI block (ui-shell.md §3, spec §3
"The schema lives in the plugin SDK next to the manifest shape, with the same
drift-guard treatment as the contract constants").

The platform (`snowline_platform.manifest`) is the PRODUCER/source of truth for
`UI_CONTRACT_VERSION` + the kind-name vocabulary; the published
`snowline-plugin-sdk` (`snowline_plugin_sdk.ui`) is the CONSUMER's vendored copy
(mirroring `contract.py`'s event-contract vendoring pattern). This test pins the
two equal so they can never silently fork — mirrors
`governance/tests/test_contract_drift.py`.

The SDK is a dev-only dependency here (rides the platform's dev group so the
combined test run can import it); `pytest.importorskip` so this test skips
cleanly in an environment where it isn't installed rather than failing the
whole suite.
"""

from __future__ import annotations

import pytest

from snowline_platform import manifest as platform_manifest

sdk_ui = pytest.importorskip("snowline_plugin_sdk.ui")


def test_ui_contract_version_equals_sdk():
    assert platform_manifest.UI_CONTRACT_VERSION == sdk_ui.UI_CONTRACT_VERSION


def test_ui_kind_vocabulary_equals_sdk():
    assert platform_manifest.UI_WIDGET_KINDS == sdk_ui.WIDGET_KINDS
    assert platform_manifest.UI_PAGE_KINDS == sdk_ui.PAGE_KINDS
    assert platform_manifest.UI_KINDS == sdk_ui.UI_KINDS
