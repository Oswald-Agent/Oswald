"""Integration test marker verification."""

import pytest


@pytest.mark.integration
def test_integration_marker_smoke() -> None:
    """Trivial test verifying the integration marker and test harness execution."""
    assert True
