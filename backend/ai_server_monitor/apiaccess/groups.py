"""Service groups: one name for every configured service that holds the same kind of data.

A model asked "what movies are we missing?" tends to query the first service it sees
(Radarr) and stop, although Whisparr holds movies too. `service: "movies"` removes the
choice: the same GET goes to every member and the answer comes back grouped per instance.
Groups only fan out reads; a write always names one service.
"""

from __future__ import annotations

from .targets import ARR_KINDS, Target

GROUP_KINDS: dict[str, tuple[str, ...]] = {
    "movies": ("radarr", "whisparr"),
    "series": ("sonarr",),
    "all_arr": ARR_KINDS,
}
GROUP_LABEL = {
    "movies": "all Radarr and Whisparr instances",
    "series": "all Sonarr instances",
    "all_arr": "every Radarr, Sonarr and Whisparr instance",
}


def build_groups(targets: dict[str, Target]) -> dict[str, list[str]]:
    """group -> member service names (in configuration order). A group exists when it has
    a member; `all_arr` only when it would add something (two or more members)."""
    groups: dict[str, list[str]] = {}
    for group, kinds in GROUP_KINDS.items():
        members = [name for name, target in targets.items() if target.kind in kinds]
        if members and (group != "all_arr" or len(members) > 1):
            groups[group] = members
    return groups
