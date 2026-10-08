"""Root pytest configuration and shared fixtures."""

import pytest


@pytest.fixture
def sample_fixture() -> str:
    """Sample fixture available across test suites."""
    return "oswald"
