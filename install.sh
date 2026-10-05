#!/usr/bin/env bash
# Cai paneltest vao /usr/local/bin (hoac PREFIX khac).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/src/paneltest/paneltest.py"
PREFIX="${PREFIX:-/usr/local}"
if [ ! -r "$SRC" ]; then echo "Khong tim thay $SRC" >&2; exit 1; fi
install -D -m 0755 "$SRC" "$PREFIX/bin/paneltest"
echo "Da cai: $PREFIX/bin/paneltest"
"$PREFIX/bin/paneltest" selftest >/dev/null && echo "selftest: PASS"
