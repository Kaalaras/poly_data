"""Generate the V2 lake quickstart notebook."""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.cells = [
        nbf.v4.new_markdown_cell(
            "# 00 — Démarrer avec le lac de données V2\n\n"
            "Ce notebook utilise par défaut la petite fixture déterministe ``data_smoke``. "
            "Pour analyser le lac complet, définissez explicitement ``POLY_DATA_ROOT`` et "
            "``POLY_NOTEBOOK_MODE=full``."
        ),
        nbf.v4.new_code_cell(
            "from poly_data.io.parquet_store import ParquetStore\n"
            "from poly_data.notebooks import resolve_notebook_context, source_inventory\n\n"
            "ctx = resolve_notebook_context()\n"
            "store = ParquetStore(ctx.root)\n"
            "SOURCES = ['order_filled_v2', 'trades', 'markets_current', 'market_assets', 'market_outcomes']\n"
            "print({'root': str(ctx.root), 'mode': ctx.mode, 'revision': ctx.revision})\n"
            "inventory = source_inventory(store, SOURCES)\n"
            "inventory"
        ),
        nbf.v4.new_markdown_cell(
            "## Le flux de données\n\n"
            "``order_filled_v2`` est le bronze : les événements de remplissage V2 avec leur "
            "provenance de bloc. ``trades`` est le silver normalisé. Les dimensions "
            "``markets_current`` et ``market_assets`` relient un actif à son marché. "
            "Enfin, ``market_outcomes`` conserve les résolutions officielles sans réécrire "
            "l'historique."
        ),
        nbf.v4.new_code_cell(
            "from pathlib import Path\n\n"
            "for source in SOURCES:\n"
            "    files = sorted((ctx.root / source).rglob('*.parquet'))\n"
            "    print(f'{source:18} partitions={len({p.parent for p in files})} files={len(files)}')\n"
            "print('manifest files:', len(list((ctx.root / '_metadata').rglob('*.json'))))"
        ),
        nbf.v4.new_markdown_cell(
            "## Glossaire\n\n"
            "- **asset** : jeton d'une issue, relié à un marché par ``market_assets``.\n"
            "- **token1 / token2** : les deux issues binaires conservées par le contrat.\n"
            "- **market_outcomes** : étiquette officielle finale ; une absence signifie qu'un "
            "marché reste non résolu, pas une issue négative.\n"
            "- **partition** : dossier ``year=YYYY/month=MM`` qui permet de lire seulement la "
            "période utile."
        ),
    ]
    for index, cell in enumerate(notebook.cells):
        cell["id"] = f"00-{index:02d}"
    output = Path(__file__).resolve().parents[1] / "examples" / "00-v2-lake-quickstart.ipynb"
    output.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
