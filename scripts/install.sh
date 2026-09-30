#!/usr/bin/env bash
# Install web-mcp globally (isolated tool environment, `web-mcp` on your PATH) with all dependencies.
#
#   ./scripts/install.sh                    # install from this checkout
#   ./scripts/install.sh --extras keyring   # also install optional extras (keyring,tokens)
#   ./scripts/install.sh --client claude-desktop --client cursor   # register with MCP clients
#   ./scripts/install.sh --searxng          # also start a local SearXNG container (needs Docker)
#   ./scripts/install.sh --uninstall
#
# Nothing here needs sudo. Everything lives in uv's tool dir and ~/.config|.cache|.local/{web-mcp}.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTRAS=""
CLIENTS=()
SEARXNG=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --extras)  EXTRAS="$2"; shift 2 ;;
    --client)  CLIENTS+=("$2"); shift 2 ;;
    --searxng) SEARXNG=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then return; fi
  say "uv not found — installing it (Python tool manager)"
  if command -v brew >/dev/null 2>&1; then
    brew install uv
  elif command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  elif command -v pipx >/dev/null 2>&1; then
    pipx install uv
  elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user uv
    export PATH="$(python3 -m site --user-base)/bin:$PATH"
  else
    echo "Could not install uv automatically. See https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi
  command -v uv >/dev/null 2>&1 || { echo "uv installed but not on PATH; open a new shell and re-run." >&2; exit 1; }
}

ensure_uv

if [[ $UNINSTALL -eq 1 ]]; then
  for c in claude-desktop cursor windsurf; do web-mcp install-client "$c" --remove 2>/dev/null || true; done
  uv tool uninstall web-mcp || true
  say "Removed. Data left in ~/.config/web-mcp, ~/.cache/web-mcp, ~/.local/state/web-mcp (delete manually if you want)."
  exit 0
fi

SPEC="$SRC"
[[ -n "$EXTRAS" ]] && SPEC="$SRC[$EXTRAS]"

say "Installing web-mcp (+ dependencies) as a global tool (Python ≥3.10; uv fetches one if missing)"
uv tool install --force --reinstall "$SPEC"

# make sure uv's tool bin dir is on PATH for future shells
uv tool update-shell >/dev/null 2>&1 || true
TOOL_BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
export PATH="$TOOL_BIN:$PATH"

command -v web-mcp >/dev/null 2>&1 || { warn "web-mcp not on PATH; add $TOOL_BIN to PATH"; exit 1; }

say "Writing default config (kept if it already exists)"
web-mcp config init >/dev/null 2>&1 || true

if [[ $SEARXNG -eq 1 ]]; then
  say "Starting local SearXNG (localhost only)"
  web-mcp searxng up || warn "SearXNG failed to start — DuckDuckGo fallback still works"
fi

for c in "${CLIENTS[@]:-}"; do
  [[ -z "$c" ]] && continue
  say "Registering with $c"
  web-mcp install-client "$c" || warn "could not register with $c"
done

say "Health check"
web-mcp doctor || true

cat <<EOF

Done. web-mcp is installed at: $(command -v web-mcp)

Next steps
  • Register with your agent:   web-mcp install-client print        (shows JSON to paste)
                                web-mcp install-client claude-desktop | claude-code | cursor | windsurf
  • Better search quality:      web-mcp searxng up                  (Docker)   or   export BRAVE_API_KEY=...
  • Tune limits / guardrails:   web-mcp config path   (edit ~/.config/web-mcp/config.toml)
  • Real search smoke test:     web-mcp doctor --live
EOF