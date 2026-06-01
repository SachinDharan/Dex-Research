"""Dune Analytics API client and SQL helpers.

Two execution paths live here:

  * Active (Uniswap V4) arms run SQL directly via Dune's Execute SQL endpoint —
    no saved query, no ids. `render_named_sql("<arm>/<dataset>", params)` reads
    queries/<arm>/<dataset>.sql and substitutes its {{params}}; the actual
    submit/poll/paged-fetch is done by dexresearch.dune_runner.
  * Legacy stages use saved queries by numeric id: write the SQL in
    queries/<name>.sql, save it on dune.com, record the id under
    `dune_queries.<name>` in config/study.yaml, then call
    run_named_query("<name>", params=[...]). Saved-query CRUD via API needs an
    Analyst plan, which is why the active arms avoid it.

Either way the SQL stays version-controlled and reviewable in queries/.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from dune_client.client import DuneClient
from dune_client.query import QueryBase
from dune_client.types import QueryParameter

from dexresearch.config import PROJECT_ROOT, get_settings, load_study


def client() -> DuneClient:
    return DuneClient(get_settings().DUNE_API_KEY)


def run_query(query_id: int, params: list[QueryParameter] | None = None) -> pd.DataFrame:
    """Execute a saved Dune query and return the result as a DataFrame."""
    query = QueryBase(query_id=query_id, params=params or [])
    return client().run_query_dataframe(query)


def _resolve_query_id(queries: dict, name: str) -> object:
    """Resolve a (possibly slash-namespaced) query name against dune_queries.

    Supports both a flat key ("gas_measurements") and a nested path that mirrors
    the queries/ tree ("<arm>/<dataset>" -> queries["<arm>"]["<dataset>"]).
    Returns None if the name isn't configured.
    """
    if name in queries:
        return queries[name]
    node: object = queries
    for part in name.split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve_query_id(name: str) -> int | None:
    """Public lookup of a configured query id by namespaced name, or None."""
    qid = _resolve_query_id(load_study().get("dune_queries", {}), name)
    return int(qid) if qid is not None else None


def run_named_query(name: str, params: list[QueryParameter] | None = None) -> pd.DataFrame:
    """Look up a query_id from study.yaml's `dune_queries` map and run it.

    `name` mirrors the queries/ tree and resolves to a numeric id under
    dune_queries — the legacy saved-query path. The active Uniswap V4 arm does
    NOT use this: it runs queries/uniswap_v4/timeline.sql via Execute SQL
    (dexresearch.dune_runner), so it needs no saved query and no id.
    """
    query_id = resolve_query_id(name)
    if query_id is None:
        raise ValueError(
            f"No Dune query_id configured for '{name}'. "
            f"Save the SQL from queries/{name}.sql on dune.com, then set "
            f"dune_queries.{name.replace('/', '.')} in config/study.yaml."
        )
    return run_query(int(query_id), params)


def read_sql(name: str) -> str:
    """Read the raw SQL text for a query — useful for review or local testing."""
    return (PROJECT_ROOT / "queries" / f"{name}.sql").read_text()


def render_named_sql(name: str, params: dict[str, str]) -> str:
    """Read queries/<name>.sql and substitute its {{param}} placeholders.

    Used by the Execute SQL path (dexresearch.dune_runner), which submits raw SQL
    text rather than a saved-query id, so there are no saved queries and no ids to
    manage. Raises if any {{placeholder}} is left unfilled.
    """
    sql = read_sql(name)
    for key, value in params.items():
        sql = sql.replace("{{" + key + "}}", str(value))
    if "{{" in sql:
        raise ValueError(f"Unfilled parameter(s) in queries/{name}.sql after render")
    return sql
