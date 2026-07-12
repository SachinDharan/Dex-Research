"""One-call loader for every table in this folder.

    from load import load
    df = load("uniswap_outstanding_allowances")   # reassembles split parts
    df = load("aave_supplies")                    # reads .csv.gz transparently

Requires pandas. Table names are the file names without extension/part suffix;
run `python load.py` to list them all.
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def _candidates(name: str) -> list[Path]:
    for sub in ("analysis", "raw_events"):
        d = ROOT / sub
        parts = sorted(d.glob(f"{name}.part*.csv")) or sorted(d.glob(f"{name}.part*.csv.gz"))
        if parts:
            return parts
        for ext in (".csv", ".csv.gz"):
            if (d / f"{name}{ext}").exists():
                return [d / f"{name}{ext}"]
    raise FileNotFoundError(f"no table named {name!r}; run `python load.py` to list tables")


def load(name: str) -> pd.DataFrame:
    files = _candidates(name)
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True)


def tables() -> list[str]:
    names = set()
    for f in ROOT.glob("*/*.csv*"):
        names.add(f.name.split(".part")[0].split(".csv")[0])
    return sorted(names)


if __name__ == "__main__":
    for t in tables():
        print(t)
