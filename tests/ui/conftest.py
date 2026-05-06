"""Shared fixtures for UI tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_refresh_panel_embed() -> None:  # type: ignore[misc]
    """refresh_panel_embed を全テストで自動モックする。"""
    with patch(
        "src.ui.control_panel.refresh_panel_embed",
        new_callable=AsyncMock,
    ):
        yield
