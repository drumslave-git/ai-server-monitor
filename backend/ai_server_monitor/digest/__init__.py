"""Snapshot digester: collector data -> compact analyzed text, anomaly rules, trends."""

from .digester import Digest, build_digest, fingerprint
from .models import Flag, score_from_flags, worst_severity
from .snapshot import Snapshot, collectors_snapshot
from .trends import JournalSummary, Trends, WindowObserver

__all__ = [
    "Digest",
    "Flag",
    "JournalSummary",
    "Snapshot",
    "Trends",
    "WindowObserver",
    "build_digest",
    "collectors_snapshot",
    "fingerprint",
    "score_from_flags",
    "worst_severity",
]
