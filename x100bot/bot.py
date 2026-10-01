"""Private chat commands, owner only. Every message that is not from owner_user_id in a private chat is ignored.
Replies go to that chat through the telegram bucket, never to the channel."""
from __future__ import annotations

import json
import logging
import random
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import exif, planner, render, vault
from .db import kv_get, kv_set
from .library.recipes import LIGHT_TAGS
from .telegram import esc

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 20 * 1024 * 1024
RECIPE_LIGHTS = {"sunny": "sunny", "overcast": "overcast", "rain": "rain", "golden": "golden_hour", "night": "night",
                 "indoor": "indoor", "bw": "bw"}
HELP = """📷 <b>X100VI COACH</b> · private commands

/today · today's brief again
/recipe [sunny|overcast|rain|golden|night|indoor|bw] · a library recipe for that light
/tip · a random camera tip
/slots · your CUSTOM 1 to 7 plan · /setc 3 Recipe Name records a bank
/favorites · your last 20 saved posts (forward a channel post to save it)
/done · mark today's assignment done
/status · budgets, stock and today's queue
<i>Send an original JPEG or HEIF as a file for a settings readout. Photos go to the teacher.</i>"""


class Bot:
    def __init__(self, x):
        self.x, self.s = x, x.s
        self.owner = x.s.telegram.owner_user_id
        self.teacher = None
        try:
            from .teacher_intake import TeacherIntake
            self.teacher = TeacherIntake(x)
        except ImportError:
            pass

    # routing ------------------------------------------------------------------------------------------------
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            if not self.allowed({**cq.get("message", {}), "from": cq.get("from", {})}):
                return
            self.x.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            if self.teacher:
                self.teacher.callback(cq)
            return
        m = update.get("message")
        if not m or not self.allowed(m):
            return
        self.chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if m.get("forward_origin") or m.get("forward_from_chat"):
            return self.save_favorite(m)
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            fn = getattr(self, "cmd_" + cmd[1:], None)
            if fn:
                vault.log_event("💬", "command", cmd)
                return fn(arg.strip(), m)
            if self.teacher and self.teacher.command(cmd, arg.strip(), m):
                return
            return self.reply("I do not know that command. /help lists them.")
        if m.get("document"):
            return self.document(m)
        if m.get("photo") or m.get("media_group_id"):
            if self.teacher:
                return self.teacher.photo(m)
            return self.reply("The photo teacher is not installed yet. Send the original as a file for a settings readout.")
        if self.teacher and text:
            return self.teacher.question(text, m)
        self.reply("Send a photo or a file, or /help for the commands.")

    def allowed(self, m: dict) -> bool:
        """The owner in the private chat, or the owner inside the bot's own topic of the group. Nobody else."""
        if m.get("from", {}).get("id") != self.owner:
            return False
        chat = m.get("chat", {})
        if chat.get("type") == "private":
            return True
        if str(chat.get("id")) == str(self.s.telegram.chat_id):
            thread = self.s.telegram.message_thread_id
            return thread is None or m.get("message_thread_id") == thread
        return False

    def reply(self, html: str, **kw) -> int:
        return self.x.tg.send(self.chat, html, parse_mode="HTML", **kw)

    # commands -----------------------------------------------------------------------------------------------
    def cmd_help(self, arg, m):
        self.reply(HELP)

    def cmd_start(self, arg, m):
        self.reply(HELP)

    def cmd_today(self, arg, m):
        today = self.x.lim.today()
        row = self.x.conn.execute("SELECT text_html FROM queue WHERE slot_at LIKE ? AND type='brief'", (f"{today}%",)).fetchone()
        self.reply(row["text_html"] if row else "No brief for today yet, the plan job runs at 05:30.")

    def cmd_recipe(self, arg, m):
        want = RECIPE_LIGHTS.get(arg.lower()) if arg else None
        if arg and not want:
            return self.reply("Choose one of: " + ", ".join(RECIPE_LIGHTS))
        rows = [dict(r) for r in self.x.conn.execute("SELECT * FROM recipes")]
        if want == "bw":
            rows = [r for r in rows if r["film_simulation"].startswith(("ACROS", "MONOCHROME", "SEPIA"))]
        elif want:
            fit = [r for r in rows if want in json.loads(r["light_tags_json"] or "[]")]
            rows = fit or [r for r in rows if "any" in json.loads(r["light_tags_json"] or "[]")] or rows
        if not rows:
            return self.reply("No recipe in the library for that light yet.")
        r = random.choice(rows)
        facts = planner.recipe_facts(self.x.conn, r, want or "any")
        text = render.render("recipe", facts)
        self.reply(text, preview_url=r["source_url"], above=self.s.telegram.preview_above_text)

    def cmd_tip(self, arg, m):
        row = self.x.conn.execute("SELECT * FROM tips ORDER BY RANDOM() LIMIT 1").fetchone()
        if not row:
            return self.reply("No tips in the library yet.")
        facts = json.loads(row["facts_json"])
        text = render.render("camera_tip", {"topic": row["topic"], "title": row["title"], "why": facts[0],
                                            "how_detail": " ".join(facts[1:]), "menu_path": row["menu_path"],
                                            "manual_url": row["manual_url"]})
        self.reply(text)

    def cmd_slots(self, arg, m):
        rows = self.x.conn.execute("SELECT b.bank, r.name, r.film_simulation FROM custom_banks b LEFT JOIN recipes r "
                                   "ON r.id=b.recipe_id ORDER BY b.bank").fetchall()
        by = {r["bank"]: r for r in rows}
        lines = ["🎞 <b>CUSTOM BANKS</b> · what you loaded", ""]
        for n in range(1, 8):
            r = by.get(n)
            lines.append(f"C{n} · <b>{esc(r['name'])}</b> · {esc(r['film_simulation'])}" if r and r["name"] else f"C{n} · <i>empty</i>")
        lines.append("<i>/setc 3 Recipe Name records a bank</i>")
        self.reply("\n".join(lines))

    def cmd_setc(self, arg, m):
        mm = re.match(r"([1-7])\s+(.+)", arg)
        if not mm:
            return self.reply("Use /setc 1 to 7 followed by the recipe name, for example /setc 3 Easy Reala Ace")
        bank, name = int(mm.group(1)), mm.group(2).strip()
        row = self.x.conn.execute("SELECT id, name FROM recipes WHERE lower(name)=lower(?)", (name,)).fetchone() or \
            self.x.conn.execute("SELECT id, name FROM recipes WHERE lower(name) LIKE lower(?)", (f"%{name}%",)).fetchone()
        if not row:
            return self.reply(f"No library recipe called {esc(name)}. Try /recipe to see names.")
        self.x.conn.execute("INSERT OR REPLACE INTO custom_banks(bank, recipe_id, set_at) VALUES(?,?,?)",
                            (bank, row["id"], self.x.lim.clock()))
        self.reply(f"C{bank} is now <b>{esc(row['name'])}</b>. Recipe posts will say so when it comes up.")

    def save_favorite(self, m):
        origin = m.get("forward_origin") or {}
        mid = origin.get("message_id") or m.get("forward_from_message_id")
        q = self.x.conn.execute("SELECT id FROM queue WHERE message_id=?", (mid,)).fetchone() if mid else None
        self.x.conn.execute("INSERT INTO favorites(message_id, queue_id, saved_at) VALUES(?,?,?)",
                            (mid, q["id"] if q else None, self.x.lim.clock()))
        self.reply("⭐ Saved to your favourites. /favorites lists them.")

    def cmd_favorites(self, arg, m):
        rows = self.x.conn.execute("SELECT f.message_id, q.type, q.facts_json FROM favorites f LEFT JOIN queue q ON "
                                   "q.id=f.queue_id ORDER BY f.id DESC LIMIT 20").fetchall()
        if not rows:
            return self.reply("No favourites yet. Forward a channel post to me to save it.")
        chat = str(self.s.telegram.chat_id)
        base = f"https://t.me/c/{chat[4:]}" if chat.startswith("-100") else f"https://t.me/{chat.lstrip('@')}"
        lines = ["⭐ <b>FAVOURITES</b> · last 20", ""]
        for r in rows:
            facts = json.loads(r["facts_json"] or "{}")
            label = facts.get("recipe_name") or facts.get("name") or facts.get("video_title") or (r["type"] or "post")
            lines.append(f"• <a href=\"{base}/{r['message_id']}\">{esc(label)}</a>" + (f" · {esc(r['type'])}" if r["type"] else ""))
        self.reply("\n".join(lines))

    def cmd_done(self, arg, m):
        today = self.x.lim.today()
        row = self.x.conn.execute("SELECT fields_json FROM queue WHERE slot_at LIKE ? AND type='brief'", (f"{today}%",)).fetchone()
        text = json.loads(row["fields_json"] or "{}").get("assignment") if row else None
        self.x.conn.execute("INSERT INTO assignments(date, text, done_at) VALUES(?,?,?) ON CONFLICT(date) DO UPDATE SET "
                            "done_at=excluded.done_at", (today, text, self.x.lim.clock()))
        week = self.x.conn.execute("SELECT COUNT(*) FROM assignments WHERE done_at IS NOT NULL AND date>=?",
                                   ((datetime.now(self.x.lim.tz).date() - timedelta(days=datetime.now(self.x.lim.tz).weekday())).isoformat(),)).fetchone()[0]
        self.reply(f"✅ Logged today's assignment. {week} done this week.")

    def cmd_status(self, arg, m):
        from .status import report
        self.reply(f"<pre>{esc(report(self.s))}</pre>", parse_mode="HTML")

    # files --------------------------------------------------------------------------------------------------
    def document(self, m):
        doc = m["document"]
        name = (doc.get("file_name") or "").lower()
        if (doc.get("file_size") or 0) > MAX_FILE_BYTES:
            return self.reply("Telegram only lets me download files up to 20 MB. Send a JPEG at a smaller size, please.")
        if name.endswith(".raf"):
            return self.reply("I cannot read RAF files. Send the JPEG or HEIF the camera saved alongside it.")
        if not name.endswith((".jpg", ".jpeg", ".heic", ".heif", ".hif", ".png")):
            return self.reply("Send a JPEG, HEIF or PNG file for the settings readout.")
        if self.teacher and not (m.get("caption") or "").lower().startswith("#settings"):
            return self.teacher.photo(m)
        self.settings_readout(doc["file_id"], name)

    def settings_readout(self, file_id: str, name: str):
        if not exif.available():
            return self.reply("exiftool is not installed on this host, so I cannot read the settings.")
        data = self.x.tg.get_file_bytes(file_id)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ("photo" + Path(name).suffix)
            p.write_bytes(data)
            fields = exif.read(p)
        shot = exif.to_recipe_like(fields)
        closest, dist = exif.closest_recipe(self.x.conn, shot)
        lines = ["📷 <b>WHAT YOU SHOT</b> · from the file's EXIF", ""]
        for label, key in (("Camera", "model"), ("Aperture", "aperture"), ("Shutter", "shutter_text"), ("ISO", "iso"),
                           ("Exposure comp", "exposure_compensation"), ("Film Simulation", "film_simulation"),
                           ("Grain", "grain"), ("Color Chrome", "color_chrome_effect"), ("FX Blue", "color_chrome_fx_blue"),
                           ("White Balance", "white_balance"), ("WB fine tune", "wb_fine_tune"),
                           ("Dynamic Range", "dynamic_range"), ("Highlight", "highlight"), ("Shadow", "shadow"),
                           ("Color", "color"), ("Sharpness", "sharpness"), ("NR", "noise_reduction"), ("Clarity", "clarity")):
            if fields.get(key) not in (None, ""):
                lines.append(f"{label} <code>{esc(fields[key])}</code>")
        if closest:
            lines += ["", f"🎞 Closest library recipe: <b>{esc(closest['name'])}</b> · {esc(closest['film_simulation'])} "
                          f"<i>(distance {dist:.0f})</i>", f'<a href="{esc(closest["source_url"])}">Full recipe</a>']
        self.reply("\n".join(lines))


def listen(s, stop=None) -> None:
    """The always on private chat listener thread."""
    from .cli import Ctx
    x = Ctx(s, "bot")
    if not x.tg:
        log.warning("no TELEGRAM_BOT_TOKEN, the private chat listener is off")
        return
    if not s.telegram.owner_user_id:
        log.warning("telegram.owner_user_id is 0, the bot answers nobody")
    bot = Bot(x)
    x.tg.poll(x.conn, bot.handle, stop)
