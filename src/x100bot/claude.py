"""Subprocess wrapper for `claude -p`: common flags, auth handling, JSON result parsing, usage limit detection.
Every call goes through the limiter (bucket claude, critique or teacher) and a process wide lock, so there is never
more than one Claude call at a time."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)
_ONE_CALL = threading.Lock()
LIMIT_RE = re.compile(r"hit your (session|weekly|Opus|Sonnet) limit", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?(?:\s*\(?[A-Za-z/_ ]+\)?)?)", re.I)


class ClaudeError(Exception):
    pass


class UsageLimit(ClaudeError):
    def __init__(self, kind: str, resets_at: datetime | None, text: str):
        super().__init__(f"Claude {kind} limit reached" + (f", resets {resets_at:%H:%M}" if resets_at else ""))
        self.kind, self.resets_at, self.text = kind, resets_at, text

    @property
    def retry_with_fallback(self) -> bool:
        return self.kind.lower() in ("opus", "sonnet")


@dataclass
class Result:
    data: dict
    session_id: str | None = None
    cost_usd: float = 0.0
    model: str = ""
    raw: dict = field(default_factory=dict)


def parse_reset(text: str, now: datetime) -> datetime | None:
    """'resets 3:45pm' -> today 15:45 local (tomorrow if already past). None when absent."""
    m = RESET_RE.search(text)
    if not m:
        return None
    t = m.group(1).strip().lower()
    hm = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not hm:
        return None
    h, mi, ap = int(hm.group(1)), int(hm.group(2) or 0), hm.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    at = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def fill(template: str, **values) -> str:
    """Fill {{placeholders}} in a prompt file."""
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


class Claude:
    def __init__(self, s, limiter, tz):
        self.s, self.lim, self.tz = s, limiter, tz
        self.cfg = s.claude

    def binary(self) -> str:
        """The claude executable. On Windows the npm shim is a .cmd that wraps bin/claude.exe; run the exe directly
        so the prompt text never goes through cmd.exe quoting. In the container it is the native install."""
        import shutil
        found = shutil.which(self.cfg.binary) or self.cfg.binary
        if found.lower().endswith(".cmd"):
            exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if exe.exists():
                return str(exe)
        return found

    def env(self) -> dict:
        env = dict(os.environ)
        env["CLAUDE_CODE_MAX_RETRIES"] = str(self.cfg.max_retries)
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        if self.cfg.auth == "oauth":
            env.pop("ANTHROPIC_API_KEY", None)   # the oauth token must win
        return env

    def argv(self, prompt: str, *, schema: Path, model: str, system_file: Path | None = None,
             allowed_tools: str | None = None, disallowed_tools: str | None = None, max_turns: int = 4,
             resume: str | None = None, persist_session: bool = False, extra: list[str] | None = None) -> list[str]:
        # --json-schema takes the schema text itself, not a file path
        a = [self.binary(), "-p", prompt, "--output-format", "json", "--json-schema",
             Path(schema).read_text(encoding="utf-8"),
             "--permission-mode", "dontAsk", "--permission-prompts", "none", "--strict-mcp-config",
             "--model", model, "--fallback-model", self.cfg.fallback_model, "--max-turns", str(max_turns)]
        if not persist_session:
            a.append("--no-session-persistence")
        if self.cfg.auth == "apikey":
            a.append("--bare")   # never with oauth: bare mode does not read CLAUDE_CODE_OAUTH_TOKEN
        if system_file:
            a += ["--append-system-prompt-file", str(system_file)]
        if allowed_tools:
            a += ["--allowedTools", allowed_tools]
        if disallowed_tools:
            a += ["--disallowedTools", disallowed_tools]
        if resume:
            a += ["--resume", resume]
        return a + (extra or [])

    def run(self, bucket: str, prompt: str, stdin: dict, *, schema: Path, timeout: int, model: str | None = None,
            cwd: Path | None = None, **kw) -> Result:
        """One call. Raises UsageLimit, or ClaudeError for any other failure. The caller decides on retries."""
        model = model or self.cfg.model
        with _ONE_CALL:
            ticket = self.lim.acquire(bucket, target=f"claude:{bucket}")
            argv = self.argv(prompt, schema=schema, model=model, **kw)
            started = time.time()
            try:
                proc = subprocess.run(argv, input=json.dumps(stdin), capture_output=True, text=True, encoding="utf-8",
                                      timeout=timeout, env=self.env(), cwd=str(cwd) if cwd else None)
            except subprocess.TimeoutExpired:
                self.lim.report(ticket, "soft_fail", reason="timeout")
                raise ClaudeError(f"claude timed out after {timeout} s")
            except FileNotFoundError:
                self.lim.report(ticket, "hard_fail", reason="claude binary missing")
                raise ClaudeError(f"{self.cfg.binary} not found")
            # an older Claude Code may not know --permission-prompts: drop it and run once more
            if proc.returncode != 0 and "permission-prompts" in (proc.stderr or ""):
                argv = [x for i, x in enumerate(argv) if x != "--permission-prompts" and argv[i - 1] != "--permission-prompts"]
                proc = subprocess.run(argv, input=json.dumps(stdin), capture_output=True, text=True, encoding="utf-8",
                                      timeout=timeout, env=self.env(), cwd=str(cwd) if cwd else None)
            log.info("claude %s finished in %.0f s, exit %d", bucket, time.time() - started, proc.returncode)
            try:
                raw = json.loads(proc.stdout) if proc.stdout.strip() else {}
            except ValueError:
                raw = {}
            text = (raw.get("result") if isinstance(raw.get("result"), str) else "") or proc.stderr or proc.stdout or ""
            m = LIMIT_RE.search(text)
            if m:
                self.lim.report(ticket, "soft_fail", reason=f"{m.group(1)} limit")
                raise UsageLimit(m.group(1), parse_reset(text, datetime.now(self.tz)), text[:300])
            if proc.returncode != 0 or raw.get("is_error") or raw.get("subtype", "success") != "success":
                self.lim.report(ticket, "soft_fail", reason=f"exit {proc.returncode} {raw.get('subtype', '')}")
                raise ClaudeError(f"claude failed: exit {proc.returncode}, {raw.get('subtype')}: "
                                  f"{(proc.stderr or text)[:300]}")
            data = raw.get("structured_output")
            if data is None:
                self.lim.report(ticket, "soft_fail", reason="no structured_output")
                raise ClaudeError("claude returned no structured_output")
            self.lim.report(ticket, "ok")
            return Result(data=data, session_id=raw.get("session_id"), cost_usd=float(raw.get("total_cost_usd") or 0),
                          model=model, raw=raw)

    def run_with_retry(self, bucket: str, prompt: str, stdin: dict, *, alert=None, **kw) -> Result | None:
        """The brief's policy: Opus or Sonnet limit -> once more with the fallback model; session or weekly limit -> give
        up; any other failure -> one retry after 60 s. Returns None when the caller should use fallback templates."""
        try:
            return self.run(bucket, prompt, stdin, **kw)
        except UsageLimit as ex:
            if ex.retry_with_fallback:
                try:
                    return self.run(bucket, prompt, stdin, model=self.cfg.fallback_model, **kw)
                except ClaudeError as ex2:
                    ex = ex2 if isinstance(ex2, UsageLimit) else ex
            when = f", resets at about {ex.resets_at:%H:%M}" if getattr(ex, "resets_at", None) else ""
            if alert:
                alert(f"Claude {getattr(ex, 'kind', '')} limit reached{when}; fallback templates used")
            return None
        except ClaudeError as ex:
            log.warning("claude %s failed, retrying once in 60 s: %s", bucket, ex)
            self.lim.sleep(60)
            try:
                return self.run(bucket, prompt, stdin, **kw)
            except ClaudeError as ex2:
                if alert:
                    alert(f"Claude {bucket} failed twice ({str(ex2)[:120]}); fallback templates used")
                return None
