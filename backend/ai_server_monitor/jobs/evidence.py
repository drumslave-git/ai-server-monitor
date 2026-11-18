"""Checking a job's findings against what its commands really printed.

A finding carries `evidence`: the exact line(s) of command output it rests on (required for
warn and crit). Every command a run executes, directly or through `delegate`, goes through a
`RecordingShell`, and every evidence line must occur in that output (or in the run's input,
for the summary, whose sources are the topic results). This validates the model's claims; it
collects nothing on its own.

The agent loop gets one retry naming the mismatches (`EvidenceCheck` as a Pydantic AI output
validator); what still does not match is stored, marked `verified: false`.
"""

from __future__ import annotations

from typing import Any

from pydantic_ai import ModelRetry

QUOTES = "`'\" "


def normalize(text: str) -> str:
    """Whitespace runs as one space: models often re-space table columns."""
    return " ".join(text.split())


def evidence_lines(evidence: list[str]) -> list[str]:
    lines = []
    for item in evidence:
        for line in str(item).splitlines():
            line = normalize(line).strip(QUOTES)
            if line and line not in ("...", "…"):
                lines.append(line)
    return lines


class RecordingShell:
    """A shell that keeps the output of every command it runs (the run's evidence pool)."""

    def __init__(self, inner: Any, outputs: list[str]):
        self._inner = inner
        self.outputs = outputs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def run(self, command: str, **kwargs: Any) -> Any:
        result = await self._inner.run(command, **kwargs)
        self.outputs.append(str(getattr(result, "output", "") or ""))
        return result


class EvidenceCheck:
    """Output validator of a job run; `sources` fills while the run goes on."""

    def __init__(self, sources: list[str]):
        self.sources = sources
        self.retried = False

    def _pool(self) -> str:
        return "\n".join(normalize(line) for s in self.sources for line in s.splitlines())

    def missing(self, finding: Any, pool: str | None = None) -> list[str]:
        """Evidence lines of one finding that occur in no source."""
        pool = self._pool() if pool is None else pool
        lines = evidence_lines(_get(finding, "evidence") or [])
        return [line for line in lines if line not in pool]

    def problems(self, findings: list[Any]) -> list[str]:
        pool = self._pool()
        out = []
        for f in findings:
            title, severity = _get(f, "title"), _get(f, "severity")
            if severity in ("warn", "crit") and not evidence_lines(_get(f, "evidence") or []):
                out.append(f'"{title}" ({severity}) has no evidence')
            for line in self.missing(f, pool):
                out.append(f'"{title}": not in any command output: "{line[:150]}"')
        return out

    def __call__(self, output: Any) -> Any:
        findings = getattr(output, "findings", None)
        if findings is None or self.retried:
            return output
        problems = self.problems(findings)
        if problems:
            self.retried = True
            raise ModelRetry(
                "Evidence check failed: "
                + "; ".join(problems[:8])
                + ". Evidence must be lines copied exactly from the command output above, and "
                "the numbers in a finding must be the ones in those lines. Fix the findings "
                "(or run the command that shows it) and call final_result again."
            )
        return output

    def mark(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stored findings with `verified`: every evidence line found (and, for warn and crit,
        at least one given)."""
        pool = self._pool()
        out = []
        for f in findings:
            lines = evidence_lines(f.get("evidence") or [])
            needed = f.get("severity") in ("warn", "crit")
            ok = (bool(lines) or not needed) and not self.missing(f, pool)
            out.append({**f, "verified": ok})
        return out


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
