"""Routing from the private chat into the teacher queue: photos and files become critique jobs, a reply to a teacher
message becomes a follow up (or a compare when it carries a photo), an album becomes one series job, plain text
becomes a question, buttons become preset follow ups. Owner only (bot.py checks that before calling)."""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from . import teacher_queue
from .teacher import BUTTON_QUESTIONS, set_setting, setting

log = logging.getLogger(__name__)
FLAG_RE = re.compile(r"#(quick|settings|assignment|compare)\b", re.I)
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".heic", ".heif", ".hif", ".png")


class TeacherIntake:
    def __init__(self, x):
        self.x, self.s = x, x.s
        self.albums: dict[str, dict] = {}
        self.lock = threading.Lock()

    # helpers ----------------------------------------------------------------------------------------------------
    def reply(self, chat, text, **kw):
        return self.x.tg.send(chat, text, parse_mode="HTML", **kw)

    def critique_for_message(self, message_id: int | None) -> int | None:
        if not message_id:
            return None
        row = self.x.conn.execute("SELECT critique_id FROM teacher_messages WHERE message_id=?", (message_id,)).fetchone()
        return row["critique_id"] if row else None

    def flags(self, caption: str) -> dict:
        return {f.lower(): True for f in FLAG_RE.findall(caption or "")}

    def enqueue(self, chat, kind: str, payload: dict, quiet: bool = False) -> None:
        try:
            job_id, ahead = teacher_queue.enqueue(self.x, kind, payload)
        except teacher_queue.QueueFull as ex:
            return self.reply(chat, f"The teacher's queue is full ({ex}). Try again later.")
        if ahead and not quiet:
            self.reply(chat, f"Got it. {ahead} job{'s' if ahead > 1 else ''} ahead of this one in the teacher's queue.")
        self.kick()

    def kick(self) -> None:
        """Drain in a background thread so the listener keeps answering."""
        from .teacher import drain
        threading.Thread(target=lambda: drain(self.x), name="teacher-drain", daemon=True).start()

    # photos and files -----------------------------------------------------------------------------------------
    def photo(self, m: dict) -> None:
        chat = m["chat"]["id"]
        caption = m.get("caption") or ""
        flags = self.flags(caption)
        if m.get("media_group_id"):
            return self.album_part(m)
        reply_cid = self.critique_for_message((m.get("reply_to_message") or {}).get("message_id"))
        if m.get("document"):
            doc = m["document"]
            name = (doc.get("file_name") or "").lower()
            if (doc.get("file_size") or 0) > self.s.teacher.max_file_mb * 1024 * 1024:
                return self.reply(chat, f"Telegram only lets me download files up to {self.s.teacher.max_file_mb} MB. Send a JPEG at a smaller size, please.")
            if name.endswith(".raf"):
                return self.reply(chat, "I cannot read RAF files. Send the JPEG or HEIF the camera saved alongside it.")
            if not name.endswith(IMAGE_SUFFIXES):
                return self.reply(chat, "Send a JPEG, HEIF or PNG.")
            file_id, uid, source, suffix = doc["file_id"], doc.get("file_unique_id"), "file", Path(name).suffix or ".jpg"
        else:
            best = max(m["photo"], key=lambda ph: ph.get("file_size") or 0)
            file_id, uid, source, suffix = best["file_id"], best.get("file_unique_id"), "photo", ".jpg"
        payload = {"chat_id": chat, "message_id": m["message_id"], "file_id": file_id, "file_unique_id": uid,
                   "source": source, "suffix": suffix, "caption": FLAG_RE.sub("", caption).strip(), "flags": flags}
        if reply_cid and (flags.get("compare") or True):
            payload["parent_id"] = reply_cid
            return self.enqueue(chat, "compare", payload)
        self.enqueue(chat, "critique", payload)

    def album_part(self, m: dict) -> None:
        gid = m["media_group_id"]
        with self.lock:
            album = self.albums.setdefault(gid, {"chat": m["chat"]["id"], "parts": [], "caption": "", "timer": None})
            if m.get("photo"):
                best = max(m["photo"], key=lambda ph: ph.get("file_size") or 0)
                album["parts"].append({"file_id": best["file_id"], "source": "photo", "suffix": ".jpg"})
            elif m.get("document"):
                album["parts"].append({"file_id": m["document"]["file_id"], "source": "file",
                                       "suffix": Path(m["document"].get("file_name") or "a.jpg").suffix or ".jpg"})
            if m.get("caption"):
                album["caption"] = m["caption"]
            if album["timer"]:
                album["timer"].cancel()
            album["timer"] = threading.Timer(self.s.teacher.album_debounce_seconds, self.album_done, args=[gid])
            album["timer"].daemon = True
            album["timer"].start()

    def album_done(self, gid: str) -> None:
        with self.lock:
            album = self.albums.pop(gid, None)
        if not album:
            return
        parts = album["parts"][: self.s.teacher.series_max_images]
        if len(parts) < 2:
            return self.enqueue(album["chat"], "critique", {"chat_id": album["chat"], "caption": album["caption"], "flags": {},
                                                             **parts[0]})
        self.enqueue(album["chat"], "series", {"chat_id": album["chat"], "caption": album["caption"], "parts": parts})

    # text ---------------------------------------------------------------------------------------------------
    def question(self, text: str, m: dict) -> None:
        chat = m["chat"]["id"]
        reply_cid = self.critique_for_message((m.get("reply_to_message") or {}).get("message_id"))
        if reply_cid:
            return self.enqueue(chat, "followup", {"chat_id": chat, "critique_id": reply_cid, "question": text,
                                                   "reply_to": m["message_id"]}, quiet=True)
        self.enqueue(chat, "question", {"chat_id": chat, "question": text, "reply_to": m["message_id"]}, quiet=True)

    def callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        chat = cq["message"]["chat"]["id"]
        if data == "t:fa:0":
            from .progress import forget_all
            return self.reply(chat, f"Deleted everything the teacher stored ({forget_all(self.x)} critiques).")
        if data == "t:fx:0":
            return self.reply(chat, "Nothing deleted.")
        m = re.fullmatch(r"t:([sdwrc]):(\d+)", data)
        if not m:
            return
        code, cid = m.group(1), int(m.group(2))
        if code == "c":
            return self.enqueue(chat, "crop", {"chat_id": chat, "critique_id": cid, "reply_to": cq["message"]["message_id"]}, quiet=True)
        self.enqueue(chat, "followup", {"chat_id": chat, "critique_id": cid, "question": BUTTON_QUESTIONS[code],
                                        "reply_to": cq["message"]["message_id"]}, quiet=True)

    # commands -----------------------------------------------------------------------------------------------
    def command(self, cmd: str, arg: str, m: dict) -> bool:
        chat = m["chat"]["id"]
        if cmd == "/teach":
            self.reply(chat, TEACH_HELP)
        elif cmd == "/level":
            if arg not in ("beginner", "intermediate", "advanced"):
                self.reply(chat, "Use /level beginner, intermediate or advanced.")
            else:
                set_setting(self.x, "level", arg)
                self.reply(chat, f"Level set to <b>{arg}</b>.")
        elif cmd == "/style":
            if arg not in ("encouraging", "direct", "socratic"):
                self.reply(chat, "Use /style encouraging, direct or socratic.")
            else:
                set_setting(self.x, "style", arg)
                self.reply(chat, f"Style set to <b>{arg}</b>.")
        elif cmd == "/quick":
            if arg not in ("on", "off"):
                self.reply(chat, "Use /quick on or /quick off.")
            else:
                set_setting(self.x, "detail", "quick" if arg == "on" else "full")
                self.reply(chat, f"Quick mode <b>{arg}</b>.")
        elif cmd in ("/ask", "/plan"):
            if not arg:
                self.reply(chat, f"Add your {'question' if cmd == '/ask' else 'plan, for example /plan street portraits, Chinatown, tomorrow evening'} after the command.")
            else:
                self.enqueue(chat, "plan" if cmd == "/plan" else "question", {"chat_id": chat, "question": arg, "reply_to": m["message_id"]}, quiet=True)
        elif cmd in ("/progress", "/best", "/history", "/forget", "/forgetall"):
            try:
                from .progress import command as progress_command
                progress_command(self.x, cmd, arg, m, self)
            except ImportError:
                self.reply(chat, "That part of the teacher is not built yet.")
        else:
            return False
        return True


TEACH_HELP = """🎓 <b>PHOTO TEACHER</b> · how to use it

📎 Send the <b>original JPEG or HEIF as a file</b> for settings advice (photos lose their EXIF). Put your intent in the caption.
🏷 Caption flags: <code>#quick</code> short critique · <code>#settings</code> readout only · <code>#assignment</code> grade against today's assignment
💬 Reply to any teacher message with a question to ask more. Reply with a new photo to compare before and after.
🖼 Send an album of 2 to 8 photos for a series review.
❓ Any other text is a question. <code>/plan what, where, when</code> gives a shot plan with the day's light and forecast.
⚙️ <code>/level</code> beginner, intermediate or advanced · <code>/style</code> encouraging, direct or socratic · <code>/quick on|off</code>
📈 <code>/progress</code> · <code>/best</code> · <code>/history</code> · <code>/forget</code> (reply to a critique) · <code>/forgetall</code>"""
