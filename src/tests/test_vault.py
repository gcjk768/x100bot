import json
import re
from datetime import date, datetime
from types import SimpleNamespace

from x100bot import compose, planner, vault

LINE = re.compile(r"^- \d\d:\d\d \S+ \*\*[^*]+\*\*( · [^\n]+)*$")


def row(**kw):
    base = {"type": "recipe", "slot_at": "2026-10-02 08:03", "facts_json": json.dumps(
        {"recipe_name": "Kodachrome 64", "author": "Ritchie Roesch", "film_simulation": "Classic Chrome",
         "source_url": "https://fujixweekly.com/k64"}), "fields_json": json.dumps({"title": "Kodachrome 64"}),
        "text_html": "🎞 <b>RECIPE</b> · Kodachrome 64\n<code>Classic Chrome</code> &amp; grain weak"}
    base.update(kw)
    return base


def today():
    return datetime.now(vault.TZ).date()


def test_activity_line_in_year_month_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    vault.log_event("🗓", "day planned", "16 slots · 2 fallback", "Kodachrome: 64?")
    d = today()
    text = (tmp_path / "Activity" / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.md").read_text(encoding="utf-8")
    assert text.startswith("---\ntags: [log]\n")
    last = text.splitlines()[-1]
    assert LINE.match(last) and last.endswith("**day planned** · 16 slots - 2 fallback · [[Kodachrome 64]]")


def test_posted_writes_entity_note_with_append_only_history_and_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    vault.posted(row(), 101)
    note = tmp_path / "Recipes" / "Kodachrome 64.md"
    text = note.read_text(encoding="utf-8")
    for want in ("type: recipe", 'author: "Ritchie Roesch"', 'film_simulation: "Classic Chrome"', "## Post",
                 "Classic Chrome & grain weak", "## History", "posted as recipe at 2026-10-02 08:03 (message 101)"):
        assert want in text, want
    assert "<b>" not in text
    vault.posted(row(slot_at="2026-10-09 08:03"), 202)
    history = note.read_text(encoding="utf-8").split("## History\n")[1].splitlines()
    assert len(history) == 2 and "(message 101)" in history[0] and "(message 202)" in history[1]
    vault.posted(row(type="composition", facts_json="{}", fields_json=json.dumps({"title": "Leading lines"})), 303)
    assert (tmp_path / "Lessons" / "Leading lines.md").is_file()
    home = (tmp_path / "Home.md").read_text(encoding="utf-8")
    d = today()
    assert f"`Activity/{d:%Y/%m}/`" in home and f"[[{d.isoformat()}]]" in home
    assert "- [[Kodachrome 64]]" in home and "- [[Leading lines]]" in home


def test_recent_is_newest_first_and_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    for i in range(300):
        vault.log_event("📨", "posted recipe", f"slot {i:03d} · Recipe number {i:03d} with a long name", f"Recipe {i}")
    text = vault.recent()
    lines = text.splitlines()
    assert len(text) <= vault.MEMORY_CHARS and 0 < len(lines) < 300
    assert lines[0].endswith("[[Recipe 299]]") and lines[0].startswith(today().isoformat())
    assert vault.memory_block().startswith(vault.MEMORY_HEADER)


def test_migrate_moves_flat_notes_and_recent_reads_them(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    flat = tmp_path / "Activity"
    flat.mkdir()
    d = today()
    (flat / f"{d.isoformat()}.md").write_text("- 08:00 📨 **posted recipe** · Velvia · [[Velvia]]\n", encoding="utf-8")
    (flat / "notes.md").write_text("keep me\n", encoding="utf-8")
    vault.migrate()
    assert not (flat / f"{d.isoformat()}.md").exists()
    assert (flat / f"{d:%Y}" / f"{d:%m}" / f"{d.isoformat()}.md").is_file() and (flat / "notes.md").is_file()
    assert vault.recent() == f"{d.isoformat()} 08:00 posted recipe · Velvia · [[Velvia]]"


def test_off_and_broken_vault_never_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("VAULT_DIR", raising=False)
    vault.log_event("x", "y")
    vault.posted(row(), 1)
    assert vault.recent() == "" and vault.memory_block() == ""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("VAULT_DIR", str(blocker))
    vault.log_event("x", "y")
    vault.posted(row(), 1)
    vault.migrate()
    assert vault.recent() == ""


def test_memory_reaches_the_compose_brief(tmp_path, monkeypatch, settings, conn, make_limiter):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    vault.posted(row(), 1)
    prompts = []

    def fake_run(self, bucket, prompt, stdin, **kw):
        prompts.append(prompt)
        return None

    monkeypatch.setattr(compose.Claude, "run_with_retry", fake_run)
    x = SimpleNamespace(s=settings, conn=conn, lim=make_limiter(), alert=lambda k, t: None)
    d = planner.plan_day(x, date(2026, 10, 6), offline=True)
    assert compose.compose_day(x, d) == {}
    assert vault.MEMORY_HEADER in prompts[0] and "posted recipe · 2026-10-02 08:03 - Kodachrome 64 - message 1 · [[Kodachrome 64]]" in prompts[0]
