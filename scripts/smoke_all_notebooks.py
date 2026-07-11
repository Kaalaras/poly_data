"""Smoke-run every notebook under examples/ via nbclient against the real data/ snapshot.

Reports per-notebook: pass/fail, wall time, last-cell error if any.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = [
    "01-trader-analysis.ipynb",
    "02-backtest.ipynb",
    "03-orderfilled-analysis.ipynb",
    "04-benchmark-polars-vs-duckdb.ipynb",
    "05-ml-dataset-and-baseline.ipynb",
    "06-copy-betting.ipynb",
]


def main() -> int:
    data_root = Path(os.environ.get("POLY_DATA_ROOT", str(ROOT / "data")))
    if not (data_root / "orderFilled").is_dir():
        raise SystemExit(f"orderFilled missing at {data_root}")
    os.environ["POLY_DATA_ROOT"] = data_root.as_posix()

    results = []
    for name in NOTEBOOKS:
        path = ROOT / "examples" / name
        if not path.is_file():
            results.append((name, "missing", 0.0, ""))
            continue
        t0 = time.perf_counter()
        try:
            nb = nbformat.read(path, as_version=4)
            NotebookClient(
                nb,
                timeout=900,
                resources={"metadata": {"path": path.parent}},
            ).execute()
            results.append((name, "ok", time.perf_counter() - t0, ""))
        except CellExecutionError as e:
            err = str(e).splitlines()
            tail = "\n  ".join(err[-12:])
            results.append((name, "FAIL", time.perf_counter() - t0, tail))
        except Exception:
            tb = traceback.format_exc().splitlines()
            tail = "\n  ".join(tb[-6:])
            results.append((name, "FAIL", time.perf_counter() - t0, tail))

    print("\n=== SMOKE SUMMARY ===")
    n_ok = sum(1 for _, s, *_ in results if s == "ok")
    for name, status, secs, err in results:
        marker = "OK " if status == "ok" else "FAIL" if status == "FAIL" else "??? "
        print(f"  [{marker}] {name:<48} {secs:>6.1f}s")
        if err:
            print(f"         {err}")
    print(f"\n{n_ok}/{len(results)} notebooks passed")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
