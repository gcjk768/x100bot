"""`x100bot status`: budgets per bucket, cooldowns, library stock against min_stock, today's queue, next runs."""
from __future__ import annotations

from datetime import datetime

from .cli import Ctx
from .scheduler import triggers


def report(s) -> str:
    x = Ctx(s, "status", offline=True)
    lim = x.lim
    when = lambda ts: datetime.fromtimestamp(ts, lim.tz).strftime("%Y-%m-%d %H:%M") if ts else "never"
    out = [f"Budgets today ({lim.today()}):"]
    for name in ("web", "openmeteo", "telegram", "claude", "critique"):
        r, st = lim.rule(name), lim.state(name)
        cap = f"/{r.per_day}" if r.per_day else ""
        out.append(f"  {name:10} {st['day_count']}{cap}")
    cools = lim.cooldowns()
    out.append("Cooldowns: " + (", ".join(f"{c['bucket']} until {when(c['cooldown_until'])} ({c['cooldown_reason']})"
                                          for c in cools) if cools else "none"))
    from .research import stock
    st = stock(x.conn)
    out.append("Library stock (min_stock):")
    for k, v in s.library.min_stock.items():
        flag = "" if st.get(k, 0) >= v else "  LOW"
        out.append(f"  {k:13} {st.get(k, 0):4} ({v}){flag}")
    today = lim.today()
    rows = x.conn.execute("SELECT slot_at, type, status, message_id, used_fallback FROM queue WHERE slot_at LIKE ? "
                          "ORDER BY slot_at", (f"{today}%",)).fetchall()
    out.append(f"Queue for {today}: " + ("empty, the plan job has not run" if not rows else ""))
    for r in rows:
        out.append(f"  {r['slot_at'][-5:]} {r['type']:13} {r['status']:8}" + (f" msg {r['message_id']}" if r["message_id"] else "")
                   + ("  fallback" if r["used_fallback"] else ""))
    now = datetime.now(lim.tz)
    for name, t in triggers(s).items():
        nxt = t.get_next_fire_time(None, now)
        out.append(f"Next {name}: {nxt.strftime('%Y-%m-%d %H:%M') if nxt else 'none'}")
    return "\n".join(out)
