"""Async, resumable, credit-aware runner for ad-hoc Dune SQL.

Runs the git SQL directly via Dune's Execute SQL endpoint
(POST /api/v1/sql/execute, Read scope — available on the community plan), so
there are no saved queries, no pasted query ids, and no manual dune.com step.
The templates in queries/ have their {{params}} substituted in first
(dune.render_named_sql), then the rendered text is submitted.

Credit discipline (no --execute gate — idempotency is the real protection):
  * the manifest (data/executions/manifest.json) caches each execution_id keyed
    by a hash of the exact SQL, so re-running never re-executes unchanged SQL.
  * result reads are PAGED (bounded page size) — avoids Dune's "response too
    large / not enough credits" error.
  * the fetch stages skip entirely when their output parquet already exists, so
    reruns don't re-read results (reads cost credits too).

Per job: submit (or reuse) -> poll status with backoff -> paged fetch. Jobs in a
stage run concurrently (ThreadPoolExecutor, max_concurrency).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import pandas as pd
import requests

from dexresearch.config import get_settings, load_study

API_BASE = "https://api.dune.com/api/v1"

_TERMINAL_OK = {"QUERY_STATE_COMPLETED"}
_TERMINAL_BAD = {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"}


@dataclass(frozen=True)
class RunnerConfig:
    performance: str | None = None     # None -> Dune default (medium)
    max_concurrency: int = 4
    page_size: int = 25000
    poll_initial_seconds: float = 2.0
    poll_max_seconds: float = 30.0
    poll_timeout_seconds: float = 3600.0   # ceiling for one execution (long query)
    max_retries: int = 5

    @classmethod
    def from_study(cls) -> "RunnerConfig":
        raw = load_study().get("dune") or {}
        known = {k: raw[k] for k in raw if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Job:
    """One execution unit: a manifest key plus the fully-rendered SQL to run."""

    key: str           # manifest identity, e.g. "uniswap_v4/timeline"
    sql: str           # SQL with {{params}} already substituted

    def sql_hash(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()[:16]


class CreditError(RuntimeError):
    """Dune signalled the credit / datapoint budget is exhausted — do not retry."""


class _Ended(RuntimeError):
    """Internal: execution reached a bad terminal state (FAILED/CANCELLED/EXPIRED)."""


class DuneRunner:
    def __init__(self, config: RunnerConfig | None = None) -> None:
        self.cfg = config or RunnerConfig.from_study()
        self._api_key = get_settings().DUNE_API_KEY
        self._manifest_path = get_settings().DATA_DIR / "executions" / "manifest.json"
        self._lock = threading.Lock()
        self._manifest: dict[str, Any] = self._load_manifest()

    # ---- manifest ---------------------------------------------------------
    def _load_manifest(self) -> dict[str, Any]:
        if self._manifest_path.exists():
            data = json.loads(self._manifest_path.read_text())
            print(f"[manifest] loaded {len(data)} entr{'y' if len(data)==1 else 'ies'} from {self._manifest_path}")
            return data
        print(f"[manifest] no manifest at {self._manifest_path}, starting fresh")
        return {}

    def _write_manifest(self) -> None:
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(json.dumps(self._manifest, indent=2, default=str))

    def _record(self, job: Job, **fields: Any) -> None:
        with self._lock:
            entry = self._manifest.get(job.key, {})
            entry.update(sql_hash=job.sql_hash(), **fields)
            self._manifest[job.key] = entry
            self._write_manifest()

    def _forget(self, job: Job) -> None:
        with self._lock:
            self._manifest.pop(job.key, None)
            self._write_manifest()

    def _cached_execution(self, job: Job) -> str | None:
        entry = self._manifest.get(job.key)
        if (
            entry
            and entry.get("sql_hash") == job.sql_hash()
            and entry.get("execution_id")
            and entry.get("state") not in _TERMINAL_BAD
        ):
            return entry["execution_id"]
        return None

    def execution_id(self, key: str) -> str | None:
        """The execution_id recorded for a job key (for upload metadata)."""
        return (self._manifest.get(key) or {}).get("execution_id")

    # ---- HTTP -------------------------------------------------------------
    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Dune-Api-Key": self._api_key}

    def _request(self, method: str, url: str, **kw: Any) -> requests.Response:
        delay, last = 1.0, ""
        for _ in range(self.cfg.max_retries + 1):
            resp = requests.request(method, url, headers=self._headers, timeout=60, **kw)
            if resp.status_code == 200:
                return resp
            body = resp.text[:500]
            last = f"{resp.status_code}: {body}"
            if resp.status_code == 402 or "credit" in body.lower():
                raise CreditError(last)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            resp.raise_for_status()
        raise RuntimeError(f"Dune API request failed after retries: {last}")

    # ---- primitives -------------------------------------------------------
    def submit(self, job: Job) -> str:
        """Return an execution_id, reusing the manifest's when the SQL is unchanged."""
        cached = self._cached_execution(job)
        if cached:
            print(f"[submit] '{job.key}' — cache hit, reusing execution_id={cached}")
            return cached
        print(f"[submit] '{job.key}' — submitting SQL to Dune (hash={job.sql_hash()}, {len(job.sql)} chars)")
        payload: dict[str, Any] = {"sql": job.sql}
        if self.cfg.performance:
            payload["performance"] = self.cfg.performance
        resp = self._request("POST", f"{API_BASE}/sql/execute", json=payload)
        execution_id = resp.json()["execution_id"]
        print(f"[submit] '{job.key}' — accepted, execution_id={execution_id}")
        self._record(job, execution_id=execution_id, state="QUERY_STATE_PENDING", submitted_at=_now())
        return execution_id

    def wait(self, job: Job, execution_id: str) -> str:
        delay = self.cfg.poll_initial_seconds
        deadline = time.monotonic() + self.cfg.poll_timeout_seconds
        start = time.monotonic()
        poll_n = 0
        print(f"[wait]   '{job.key}' — polling execution_id={execution_id}")
        while True:
            state = self._request(
                "GET", f"{API_BASE}/execution/{execution_id}/status"
            ).json().get("state", "")
            self._record(job, execution_id=execution_id, state=state)
            elapsed = time.monotonic() - start
            poll_n += 1
            print(f"[wait]   '{job.key}' poll #{poll_n}: state={state} (elapsed={elapsed:.1f}s)")
            if state in _TERMINAL_OK:
                print(f"[wait]   '{job.key}' — completed in {elapsed:.1f}s after {poll_n} poll(s)")
                return state
            if state in _TERMINAL_BAD:
                print(f"[wait]   '{job.key}' — FAILED with state={state} after {elapsed:.1f}s")
                raise _Ended(state)
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"{job.key}: execution {execution_id} not done in "
                    f"{self.cfg.poll_timeout_seconds}s (state={state})"
                )
            time.sleep(delay)
            delay = min(delay * 2, self.cfg.poll_max_seconds)

    def fetch_pages(
        self,
        execution_id: str,
        *,
        start_offset: int = 0,
        start_page: int = 0,
        columns: Sequence[str] | None = None,
        filters: str | None = None,
    ):
        """Yield (page_n, offset, page_df, next_offset) for each result page.

        Callers control what to do with each page (save to GCS, accumulate in
        memory, etc.).  start_offset / start_page let a caller resume mid-way.

        columns/filters are passed to Dune's results endpoint so only the
        requested cells are read — billed datapoints are rows x columns
        INCLUDING nulls, so projecting away a wide superset's padding is the
        main read-cost lever.
        """
        offset = start_offset
        page_n = start_page
        print(f"[fetch]  execution_id={execution_id} — "
              f"{'resuming' if start_offset else 'starting'} paged fetch "
              f"(page_size={self.cfg.page_size}, offset={offset:,}"
              f"{', projected' if columns else ''}{', filtered' if filters else ''})")
        while True:
            params: dict[str, Any] = {
                "limit": self.cfg.page_size, "offset": offset, "allow_partial_results": "true",
            }
            if columns:
                params["columns"] = ",".join(columns)
            if filters:
                params["filters"] = filters
            body = self._request(
                "GET",
                f"{API_BASE}/execution/{execution_id}/results",
                params=params,
            ).json()
            page = body.get("result", {}).get("rows", [])
            nxt = body.get("next_offset")
            page_n += 1
            yield page_n, offset, pd.DataFrame(page), nxt
            if not page or nxt is None:
                break
            offset = nxt

    # ---- orchestration ----------------------------------------------------
    def _run_one(self, job: Job) -> pd.DataFrame:
        print(f"[job]    '{job.key}' — starting")
        execution_id = self.submit(job)
        try:
            self.wait(job, execution_id)
        except _Ended:
            print(f"[job]    '{job.key}' — stale execution ended, re-submitting fresh")
            self._forget(job)
            execution_id = self.submit(job)
            self.wait(job, execution_id)
        rows = []
        for _, _, page_df, _ in self.fetch_pages(execution_id):
            rows.append(page_df)
        df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        self._record(job, rows=len(df), fetched_at=_now())
        print(f"[job]    '{job.key}' — done, {len(df):,} rows stored")
        return df

    def run_jobs(
        self,
        jobs: Sequence[Job],
        *,
        progress: Callable[[str], None] = print,
    ) -> dict[str, pd.DataFrame]:
        """Submit/poll/fetch every job concurrently; return {job.key: DataFrame}."""
        print(f"[runner] launching {len(jobs)} job(s) with max_concurrency={self.cfg.max_concurrency}")
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=self.cfg.max_concurrency) as pool:
            futures = {pool.submit(self._run_one, j): j for j in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                df = fut.result()
                results[job.key] = df
                progress(f"[done]   {job.key}: {len(df):,} rows")
        print(f"[runner] all {len(jobs)} job(s) complete")
        return results


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
