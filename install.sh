#!/usr/bin/env bash
# Install vm-continuity: register the skill globally, ensure OpenCode, add a shim.
#
# Usage: install.sh [--no-opencode]
#   --no-opencode  skip the OpenCode check/install (for callers that install
#                  OpenCode themselves, e.g. a project bootstrap job running in
#                  parallel — avoids racing that install).
set -euo pipefail

SKIP_OPENCODE=0
for arg in "$@"; do
  case "$arg" in
    --no-opencode) SKIP_OPENCODE=1 ;;
    -h|--help) echo "usage: install.sh [--no-opencode]"; exit 0 ;;
    *) echo "[vm-continuity] unknown argument: $arg" >&2; exit 1 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[vm-continuity] repo: $REPO"

# 1. OpenCode present? (installed by this repo if absent, so it works standalone.)
if [ "$SKIP_OPENCODE" -eq 0 ]; then
  if ! command -v opencode >/dev/null 2>&1 && [ ! -x "$HOME/.opencode/bin/opencode" ]; then
    echo "[vm-continuity] opencode not found — installing"
    curl -fsSL https://opencode.ai/install | bash
    export PATH="$HOME/.opencode/bin:$PATH"
  fi
fi

# 2. Register the skill in OpenCode's GLOBAL skill source, so it is advertised
#    in every project, not just this repo.
mkdir -p "$HOME/.config/opencode/skills"
ln -sfn "$REPO/skills/vm-continuity" "$HOME/.config/opencode/skills/vm-continuity"
echo "[vm-continuity] skill  -> ~/.config/opencode/skills/vm-continuity"

# 3. PATH shim so the CLI is `vm-continuity ...`. Prefer a directory already on
#    PATH (/usr/local/bin on Colab, writable as root), else ~/.local/bin + a hint.
if mkdir -p /usr/local/bin 2>/dev/null && [ -w /usr/local/bin ]; then
  SHIM_DIR=/usr/local/bin
else
  SHIM_DIR="$HOME/.local/bin"
  mkdir -p "$SHIM_DIR"
fi
chmod +x "$REPO/continuity.py"
ln -sfn "$REPO/continuity.py" "$SHIM_DIR/vm-continuity"
echo "[vm-continuity] shim   -> $SHIM_DIR/vm-continuity"
case ":$PATH:" in
  *":$SHIM_DIR:"*) ;;
  *) echo "[vm-continuity] NOTE: $SHIM_DIR is not on PATH; add it, or run: python $REPO/continuity.py" ;;
esac

# 4. Report prerequisites.
echo "[vm-continuity] gsutil: $(command -v gsutil || echo 'MISSING — needed to ship/pull')"
echo "[vm-continuity] CONTINUITY_GCS=${CONTINUITY_GCS:-<unset — using default>}"
echo "[vm-continuity] done. Try: vm-continuity list"
