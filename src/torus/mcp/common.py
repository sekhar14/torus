"""Pieces shared by the MCP server's tool modules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
# Reads the web (SEC EDGAR, price data): results depend on the outside world.
FETCH = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

EntityRef = Annotated[str, Field(description="Ticker, CIK, company name, canonical key or id")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0, description="0 = guess, 1 = certain")]


def aware(value: datetime | None) -> datetime | None:
    """Treat naive datetimes from agents as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
