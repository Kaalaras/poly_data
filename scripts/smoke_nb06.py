"""Smoke-run examples/06-copy-betting.ipynb via nbclient against the
``data/`` real snapshot (or POLY_DATA_ROOT override)."""
from __future__ import annotations

import os
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "examples" / "06-copy-betting.ipynb"


def main() -> None:
    data_root = Path(os.environ.get("POLY_DATA_ROOT", str(ROOT / "data")))
    if not (data_root / "trades").is_dir():
        raise SystemExit(
            f"trades not found at {data_root}; "
            "run the update + process pipeline first"
        )
    os.environ["POLY_DATA_ROOT"] = data_root.as_posix()

    nb = nbformat.read(NB, as_version=4)
    NotebookClient(
        nb, timeout=900, resources={"metadata": {"path": NB.parent}}
    ).execute()
    print("nb06 smoke OK")


if __name__ == "__main__":
    main()
