"""Row and tooltip text, built from a Snapshot. See ui-spec.md §5/§7.

claudemon.swift 508-602. `Seg`, `Line`, `TITLE_DOTS` and `SPARKS` are real (drawing.py imports
`Seg`); `build_rows`/`compact_summary`/the tooltip functions are stubs, owned by task P3.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import List

from . import formatting as fmt
from . import layout
from . import theme
from .animation import Animator
from .model import Limits, Snapshot, Span


@dataclass
class Seg:
    text: str
    role: str = "text"
    bold: bool = False


@dataclass
class Line:
    segs: List[Seg]
    tip: List[str] = field(default_factory=list)
    alpha: float = 1.0


# ui-spec.md §4 "Dots": "● " hot, "● " warn, "●" green_dot.
TITLE_DOTS: List[Seg] = [
    Seg("● ", "hot"),
    Seg("● ", "warn"),
    Seg("●", "green_dot"),
]

SPARKS = "▁▂▃▄▅▆▇█"

# Activity amendment B/F6: actions are stored in full and clipped only here.
SESSION_ACTION_CAP = 48
AGENT_ACTION_CAP = 40


def _label(text: str) -> Seg:
    """claudemon.swift 383: text padded/truncated to 10 chars, label role, bold."""
    padded = text[:10].ljust(10)
    return Seg(padded, "label", True)


def build_rows(snap: Snapshot, anim: Animator, now: float, spin_phase: bool) -> List[Line]:
    """Build the full-mode row list (Sessions, Tokens, in/out, Speed, Models, Agents, agent rows,
    Projects). See ui-spec.md §5."""
    if not snap.loaded:
        return [Line([Seg("Reading Claude Code logs…", "dim")])]

    lines: List[Line] = []

    # 1. Sessions [claudemon.swift 513-522]
    ctx = anim.num("ctx", snap.context)
    ses_segs = [_label("Sessions"), Seg(str(snap.live_sessions), "fg", True), Seg(" live", "dim")]
    if snap.busy_sessions > 0:
        ses_segs += [Seg(" · ", "dim"), Seg(f"{snap.busy_sessions} busy", "warn")]
    if ctx > 0:
        ses_segs += [
            Seg(" · ctx ", "dim"),
            Seg(fmt.tokens(ctx), "text"),
            Seg(" " + fmt.clip(snap.context_project, 16).strip(), "dim"),
        ]
    ses_tip = [
        "Sessions",
        f"{snap.live_sessions} running now, {snap.busy_sessions} busy",
        f"Latest conversation: {fmt.exact(snap.context)} tokens of context ({snap.context_project})"
        if snap.context > 0
        else "No conversation yet",
    ]
    lines.append(Line(ses_segs, ses_tip))

    # 1b. "Now" block: live sessions with their running agents (activity spec §2)
    lines += now_lines(snap, anim, now, spin_phase)

    # 2. Tokens [claudemon.swift 524-534] and 3. in/out line [535-537]
    usage_tip = [
        f"Tokens today ({fmt.exact(snap.today_replies)} replies)",
        f"Input:        {fmt.exact(snap.today.input)}",
        f"Cache writes: {fmt.exact(snap.today.cache_write)}",
        f"Cache reads:  {fmt.exact(snap.today.cache_read)}",
        f"Output:       {fmt.exact(snap.today.output)}",
        f"5h window since {fmt.hm(snap.window_start)} (estimate): {fmt.exact(snap.window)}"
        if snap.window_start is not None
        else "No active 5h window",
    ]
    tok_segs = [
        _label("Tokens"),
        Seg("today ", "dim"),
        Seg(fmt.tokens(anim.num("today", snap.today.total)), "fg", True),
    ]
    if snap.window_reset is not None:
        tok_segs += [
            Seg("  5h ", "dim"),
            Seg(fmt.tokens(anim.num("window", snap.window)), "fg", True),
            Seg(f" · resets ~{fmt.hm(snap.window_reset)}", "dim"),
        ]
    lines.append(Line(tok_segs, usage_tip))

    inout_segs = [
        Seg(" " * 10, "text"),
        Seg("in ", "dim"),
        Seg(fmt.tokens(snap.today.input + snap.today.cache_write), "text"),
        Seg("  out ", "dim"),
        Seg(fmt.tokens(snap.today.output), "text"),
        Seg("  cache ", "dim"),
        Seg(fmt.tokens(snap.today.cache_read), "text"),
    ]
    lines.append(Line(inout_segs, usage_tip))

    # 3b. Official limits (activity spec §3)
    if snap.limits is not None:
        lines.append(limits_line(snap.limits, now))

    # 4. Speed [claudemon.swift 539-544]
    speed = anim.num("speed", snap.per_minute)
    speed_segs = [
        _label("Speed"),
        Seg(fmt.tokens(speed), "fg" if speed > 0 else "dim", True),
        Seg("/min now", "dim"),
        Seg(" · peak ", "dim"),
        Seg(fmt.tokens(anim.num("peak", snap.peak_per_minute)), "text"),
        Seg("/min", "dim"),
    ]
    speed_tip = [
        "Speed",
        f"Now: {fmt.exact(snap.per_minute)} tokens/min (average of the last 5 minutes)",
        f"Peak: {fmt.exact(snap.peak_per_minute)} tokens/min (busiest minute in the last hour)",
    ]
    lines.append(Line(speed_segs, speed_tip))

    # 5. Models [claudemon.swift 546-563]
    total = max(1, sum(m.tokens for m in snap.models))
    models_segs = [_label("Models")]
    if not snap.models:
        models_segs.append(Seg("no replies today", "dim"))
    else:
        used = 0
        last = len(snap.models) - 1
        for i, m in enumerate(snap.models):
            w = (18 - used) if i == last else max(1, m.tokens * 18 // total)
            models_segs.append(Seg("█" * max(0, w), theme.model_role(m.name)))
            used += w
        for m in snap.models[:3]:
            pct = "<1%" if m.tokens * 100 < total else f"{m.tokens * 100 // total}%"
            models_segs.append(Seg(f" {m.name} ", theme.model_role(m.name)))
            models_segs.append(Seg(pct, "dim"))
    models_tip = ["Models today"] + [
        f"{m.name}: {fmt.exact(m.tokens)} tokens · {m.replies} replies · "
        f"{'<1' if m.tokens * 100 < total else str(m.tokens * 100 // total)}%"
        for m in snap.models
    ]
    lines.append(Line(models_segs, models_tip))

    # 6. Agents summary [claudemon.swift 565-567]
    running = layout.running_agents(snap)
    agents_segs = [
        _label("Agents"),
        Seg(str(running), "warn" if running > 0 else "dim", True),
        Seg(" running", "dim"),
        Seg(" · ", "dim"),
        Seg(str(len(snap.agents_today)), "text"),
        Seg(" today", "dim"),
    ]
    agents_tip = ["Subagents", f"{running} running now", f"{len(snap.agents_today)} run today"]
    lines.append(Line(agents_segs, agents_tip))

    # 7. Agent rows, first 4 [claudemon.swift 568-579]
    for a in snap.agents_today[:4]:
        icon = ("◐ " if spin_phase else "◓ ") if a.running else "✓ "
        row_segs = [
            Seg("  "),
            Seg(icon, "warn" if a.running else "fg"),
            Seg(fmt.clip(a.type, 15), "text" if a.running else "dim"),
            Seg(" " + fmt.clip(a.task, 22), "dim"),
            Seg(" " + fmt.tokens(a.tokens).ljust(6), "text" if a.running else "dim"),
            Seg(fmt.duration(now - a.start) if a.running else "", "warn"),
        ]
        lines.append(Line(row_segs, _agent_tip(a, now), anim.fade(a.id, now)))

    # 8. Projects [claudemon.swift 581-588]
    if snap.projects:
        proj_segs = [_label("Projects")]
        for i, p in enumerate(snap.projects[:2]):
            if i > 0:
                proj_segs.append(Seg(" · ", "dim"))
            proj_segs.append(Seg(fmt.clip(p.name, 16).strip(), "text"))
            proj_segs.append(Seg(f" {fmt.tokens(p.tokens)}", "dim"))
        proj_tip = ["Projects today"] + [
            f"{p.name}: {fmt.exact(p.tokens)} tokens" for p in snap.projects[:6]
        ]
        lines.append(Line(proj_segs, proj_tip))

    return lines


def _agent_tip(a: Span, now: float) -> List[str]:
    """The Agents-row tooltip for one agent [claudemon.swift 568-579]."""
    # Note the "Ran " prefix here differs from span_tooltip (ui-spec.md §7 has none).
    return [
        a.type,
        a.task or "(no description)",
        f"Model: {a.family} · {fmt.exact(a.tokens)} tokens",
        f"Running for {fmt.duration(now - a.start)}"
        if a.running
        else f"Ran {fmt.hm(a.start)}–{fmt.hm(a.end)} ({fmt.duration(a.end - a.start)})",
    ]


def now_lines(snap: Snapshot, anim: Animator, now: float, spin_phase: bool) -> List[Line]:
    """The "Now" block (activity spec §2 and F1-F6): one line per live session, each followed by
    its running subagents (newest first) as a small tree. Empty when no session is live."""
    out: List[Line] = []
    for ses in snap.now_sessions:
        # F5: project in the normal text colour, action and elapsed dim; busy only colours the dot.
        segs = [
            Seg("  "),
            Seg("● ", "warn" if ses.busy else "dim"),
            Seg(fmt.clip(ses.project, 16).strip(), "text"),
        ]
        tip = [ses.project]
        if ses.name:
            tip.append(f"Session: {ses.name}")
        if ses.cwd:
            tip.append(ses.cwd)
        tip.append(f"Status: {ses.status or ('busy' if ses.busy else 'idle')}")
        if ses.action is not None:
            segs.append(Seg(" · " + fmt.clip_text(ses.action, SESSION_ACTION_CAP), "dim"))
            tip.append(ses.action)
        if ses.action_time is not None:
            segs.append(Seg(" · " + fmt.duration(now - ses.action_time), "dim"))
            tip.append(f"since {fmt.clock(ses.action_time)}")
        out.append(Line(segs, tip))

        last = len(ses.agents) - 1
        for i, ag in enumerate(ses.agents):
            a = ag.span
            branch = "└─ " if i == last and ses.more_agents == 0 else "├─ "
            asegs = [
                Seg("    " + branch, "dim"),
                Seg("◐ " if spin_phase else "◓ ", "warn"),
                Seg(fmt.clip_text(fmt.sanitize(a.type), 20), "text"),
            ]
            atip = _agent_tip(a, now)
            if ag.action is not None:  # F2: no ": <action>" without an action; G6: the ":" is dim
                asegs.append(Seg(": " + fmt.clip_text(ag.action, AGENT_ACTION_CAP) + " · ", "dim"))
                atip.append(f"Now: {ag.action}")
            else:
                asegs.append(Seg(" · ", "dim"))
            asegs.append(Seg(fmt.duration(now - a.start), "warn"))
            out.append(Line(asegs, atip, anim.fade(a.id, now)))
        if ses.more_agents > 0:
            out.append(Line([Seg(f"    └─ +{ses.more_agents} more", "dim")]))
    if snap.more_sessions > 0:
        out.append(Line([Seg(f"  +{snap.more_sessions} more sessions", "dim")]))
    return out


def _pct_role(pct: int) -> str:
    """Meter colours: normal below 70, warn from 70, hot from 90."""
    if pct >= 90:
        return "hot"
    if pct >= 70:
        return "warn"
    return "text"


def _local(t: float, pattern: str) -> str:
    """Local time t formatted with strftime pattern; "?" when t is outside the platform's range
    (resets_at comes from a user-writable file)."""
    try:
        return datetime.datetime.fromtimestamp(t).strftime(pattern)
    except (OverflowError, OSError, ValueError):
        return "?"


def _long_reset(t: float) -> str:
    """"Wed 1 Oct 02:52:00"."""
    try:
        d = datetime.datetime.fromtimestamp(t)
    except (OverflowError, OSError, ValueError):
        return "?"
    return f"{fmt.day_long(d.date())} {d.strftime('%H:%M:%S')}"


def limits_line(limits: Limits, now: float) -> Line:
    """The Limits row (activity spec §3): only the windows present, percent rounded half-up."""
    segs = [_label("Limits")]
    tip = [
        "Official Claude usage from Claude Code's status line",
        f"Updated {int(max(0.0, now - limits.updated_at))}s ago",
    ]
    parts = (
        ("5h", "5-hour", limits.five_hour, "%H:%M"),
        ("week", "7-day", limits.seven_day, "%a %H:%M"),
    )
    first = True
    for short, long_name, w, when in parts:
        if w is None:
            continue
        pct = fmt.round_half_up(w.used_percentage)
        if not first:
            segs.append(Seg(" · ", "dim"))
        first = False
        segs += [
            Seg(short + " ", "dim"),
            Seg(f"{pct}%", _pct_role(pct), True),
            Seg(" · resets " + _local(w.resets_at, when), "dim"),
        ]
        tip.append(f"{long_name}: {w.used_percentage:g}% used, resets {_long_reset(w.resets_at)}")
    return Line(segs, tip)


def compact_summary(snap: Snapshot, anim: Animator, spin_phase: bool) -> List[Seg]:
    """Build the compact-mode summary segments (speed, sparkline, live count, running agents).
    See ui-spec.md §5 "Compact summary"."""
    if not snap.loaded:
        return [Seg("loading…", "dim")]

    segs = [Seg(fmt.tokens(anim.num("speed", snap.per_minute)), "fg", True), Seg("/min ", "dim")]

    recent = snap.last_hour[-20:]
    peak = max(1, max(recent) if recent else 1)
    for v in recent:
        if v == 0:
            segs.append(Seg("·", "dim"))
        else:
            segs.append(Seg(SPARKS[min(7, v * 8 // (peak + 1))], "fg"))

    segs.append(Seg(f"  {snap.live_sessions} live", "dim"))

    running = layout.running_agents(snap)
    if running > 0:
        icon = "◐" if spin_phase else "◓"
        segs.append(Seg(f"  {icon} {running} agent{'' if running == 1 else 's'}", "warn"))

    return segs


def chart_tooltip(snap: Snapshot, i: int, now: float) -> List[str]:
    """Tooltip lines for line-chart bucket i. See ui-spec.md §7."""
    end = now - (len(snap.main) - 1 - i) * snap.bucket_seconds
    start = end - snap.bucket_seconds
    first = fmt.hm(end) if snap.bucket_seconds <= 60 else f"{fmt.hm(start)}–{fmt.hm(end)}"
    lines = [first, f"Main session: {fmt.tokens(int(snap.main[i]))}/min"]
    if layout.has_agent_series(snap):
        lines.append(f"Agents: {fmt.tokens(int(snap.agent_series[i]))}/min")
    return lines


def span_tooltip(span: Span, now: float) -> List[str]:
    """Tooltip lines for an agent timeline span. See ui-spec.md §7."""
    return [
        span.type,
        span.task or "(no description)",
        f"Model: {span.family} · {fmt.exact(span.tokens)} tokens",
        f"Running for {fmt.duration(now - span.start)}"
        if span.running
        else f"{fmt.hm(span.start)}–{fmt.hm(span.end)} ({fmt.duration(span.end - span.start)})",
    ]


def heat_tooltip(snap: Snapshot, d: int, h: int) -> List[str]:
    """Tooltip lines for heatmap cell (day d, hour h). See ui-spec.md §7."""
    return [
        f"{fmt.day_long(snap.heat_days[d])}, {h:02d}:00–{(h + 1) % 24:02d}:00",
        f"{fmt.exact(snap.heat[d][h])} tokens",
    ]
