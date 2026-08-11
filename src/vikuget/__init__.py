"""Narrow HTTPS GET gateway for a single Vikunja project."""

from .app import create_app
from .config import Settings

__all__ = ["Settings", "create_app"]
