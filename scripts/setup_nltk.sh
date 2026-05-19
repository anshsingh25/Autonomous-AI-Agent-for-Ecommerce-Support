#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p nltk_data/tokenizers
curl -fsSL -o /tmp/punkt_tab.zip \
  "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip"
unzip -qo /tmp/punkt_tab.zip -d nltk_data/tokenizers
rm -f /tmp/punkt_tab.zip
echo "NLTK punkt_tab installed under $(pwd)/nltk_data"
