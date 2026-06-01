"""Shared helpers for the Uniswap V4 approval-cost fetch (Ethereum-only).

Window-date math and an output-exists check used by fetch.timeline.
"""
from __future__ import annotations

from datetime import date

from dexresearch import gcs
from dexresearch.config import get_settings, load_study

CHAIN = "ethereum"


def output_exists(blob: str) -> bool:
    """True if a stage's output parquet is already present (local cache or GCS).

    Lets a rerun skip work that's already done — re-reading results costs credits
    too, so this is a credit-preventative measure, not just a speedup.
    """
    local_path = get_settings().DATA_DIR / blob
    if local_path.exists():
        print(f"[check]  local cache hit: {local_path}")
        return True
    print(f"[check]  not in local cache, checking GCS: {blob}")
    try:
        exists = gcs.blob_exists(blob)
        print(f"[check]  GCS: {'found' if exists else 'not found'} — {blob}")
        return exists
    except Exception:  # noqa: BLE001 — no creds / offline => treat as absent
        print(f"[check]  GCS check failed (no creds / offline), treating as absent: {blob}")
        return False


def month_bounds(month: str) -> tuple[str, str]:
    """('2025-11') -> ('2025-11-01', '2025-12-01'): inclusive start, exclusive end."""
    y, m = (int(p) for p in month.split("-"))
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start.isoformat(), end.isoformat()


def minus_months(month: str, n: int) -> str:
    """First-of-month n months before `month`, as an ISO date string."""
    y, m = (int(p) for p in month.split("-"))
    total = y * 12 + (m - 1) - n
    return date(total // 12, total % 12 + 1, 1).isoformat()


def study_window() -> tuple[list[str], str, str, str]:
    """Return (months, window_start, window_end, lookback_start) as ISO dates."""
    window = load_study()["window"]
    months = list(window["months"])
    window_start, _ = month_bounds(months[0])
    _, window_end = month_bounds(months[-1])
    lookback_start = minus_months(months[0], window.get("approvals_lookback_months", 18))
    return months, window_start, window_end, lookback_start
