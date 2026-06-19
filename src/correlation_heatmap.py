"""Generate a Jupyter notebook with correlation heatmaps for the property data.

Builds a notebook at data/correlation_heatmaps.ipynb that loads
data/forsale.csv and data/torent.csv, keeps the numeric columns that are
sufficiently populated (few missing values), and renders a Pearson correlation
heatmap for each dataset. The notebook is executed so the heatmaps are embedded
in the output file, and can also be re-run interactively in Jupyter.
"""

from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook
from nbconvert.preprocessors import ExecutePreprocessor

# This script lives in src/, so the project root is one level up.
PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_DIR / "data" / "correlation_heatmaps.ipynb"

# Keep a numeric column only if at least this fraction of rows have a value.
MIN_NON_NULL_FRACTION = 0.30


SETUP_CODE = f'''\
from pathlib import Path

import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

%matplotlib inline

DATA_DIR = Path.cwd()
DATASETS = {{
    "For sale": DATA_DIR / "forsale.csv",
    "To rent": DATA_DIR / "torent.csv",
}}

# Keep a numeric column only if at least this fraction of rows have a value.
MIN_NON_NULL_FRACTION = {MIN_NON_NULL_FRACTION}

# Numeric but not meaningful to correlate (identifiers / coordinates).
EXCLUDE = {{"latitude", "longitude", "postal_code"}}


def numeric_correlation(csv_path):
    """Load a dataset and return the correlation matrix of its well-populated
    numeric columns."""
    df = pd.read_csv(csv_path, low_memory=False)

    # Coerce everything we can to numeric; non-numeric columns become all-NaN.
    numeric = df.apply(pd.to_numeric, errors="coerce")

    # Drop columns that are too sparse or explicitly excluded.
    threshold = int(len(numeric) * MIN_NON_NULL_FRACTION)
    kept = numeric.dropna(axis=1, thresh=threshold)
    kept = kept.drop(columns=[c for c in EXCLUDE if c in kept.columns])

    # Drop constant columns (correlation is undefined for those).
    kept = kept.loc[:, kept.nunique(dropna=True) > 1]

    return kept.corr(numeric_only=True)
'''


def plot_code(name: str) -> str:
    return f'''\
corr = numeric_correlation(DATASETS["{name}"])

fig, ax = plt.subplots(figsize=(14, 12))
sns.heatmap(
    corr,
    cmap="RdBu_r",
    vmin=-1,
    vmax=1,
    center=0,
    square=True,
    linewidths=0.5,
    cbar_kws={{"shrink": 0.8, "label": "Pearson r"}},
    ax=ax,
)
ax.set_title(
    "{name} — correlation of %d populated numeric features" % len(corr.columns)
)
plt.tight_layout()
plt.show()
'''


def build_notebook() -> nbformat.NotebookNode:
    nb = new_notebook()
    nb.cells = [
        new_markdown_cell(
            "# Property feature correlation heatmaps\n\n"
            "Pearson correlation of the numeric columns that are sufficiently "
            "populated (at least "
            f"{int(MIN_NON_NULL_FRACTION * 100)}% non-missing) in each dataset. "
            "Identifier and coordinate columns are excluded."
        ),
        new_code_cell(SETUP_CODE),
        new_markdown_cell("## For sale"),
        new_code_cell(plot_code("For sale")),
        new_markdown_cell("## To rent"),
        new_code_cell(plot_code("To rent")),
    ]
    return nb


def main() -> None:
    nb = build_notebook()

    # Execute with the data directory as the working directory so the relative
    # CSV paths resolve and plot outputs get embedded.
    ep = ExecutePreprocessor(timeout=600, kernel_name="python3")
    ep.preprocess(nb, {"metadata": {"path": str(OUTPUT.parent)}})

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, OUTPUT)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
