"""Subscription quota (Claude, ChatGPT): used share of the rolling windows.

A subscription has no pay-per-use cost, but a cap per window (5 hours, 7 days). The official
CLIs report its state; Synaptane never reads their credentials.

Format stored with each tidy-up pass:
    {"status": "allowed",
     "windows": {"five_hour": {"before": 3.0, "after": 5.0, "resets_at": 1791303600}, …}}
`before` and `after` are percentages (0 to 100); `resets_at`, a Unix timestamp.
"""

from typing import Any

LABELS = {"five_hour": "5 h", "seven_day": "7 days", "seven_day_opus": "7 days (Opus)"}
HIGH = 90.0  # above this, administrators are alerted

Windows = dict[str, dict[str, Any]]


def claude_windows(info: dict[str, Any]) -> Windows:
    """Windows of a Claude Code `rate_limit_event` event (`rate_limit_info`)."""
    windows: Windows = {}
    for name, window in (info.get("unifiedWindows") or {}).items():
        if isinstance(window, dict) and isinstance(window.get("utilization"), int | float):
            windows[str(name)] = {
                "used": round(float(window["utilization"]) * 100, 1),
                "resets_at": window.get("resetsAt"),
            }
    if not windows and isinstance(info.get("utilization"), int | float):
        name = str(info.get("rateLimitType") or "five_hour")
        windows[name] = {
            "used": round(float(info["utilization"]) * 100, 1),
            "resets_at": info.get("resetsAt"),
        }
    return windows


def span(first: Windows, last: Windows, status: str = "") -> dict[str, Any] | None:
    """Quota state over a run: first and last readings."""
    if not last:
        return None
    windows = {
        name: {
            "before": (first.get(name) or window)["used"],
            "after": window["used"],
            "resets_at": window.get("resets_at"),
        }
        for name, window in last.items()
    }
    return {"status": status, "windows": windows}


def merge(earlier: dict[str, Any] | None, later: dict[str, Any] | None) -> dict[str, Any] | None:
    """Two successive readings (a run made of several calls): the start of the first, the end
    of the second."""
    if not earlier or not later:
        return later or earlier
    windows = {}
    for name, window in later["windows"].items():
        before = (earlier["windows"].get(name) or window)["before"]
        windows[name] = {**window, "before": before}
    return {"status": later.get("status", ""), "windows": windows}


def highest(quota: dict[str, Any] | None) -> float:
    """Highest share reached, across all windows (0 if unknown)."""
    if not quota:
        return 0.0
    return max((float(w.get("after", 0)) for w in quota.get("windows", {}).values()), default=0.0)


def _percent(value: float, locale: str) -> str:
    return f"{value:g} %".replace(".", ",") if locale == "fr" else f"{value:g}%"


def summary(quota: dict[str, Any] | None, locale: str = "en") -> str:
    """ "5 h: 3% → 5% · 7 days: 29%" (the starting reading is shown only if it differs), with
    the language's labels and number format ("7 jours : 29 %" in French)."""
    from synaptane.web.i18n import translator

    if not quota:
        return ""
    translate = translator(locale)
    parts = []
    for name, window in quota.get("windows", {}).items():
        label = translate(LABELS[name]) if name in LABELS else name.replace("_", " ")
        before, after = float(window.get("before", 0)), float(window.get("after", 0))
        values = (
            _percent(after, locale)
            if before == after
            else f"{_percent(before, locale)} → {_percent(after, locale)}"
        )
        parts.append(f"{label} : {values}" if locale == "fr" else f"{label}: {values}")
    return " · ".join(parts)
