"""Persisting GUI-edited settings in the database, and merging edits from the API."""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

from pydantic import ValidationError

from .config import SECRET_MASK, Settings
from .db import Database

log = logging.getLogger(__name__)

KV_KEY = "settings"


def merge_secrets(incoming: Any, current: Any) -> Any:
    """Replace masked secrets in `incoming` with the stored values from `current`.

    The API never returns secrets, only SECRET_MASK; a client sending the mask back
    means "keep what is stored". List items are matched by their `name` when they have
    one (so deleting/reordering *arr instances keeps the right keys), else by index.
    """
    if incoming == SECRET_MASK:
        return current if isinstance(current, str) else ""
    if isinstance(incoming, dict):
        cur = current if isinstance(current, dict) else {}
        return {k: merge_secrets(v, cur.get(k)) for k, v in incoming.items()}
    if isinstance(incoming, list):
        cur_list = current if isinstance(current, list) else []
        by_name = {c.get("name"): c for c in cur_list if isinstance(c, dict) and c.get("name")}
        out = []
        for i, item in enumerate(incoming):
            if isinstance(item, dict) and item.get("name") in by_name:
                match = by_name[item["name"]]
            else:
                match = cur_list[i] if i < len(cur_list) else None
            out.append(merge_secrets(item, match))
        return out
    return incoming


def _delete_path(data: Any, loc: tuple[Any, ...]) -> bool:
    node = data
    for key in loc[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        del node[loc[-1]]
    except (KeyError, IndexError, TypeError):
        return False
    return True


def lenient_validate(raw: dict[str, Any]) -> tuple[Settings, list[str]]:
    """Validate stored settings, dropping (→ default) only the parts that no longer fit,
    e.g. after an upgrade renamed or removed a field. Returns (settings, problems)."""
    data = copy.deepcopy(raw)
    problems: list[str] = []
    for _ in range(200):
        try:
            return Settings.model_validate(data), problems
        except ValidationError as exc:
            # Fix one error at a time and re-validate: removing a list item shifts the
            # indices the other errors refer to.
            err = exc.errors(include_url=False, include_input=False)[0]
            loc = tuple(err["loc"])
            problems.append(f"{'.'.join(map(str, loc))}: {err['msg']}")
            # Drop the offending value; if it can't be dropped (a *missing* required
            # field), drop its parent instead, e.g. the incomplete *arr instance.
            if not any(_delete_path(data, loc[:depth]) for depth in range(len(loc), 0, -1)):
                break
    return Settings(), [*problems, "stored settings unusable; using defaults"]


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db

    async def load(self) -> Settings:
        raw = await self.db.kv_get(KV_KEY)
        if raw is None:
            return Settings()
        settings, problems = lenient_validate(raw if isinstance(raw, dict) else {})
        if problems:
            log.warning("some stored settings were reset to defaults: %s", "; ".join(problems))
            await self.db.kv_set(f"{KV_KEY}.backup.{int(time.time())}", raw)
            await self.save(settings)
        return settings

    async def save(self, settings: Settings) -> None:
        await self.db.kv_set(KV_KEY, settings.storage_dict())

    @staticmethod
    def apply_update(current: Settings, incoming: dict[str, Any]) -> Settings:
        """Validate an edit from the API (may contain masked secrets).
        Raises pydantic.ValidationError."""
        merged = merge_secrets(incoming, current.storage_dict())
        return Settings.model_validate(merged)
