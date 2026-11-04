"""Dedicated curated corporate/macro context; no live vote or UW gate changes."""
from .collector import loop
from .view import build

__all__ = ["build", "loop"]
