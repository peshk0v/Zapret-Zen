"""Command-line interface for the smart orchestrator.

Everything the UI can do is also a CLI verb, which makes the feature
debuggable when the GUI is broken or when the user wants to repair their setup
from a console::

    python -m zapret_zen.orchestrator.cli status
    python -m zapret_zen.orchestrator.cli watch
    python -m zapret_zen.orchestrator.cli connections --limit 40
    python -m zapret_zen.orchestrator.cli probe youtube.com
    python -m zapret_zen.orchestrator.cli rules
    python -m zapret_zen.orchestrator.cli forget youtube.com
    python -m zapret_zen.orchestrator.cli fix --all

The CLI never imports PySide6.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from zapret_zen.orchestrator.client import OrchestratorClient
from zapret_zen.orchestrator.ipc.named_pipe import PipeError
from zapret_zen.orchestrator.seed import seed_hosts
from zapret_zen.runtime_env import is_packaged_runtime, packaged_install_root


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def resolve_roots(install_root: str = "", data_dir: str = "") -> tuple[Path, Path]:
    if install_root:
        root = Path(install_root).expanduser().resolve()
    elif is_packaged_runtime():
        root = packaged_install_root()
    else:
        # src/zapret_zen/orchestrator/cli.py -> repository root
        root = Path(__file__).resolve().parents[3]
    if data_dir:
        data = Path(data_dir).expanduser().resolve()
    elif not is_packaged_runtime() or (root / "portable.flag").exists():
        data = root / "data"
    else:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        data = Path(appdata) / "Zapret-Zen" / "data"
    return root, data


def _client(args: argparse.Namespace, *, autostart: bool = True) -> OrchestratorClient:
    install_root, data_dir = resolve_roots(args.install_root, args.data_dir)
    seeds: list[str] = list(seed_hosts(limit=12))
    for host in getattr(args, "seed_host", []) or []:
        text = str(host).strip().lower()
        if text and text not in seeds:
            seeds.append(text)
    return OrchestratorClient(
        install_root=install_root,
        data_dir=data_dir,
        pipe_name=getattr(args, "pipe", "") or None,
        seed_hosts=seeds,
        autostart=autostart,
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fmt_ts(value: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return "-"


def _short(value: Any, width: int) -> str:
    text = str(value if value is not None else "")
    if len(text) <= width:
        return text.ljust(width)
    return (text[: max(0, width - 1)] + "…").ljust(width)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int]) -> None:
    print("  ".join(_short(header, widths[index]).upper() for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(_short(cell, widths[index]) for index, cell in enumerate(row)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("status")
    if args.json:
        _print_json(payload)
    elif not payload.get("ok"):
        print(f"orchestrator: unavailable ({payload.get('error') or 'no response'})")
    else:
        observer = payload.get("observer") or {}
        counters = payload.get("counters") or {}
        print(f"orchestrator  mode={payload.get('mode')}  network={payload.get('network') or '-'}  pid={payload.get('pid')}")
        print(f"observer      {observer.get('name')} available={observer.get('available')} live={observer.get('live')}")
        print(f"trial         available={(payload.get('trial') or {}).get('available')}")
        print(f"counters      hosts={counters.get('hosts')} rules={counters.get('rules')} probes={counters.get('probes')} healed={counters.get('healed')}")
        print(f"uptime        {payload.get('uptime_s')}s  requests={payload.get('requests')}")
        error = payload.get("error") or ""
        if error:
            print(f"last error    {error}")
    return 0 if payload.get("ok") else 1


def cmd_watch(args: argparse.Namespace) -> int:
    client = _client(args)
    interval = max(0.5, float(args.interval))
    try:
        while True:
            payload = client.try_request("status", timeout_s=5.0)
            if not payload.get("ok"):
                print(f"[{time.strftime('%H:%M:%S')}] unavailable: {payload.get('error') or 'no response'}")
            else:
                observer = payload.get("observer") or {}
                counters = payload.get("counters") or {}
                applier = payload.get("applier") or {}
                pending = "*" if applier.get("pending_restart") else ""
                print(
                    f"[{time.strftime('%H:%M:%S')}] mode={payload.get('mode')} live={observer.get('live')} "
                    f"hosts={counters.get('hosts')} rules={counters.get('rules')} "
                    f"probes={counters.get('probes')} healed={counters.get('healed')} restart={pending or '-'}"
                )
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_connections(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("connections", {"limit": args.limit, "problematic_only": args.problematic}, timeout_s=10.0)
    if args.json:
        _print_json(payload)
        return 0 if payload.get("ok") else 1
    items = payload.get("connections") or []
    if not payload.get("ok"):
        print(f"orchestrator: unavailable ({payload.get('error') or 'no response'})")
        return 1
    if not items:
        print("no live connections (the observer only reports new 5-tuples)")
        return 0
    rows = [
        (
            _fmt_ts(item.get("ts")),
            item.get("process") or "-",
            item.get("protocol") or "-",
            f"{item.get('remote_address')}:{item.get('remote_port')}",
            item.get("hostname") or item.get("remote_address") or "-",
            item.get("verdict") or "-",
        )
        for item in items
    ]
    _table(("time", "process", "proto", "remote", "host", "verdict"), rows, (8, 18, 5, 24, 32, 10))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("probe", {"host": args.host, "trials": not args.no_trials}, timeout_s=max(30.0, args.timeout))
    if args.json:
        _print_json(payload)
        return 0 if payload.get("ok") else 1
    if not payload.get("ok") and not payload.get("baseline") and not payload.get("trials"):
        print(f"probe failed: {payload.get('error') or 'no response'}")
        return 1
    baseline = payload.get("baseline") or {}
    print(f"baseline  {baseline.get('kind')}  {baseline.get('error') or ''}".rstrip())
    for trial in payload.get("trials") or []:
        print(f"  trial   {trial.get('recipe')}: {trial.get('kind')} {trial.get('error') or ''}".rstrip())
    rule = payload.get("rule") or {}
    if rule:
        print(f"rule      {rule.get('host')} -> {rule.get('recipe')} ({rule.get('mode')}, confidence {rule.get('confidence')})")
    decision = payload.get("decision") or {}
    if decision:
        print(f"decision  {decision.get('action')}  {decision.get('reason')}")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("rules", timeout_s=10.0)
    if args.json:
        _print_json(payload)
        return 0 if payload.get("ok") else 1
    rules = payload.get("rules") or []
    if not payload.get("ok"):
        print(f"orchestrator: unavailable ({payload.get('error') or 'no response'})")
        return 1
    if not rules:
        print("no per-domain rules yet")
        return 0
    rows = [
        (
            rule.get("host"),
            rule.get("mode"),
            rule.get("recipe") or "-",
            f"{float(rule.get('confidence') or 0):.2f}",
            rule.get("source") or "-",
            rule.get("network") or "-",
            "yes" if rule.get("pinned") else ("no" if rule.get("enabled") else "off"),
            _fmt_ts(rule.get("last_ok")),
        )
        for rule in rules
    ]
    _table(("host", "mode", "recipe", "conf", "source", "network", "active", "last ok"), rows, (28, 7, 16, 5, 8, 16, 6, 8))
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("forget", {"host": args.host}, timeout_s=15.0)
    if args.json:
        _print_json(payload)
    elif payload.get("ok"):
        print(f"forgot {args.host}: {payload.get('removed')} rule(s) removed")
    else:
        print(f"failed: {payload.get('error') or 'no response'}")
    return 0 if payload.get("ok") else 1


def cmd_pin(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request(
        "pin",
        {"host": args.host, "recipe": args.recipe, "mode": args.mode},
        timeout_s=15.0,
    )
    if args.json:
        _print_json(payload)
    elif payload.get("ok"):
        rule = payload.get("rule") or {}
        print(f"pinned {rule.get('host')} -> {rule.get('recipe')} ({rule.get('mode')})")
    else:
        print(f"failed: {payload.get('error') or 'no response'}")
    return 0 if payload.get("ok") else 1


def cmd_mode(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("set_mode", {"mode": args.mode}, timeout_s=10.0)
    if args.json:
        _print_json(payload)
    elif payload.get("ok"):
        print(f"mode = {payload.get('mode')}")
    else:
        print(f"failed: {payload.get('error') or 'no response'}")
    return 0 if payload.get("ok") else 1


def cmd_apply(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.try_request("apply", timeout_s=20.0)
    if args.json:
        _print_json(payload)
    elif payload.get("ok"):
        print(f"overlay written: {payload.get('count')} section(s), restart pending")
    else:
        print(f"failed: {payload.get('error') or 'no response'}")
    return 0 if payload.get("ok") else 1


def cmd_fix(args: argparse.Namespace) -> int:
    client = _client(args)
    snapshot = client.try_request("snapshot", timeout_s=10.0)
    if not snapshot.get("ok"):
        print(f"orchestrator: unavailable ({snapshot.get('error') or 'no response'})")
        return 1
    hosts: list[str] = []
    for host in args.host or []:
        text = str(host).strip().lower()
        if text and text not in hosts:
            hosts.append(text)
    if args.all:
        for item in snapshot.get("stats") or []:
            host = str(item.get("host") or "")
            verdict = str(item.get("verdict") or "")
            if host and verdict in {"blocked", "suspect"} and host not in hosts:
                hosts.append(host)
        for host in seed_hosts(limit=8):
            if host not in hosts:
                hosts.append(host)
    if not hosts:
        print("nothing to fix: pass --host DOMAIN or --all")
        return 1

    print(f"probing {len(hosts)} host(s)...")
    healed = 0
    for host in hosts:
        payload = client.try_request("probe", {"host": host, "trials": True}, timeout_s=max(45.0, args.timeout))
        baseline = payload.get("baseline") or {}
        if not payload.get("ok") and not baseline:
            print(f"  {host:<32} error: {payload.get('error') or 'no response'}")
            continue
        # ``ok`` only says that the call succeeded: a healed (or hopeless) probe
        # also returns ``ok``.  The verdict lives in the baseline kind.
        kind = str(baseline.get("kind") or payload.get("error") or "error")
        if kind == "ok":
            print(f"  {host:<32} direct OK")
            continue
        rule = payload.get("rule") or {}
        if rule:
            healed += 1
            print(f"  {host:<32} {kind} -> {rule.get('recipe')} ({rule.get('mode')})")
        else:
            decision = payload.get("decision") or {}
            action = decision.get("action") or "none"
            print(f"  {host:<32} {kind} -> no recipe ({action})")

    applied = client.try_request("apply", timeout_s=20.0) if healed else {"ok": True, "count": 0}
    print(f"rules written: {healed}; overlay: {applied.get('count', 0)} section(s)")
    if healed:
        print("restart zapret (or let the app do it) so the new rules take effect")
    if args.json:
        _print_json({"hosts": hosts, "healed": healed, "apply": applied})
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    client = _client(args, autostart=False)
    ok = client.shutdown()
    print("orchestrator stopped" if ok else "orchestrator was not running")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from zapret_zen.orchestrator.sidecar import main as sidecar_main

    install_root, data_dir = resolve_roots(args.install_root, args.data_dir)
    argv: list[str] = [
        "--install-root",
        str(install_root),
        "--data-dir",
        str(data_dir),
        "--parent-pid",
        str(args.parent_pid),
        "--idle-timeout",
        str(args.idle_timeout),
    ]
    if args.pipe:
        argv.extend(["--pipe", args.pipe])
    if args.verbose:
        argv.append("--verbose")
    for host in args.seed_host or []:
        argv.extend(["--seed-host", host])
    return sidecar_main(argv)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zapret-zen-orchestrator", description="Zapret-Zen smart orchestrator CLI")
    parser.add_argument("--install-root", default="", help="installation root (auto-detected by default)")
    parser.add_argument("--data-dir", default="", help="writable data directory (auto-detected by default)")
    parser.add_argument("--pipe", default="", help="override the named-pipe name")
    parser.add_argument("--seed-host", action="append", default=[], help="extra host to probe (repeatable)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="show orchestrator status")
    status.set_defaults(func=cmd_status)

    watch = subparsers.add_parser("watch", help="poll status until interrupted")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    connections = subparsers.add_parser("connections", help="show recent live connections")
    connections.add_argument("--limit", type=int, default=40)
    connections.add_argument("--problematic", action="store_true", help="only suspect/blocked entries")
    connections.set_defaults(func=cmd_connections)

    probe = subparsers.add_parser("probe", help="probe a host and try recipes")
    probe.add_argument("host")
    probe.add_argument("--no-trials", action="store_true", help="do not try desync recipes")
    probe.add_argument("--timeout", type=float, default=90.0)
    probe.set_defaults(func=cmd_probe)

    rules = subparsers.add_parser("rules", help="list per-domain rules")
    rules.set_defaults(func=cmd_rules)

    forget = subparsers.add_parser("forget", help="drop a learned rule")
    forget.add_argument("host")
    forget.set_defaults(func=cmd_forget)

    pin = subparsers.add_parser("pin", help="pin a recipe to a host")
    pin.add_argument("host")
    pin.add_argument("recipe")
    pin.add_argument("--mode", default="desync", choices=["desync", "direct", "proxy"])
    pin.set_defaults(func=cmd_pin)

    mode = subparsers.add_parser("mode", help="set orchestrator mode")
    mode.add_argument("mode", choices=["off", "observe", "suggest", "auto"])
    mode.set_defaults(func=cmd_mode)

    apply_cmd = subparsers.add_parser("apply", help="write the override overlay")
    apply_cmd.set_defaults(func=cmd_apply)

    fix = subparsers.add_parser("fix", help="probe and heal hosts, then write the overlay")
    fix.add_argument("--host", action="append", default=[], help="host to fix (repeatable)")
    fix.add_argument("--all", action="store_true", help="fix all blocked hosts plus service seeds")
    fix.add_argument("--timeout", type=float, default=120.0)
    fix.set_defaults(func=cmd_fix)

    stop = subparsers.add_parser("stop", help="stop the sidecar")
    stop.set_defaults(func=cmd_stop)

    serve = subparsers.add_parser("serve", help="run the sidecar in the foreground")
    serve.add_argument("--parent-pid", type=int, default=0)
    serve.add_argument("--idle-timeout", type=float, default=0.0)
    serve.add_argument("--verbose", action="store_true")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except PipeError as error:
        print(f"orchestrator: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
