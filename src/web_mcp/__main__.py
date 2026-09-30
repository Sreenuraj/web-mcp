"""web-mcp command line: serve | approve | audit | config | doctor | key | searxng | install-client | cache"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_TEMPLATE, ConfigError, config_dir, config_paths, expand, load_config


def _cfg_or_exit():
    try:
        return load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------------------------------
def cmd_serve(_a) -> int:
    from .server import serve

    serve()
    return 0


def cmd_approve(a) -> int:
    """Optional: inspect / answer pending approvals from a terminal (normally the agent relays your answer)."""
    from .storage import Storage

    cfg = _cfg_or_exit()
    st = Storage(expand(cfg.state.path), None)
    if not a.id:
        pending = st.pending_approvals()
        if not pending:
            print("No pending approvals.")
        for p in pending:
            print(f"{p['id']}  session={p['session_id']}  [{p['kind']}]\n    {p['summary']}\n    options: {', '.join(json.loads(p['options']))}")
        return 0
    rec = st.get_approval(a.id)
    if not rec or rec["status"] != "pending":
        print(f"No pending approval with id {a.id}.", file=sys.stderr)
        return 1
    options = json.loads(rec["options"])
    choice = "deny" if a.deny else a.choice
    if choice not in options:
        print(f"Choice '{choice}' not available for this request. Options: {', '.join(options)}", file=sys.stderr)
        return 1
    st.decide_approval(a.id, choice, "answered via terminal by the user")
    print(f"Recorded '{choice}' for {a.id}. Ask the agent to repeat its last call.")
    return 0


def cmd_audit(a) -> int:
    from .audit import read_audit, summarize

    cfg = _cfg_or_exit()
    recs = read_audit(expand(cfg.audit.path), 24 if a.today else (a.hours or None))
    if a.json:
        print(json.dumps(recs, indent=1))
    else:
        print(summarize(recs))
    return 0


def cmd_config(a) -> int:
    if a.action == "init":
        target = config_dir() / "config.toml"
        if target.exists() and not a.force:
            print(f"{target} already exists (use --force to overwrite).")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"Wrote {target}")
    elif a.action == "path":
        for p in config_paths():
            print(("✓ " if p.is_file() else "  ") + str(p))
    else:
        print(_cfg_or_exit().model_dump_json(indent=2))
    return 0


def cmd_key(a) -> int:
    from .providers.base import KEY_ENV, KEYRING_SERVICE

    if a.provider not in KEY_ENV:
        print(f"provider must be one of: {', '.join(KEY_ENV)}", file=sys.stderr)
        return 1
    try:
        import keyring  # type: ignore
    except ImportError:
        print(f"The keychain needs the optional extra: uv tool install 'web-mcp[keyring]'  (or set ${KEY_ENV[a.provider]})", file=sys.stderr)
        return 1
    if a.action == "set":
        import getpass

        val = getpass.getpass(f"{a.provider} API key (input hidden): ").strip()
        if not val:
            print("empty; nothing stored")
            return 1
        keyring.set_password(KEYRING_SERVICE, a.provider, val)
        print(f"Stored {a.provider} key in the OS keychain. Add '{a.provider}' to [providers].chain in your config.")
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, a.provider)
            print("Removed.")
        except Exception:
            print("No stored key.")
    return 0


def cmd_cache(a) -> int:
    from .storage import Storage

    cfg = _cfg_or_exit()
    st = Storage(expand(cfg.state.path), expand(cfg.cache.path))
    n = st.cache_purge(everything=True)
    print(f"Cleared {n} cached entries.")
    return 0


# ---- doctor ----------------------------------------------------------------------------------------
def cmd_doctor(a) -> int:
    ok = True

    def line(status: str, msg: str):
        print(f"  {status} {msg}")

    print(f"web-mcp {__version__} · python {sys.version.split()[0]} · {sys.platform}")
    cfg = _cfg_or_exit()
    line("✓", "config valid (" + (", ".join(str(p) for p in config_paths() if p.is_file()) or "defaults") + ")")
    for mod in ("mcp", "httpx", "pydantic", "trafilatura", "selectolax", "pypdf", "ddgs"):
        try:
            __import__(mod)
            line("✓", f"import {mod}")
        except ImportError:
            line("✗", f"missing dependency: {mod}")
            ok = False
    for label, path in (("state db", cfg.state.path), ("cache db", cfg.cache.path), ("audit log", cfg.audit.path)):
        p = expand(path).parent
        try:
            p.mkdir(parents=True, exist_ok=True)
            (p / ".w").write_text("x")
            (p / ".w").unlink()
            line("✓", f"{label} dir writable: {p}")
        except OSError as e:
            line("✗", f"{label} dir not writable: {p} ({e})")
            ok = False

    async def providers():
        import httpx

        from .providers.base import get_api_key

        good = 0
        for name in cfg.providers.chain:
            if name == "searxng":
                try:
                    async with httpx.AsyncClient(timeout=4, trust_env=False) as c:
                        r = await c.get(cfg.providers.searxng_url.rstrip("/") + "/search", params={"q": "test", "format": "json"})
                    if r.status_code == 200:
                        line("✓", f"searxng reachable at {cfg.providers.searxng_url}")
                        good += 1
                    elif r.status_code == 403:
                        line("!", "searxng reachable but JSON format disabled (run `web-mcp searxng up` for a ready config)")
                    else:
                        line("!", f"searxng returned HTTP {r.status_code}")
                except httpx.HTTPError:
                    line("!", f"searxng not reachable at {cfg.providers.searxng_url} (fine if you rely on a fallback; `web-mcp searxng up` starts one)")
            elif name == "duckduckgo":
                try:
                    import ddgs  # noqa: F401

                    line("✓", "duckduckgo available (key-less fallback)")
                    good += 1
                except ImportError:
                    line("!", "duckduckgo: `ddgs` not installed")
            else:
                if get_api_key(name):
                    line("✓", f"{name}: API key found")
                    good += 1
                else:
                    line("!", f"{name}: no API key (env or `web-mcp key set {name}`)")
        return good

    if asyncio.run(providers()) == 0:
        line("✗", "no usable search provider")
        ok = False
    line("✓", f"approval: {' → '.join(cfg.approval.modes)} (relay = the agent asks you in the conversation; no dialogs)")
    exe = shutil.which("web-mcp")
    line("✓" if exe else "!", f"web-mcp on PATH: {exe or 'not found — use `install-client` (absolute path is written) or `uv tool install .`'}")
    if a.live:
        async def live():
            from .core import Engine

            e = Engine(cfg)
            try:
                out = await e.start(None, "python release", "check search works", ["latest python version"], "quick", "any")
                sid = out.split("session: ")[1].split(" ")[0]
                res = await e.search(None, sid, "python latest release", None, 3, None)
                print("\n" + res)
                return "No usable results" not in res and not res.startswith("ERROR")
            finally:
                await e.aclose()

        if not asyncio.run(live()):
            ok = False
    print("\n" + ("All good." if ok else "Problems found — see ✗ above."))
    return 0 if ok else 1


# ---- searxng helper --------------------------------------------------------------------------------
SEARXNG_NAME = "web-mcp-searxng"


def cmd_searxng(a) -> int:
    docker = shutil.which("docker")
    if not docker:
        print("Docker not found. Install Docker Desktop, or point [providers].searxng_url at any SearXNG instance,\n"
              "or just use the key-less duckduckgo fallback (works out of the box).", file=sys.stderr)
        return 1
    if a.action == "down":
        subprocess.run([docker, "rm", "-f", SEARXNG_NAME])
        return 0
    if a.action == "status":
        return subprocess.run([docker, "ps", "--filter", f"name={SEARXNG_NAME}"]).returncode
    d = config_dir() / "searxng"
    d.mkdir(parents=True, exist_ok=True)
    settings = d / "settings.yml"
    if not settings.exists():
        settings.write_text(
            "use_default_settings: true\n"
            f"server:\n  secret_key: \"{secrets.token_hex(24)}\"\n  limiter: false\n  image_proxy: false\n"
            "search:\n  formats:\n    - html\n    - json\n", encoding="utf-8")
    subprocess.run([docker, "rm", "-f", SEARXNG_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run([docker, "run", "-d", "--name", SEARXNG_NAME, "--restart", "unless-stopped",
                        "-p", "127.0.0.1:8888:8080", "-v", f"{d}:/etc/searxng", "searxng/searxng:latest"])
    if r.returncode == 0:
        print("SearXNG starting on http://127.0.0.1:8888 (bound to localhost only). Check with: web-mcp doctor")
    return r.returncode


# ---- install-client --------------------------------------------------------------------------------
def _server_command() -> tuple[str, list[str]]:
    exe = shutil.which("web-mcp")
    if exe:
        return exe, ["serve"]
    return sys.executable, ["-m", "web_mcp", "serve"]


def _client_files() -> dict[str, Path]:
    home = Path.home()
    if sys.platform == "darwin":
        claude = home / "Library/Application Support/Claude/claude_desktop_config.json"
    elif sys.platform.startswith("win"):
        claude = Path(os.environ.get("APPDATA", home)) / "Claude/claude_desktop_config.json"
    else:
        claude = home / ".config/Claude/claude_desktop_config.json"
    return {"claude-desktop": claude, "cursor": home / ".cursor/mcp.json", "windsurf": home / ".codeium/windsurf/mcp_config.json"}


def cmd_install_client(a) -> int:
    cmd, args = _server_command()
    entry = {"command": cmd, "args": args}
    targets = _client_files()
    if a.client in ("print", None):
        print(json.dumps({"mcpServers": {"web-mcp": entry}}, indent=2))
        print("\nPaste into your client's MCP settings (Cline/PostQode/Roo: 'MCP Servers' → edit config; VS Code: .vscode/mcp.json uses key \"servers\").")
        return 0
    if a.client == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            print("`claude` CLI not found. Manual: claude mcp add web-mcp --scope user -- " + " ".join([cmd, *args]))
            return 1
        return subprocess.run([claude, "mcp", "add", "web-mcp", "--scope", "user", "--", cmd, *args]).returncode
    if a.client not in targets:
        print("client must be one of: print, claude-code, " + ", ".join(targets), file=sys.stderr)
        return 1
    path = targets[a.client]
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"{path} is not valid JSON; refusing to modify it.", file=sys.stderr)
            return 1
        shutil.copy2(path, str(path) + ".bak")
    servers = data.setdefault("mcpServers", {})
    if a.remove:
        servers.pop("web-mcp", None)
    else:
        servers["web-mcp"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{'Removed from' if a.remove else 'Installed into'} {path} (backup: {path.name}.bak). Restart {a.client}.")
    return 0


# ---------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="web-mcp", description="Guard-railed web research MCP server")
    p.add_argument("--version", action="version", version=f"web-mcp {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)").set_defaults(fn=cmd_serve)
    ap = sub.add_parser("approve", help="(optional) list / answer pending approvals from a terminal")
    ap.add_argument("id", nargs="?")
    ap.add_argument("--choice", default="once", choices=["once", "extend", "session"])
    ap.add_argument("--deny", action="store_true")
    ap.set_defaults(fn=cmd_approve)
    au = sub.add_parser("audit", help="show the audit log summary")
    au.add_argument("--today", action="store_true")
    au.add_argument("--hours", type=float)
    au.add_argument("--json", action="store_true")
    au.set_defaults(fn=cmd_audit)
    cf = sub.add_parser("config", help="show | init | path")
    cf.add_argument("action", nargs="?", default="show", choices=["show", "init", "path"])
    cf.add_argument("--force", action="store_true")
    cf.set_defaults(fn=cmd_config)
    dr = sub.add_parser("doctor", help="diagnose the installation")
    dr.add_argument("--live", action="store_true", help="also run one real search")
    dr.set_defaults(fn=cmd_doctor)
    ky = sub.add_parser("key", help="store/remove a provider API key in the OS keychain")
    ky.add_argument("action", choices=["set", "delete"])
    ky.add_argument("provider")
    ky.set_defaults(fn=cmd_key)
    sx = sub.add_parser("searxng", help="manage a local SearXNG container")
    sx.add_argument("action", choices=["up", "down", "status"])
    sx.set_defaults(fn=cmd_searxng)
    ic = sub.add_parser("install-client", help="register web-mcp with an MCP client")
    ic.add_argument("client", nargs="?", default="print",
                    help="print | claude-desktop | claude-code | cursor | windsurf")
    ic.add_argument("--remove", action="store_true")
    ic.set_defaults(fn=cmd_install_client)
    cc = sub.add_parser("cache-clear", help="clear the page/search cache")
    cc.set_defaults(fn=cmd_cache)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    fn = getattr(args, "fn", cmd_serve)
    try:
        sys.exit(fn(args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
