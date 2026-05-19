"""Ensure NLTK punkt_tab is available before Pipecat uses sentence tokenization."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_NLTK_DIR = _PROJECT_ROOT / "nltk_data"
_PUNKT_TAB_DIR = _NLTK_DIR / "tokenizers" / "punkt_tab"
_PUNKT_TAB_ZIP_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip"
)


def _download_punkt_tab() -> None:
    _NLTK_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _NLTK_DIR / "punkt_tab.zip"
    if shutil.which("curl"):
        subprocess.run(
            ["curl", "-fsSL", "-o", str(zip_path), _PUNKT_TAB_ZIP_URL],
            check=True,
        )
    else:
        import urllib.request

        urllib.request.urlretrieve(_PUNKT_TAB_ZIP_URL, zip_path)

    (_NLTK_DIR / "tokenizers").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(_NLTK_DIR / "tokenizers")
    zip_path.unlink(missing_ok=True)


def ensure_nltk_data() -> None:
    """Point NLTK at project data and download punkt_tab if missing."""
    os.environ.setdefault("NLTK_DATA", str(_NLTK_DIR))

    if _PUNKT_TAB_DIR.is_dir():
        return

    try:
        import nltk

        nltk.data.find("tokenizers/punkt_tab")
        return
    except LookupError:
        pass

    logger.info("Downloading NLTK punkt_tab tokenizer data to %s", _NLTK_DIR)
    try:
        _download_punkt_tab()
    except Exception as exc:
        logger.error(
            "Could not download punkt_tab (%s). Run: ./scripts/setup_nltk.sh",
            exc,
        )
        raise
