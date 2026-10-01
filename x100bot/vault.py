"""Best effort movement log into an Obsidian vault (VAULT_DIR), one line per event in Activity/YYYY-MM-DD.md.
Any error is logged and ignored: the vault must never break a run or lose an alert. Never writes secrets."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Singapore")


def log_event(emoji: str, what: str, detail: str = "", entity: str | None = None) -> None:
    root = os.environ.get("VAULT_DIR")
    if not root:
        return
    try:
        now = datetime.now(TZ)
        day = Path(root) / "Activity" / f"{now:%Y-%m-%d}.md"
        day.parent.mkdir(parents=True, exist_ok=True)
        if not day.exists():
            day.write_text(f"---\ntags: [active]\nupdated: {now:%Y-%m-%d}\n---\n# {now:%Y-%m-%d}\n\n", encoding="utf-8")
        line = f"- {now:%H:%M} {emoji} **{what}**" + (f" · {detail}" if detail else "") + (f" · [[{entity}]]" if entity else "")
        with day.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as ex:   # noqa: BLE001
        log.warning("vault write skipped: %s", ex)


def recent(max_chars: int = 4000) -> str:
    """Newest first excerpt of the last few Activity notes, capped, for prompts that want memory."""
    root = os.environ.get("VAULT_DIR")
    if not root:
        return ""
    try:
        files = sorted((Path(root) / "Activity").glob("*.md"), reverse=True)[:3]
        text = "\n".join(f.read_text(encoding="utf-8") for f in files)
        return text[:max_chars]
    except Exception as ex:   # noqa: BLE001
        log.warning("vault read skipped: %s", ex)
        return ""
