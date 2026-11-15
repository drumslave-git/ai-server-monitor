"""Errors of the API access layer. The message is meant for the model: short, and saying
what to change."""

from __future__ import annotations


class ApiAccessError(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status
        """HTTP status when the service answered."""
