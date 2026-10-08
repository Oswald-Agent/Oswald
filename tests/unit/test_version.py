"""Smoke tests for package initialization and metadata."""

import oswald


def test_package_version() -> None:
    """Verify that oswald package exposes a valid version string."""
    assert hasattr(oswald, "__version__")
    assert isinstance(oswald.__version__, str)
    assert oswald.__version__ == "0.1.0"
