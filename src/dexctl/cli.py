from __future__ import annotations

import argparse
import json
import sys

from .app import DexctlApp, DexctlError
from .render import render_ls, render_ls_interactive, render_show
from .ui import PickerItem, reorder_items


def emit(result: dict, *, as_json: bool, render, exit_code: int = 0) -> int:
    if as_json:
        print(json.dumps({"status": "ok", "result": result}, indent=2))
    else:
        text = render(result)
        if text:
            print(text)
    return exit_code


def emit_error(exc: DexctlError, *, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
                indent=2,
            ),
            file=sys.stderr,
        )
    else:
        print(f"dexctl: {exc.message}", file=sys.stderr)
    return exc.exit_code


def build_parser() -> argparse.ArgumentParser:
    formatter = lambda prog: argparse.RawTextHelpFormatter(prog, max_help_position=32, width=100)
    parser = argparse.ArgumentParser(
        prog="dexctl",
        description=(
            "Codex account control plane.\n\n"
            "dexctl owns account registry/state, auth identity mapping, runtime auth installation and capture,\n"
            "usage inspection/cache, migration, and health validation. Shell, iTerm, and other automations are\n"
            "external adapters that call this CLI; they do not own account logic."
        ),
        epilog=(
            "Key workflows:\n"
            "  Inspect current state:\n"
            "    dexctl current\n"
            "    dexctl show\n"
            "    dexctl doctor --json\n\n"
            "  Switch accounts safely:\n"
            "    dexctl switch bob\n"
            "    dexctl cycle next\n"
            "    dexctl switch              # interactive picker\n\n"
            "  Shell adapter sequence:\n"
            "    dexctl show\n"
            "    dexctl runtime prepare --json\n"
            "    CODEX_HOME=<runtime_home> codex ...\n"
            "    dexctl runtime capture\n\n"
            "  Migration and lifecycle:\n"
            "    dexctl migrate legacy --dry-run\n"
            "    dexctl add work --label \"Work\" --from-auth /path/to/auth.json\n"
            "    dexctl remove work --yes\n\n"
            "Machine-readable output:\n"
            "  Adapter-facing commands support --json and return a stable envelope:\n"
            "    {\"status\": \"ok\", \"result\": ...}\n"
            "    {\"status\": \"error\", \"error\": {\"code\": ..., \"message\": ..., \"details\": ...}}"
        ),
        formatter_class=formatter,
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="COMMAND",
        description=(
            "Registry, runtime, migration, and operator commands. Run `dexctl COMMAND -h` for detailed usage."
        ),
    )

    def add_json_flag(cmd):
        cmd.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of human output.",
        )

    ls_cmd = sub.add_parser(
        "ls",
        help="List registered accounts with plan, usage, and health summary.",
        description=(
            "List all registered accounts in persisted cycle order.\n\n"
            "Default behavior is stale-aware: cached usage is reused when fresh and refreshed when stale.\n"
            "Use --refresh to force a live usage fetch or --cached for a strictly local read."
        ),
        formatter_class=formatter,
    )
    ls_mode = ls_cmd.add_mutually_exclusive_group()
    ls_mode.add_argument(
        "--refresh",
        action="store_true",
        help="Force live usage refresh for every listed account.",
    )
    ls_mode.add_argument(
        "--cached",
        action="store_true",
        help="Use cached usage data only; do not attempt network refresh.",
    )
    ls_cmd.add_argument(
        "--pick",
        action="store_true",
        help="Open the interactive account picker. Navigate with j/k or arrows, Enter to switch.",
    )
    add_json_flag(ls_cmd)

    current_cmd = sub.add_parser(
        "current",
        help="Show the active account and whether runtime auth matches it.",
        description="Read the active account from the registry and report runtime alignment.",
        formatter_class=formatter,
    )
    add_json_flag(current_cmd)

    show_cmd = sub.add_parser(
        "show",
        help="Render the shell-facing account and usage block.",
        description=(
            "Render the active account header plus usage lines used by the shell adapter before launching Codex.\n"
            "This is the CLI replacement for the old shell-computed pre-launch banner."
        ),
        formatter_class=formatter,
    )
    add_json_flag(show_cmd)

    switch_cmd = sub.add_parser(
        "switch",
        help="Switch the active account and prepare runtime auth immediately.",
        description=(
            "Switch to a specific account id or open the interactive picker when no account id is supplied.\n\n"
            "A successful switch updates:\n"
            "  1. the active account in the registry\n"
            "  2. the shared runtime auth in ~/.codex-shared/auth.json\n"
            "  3. the compatibility pointer file when enabled"
        ),
        formatter_class=formatter,
    )
    switch_cmd.add_argument(
        "account_id",
        nargs="?",
        help="Registered account id to activate. Omit to open the interactive picker.",
    )
    add_json_flag(switch_cmd)

    cycle_cmd = sub.add_parser(
        "cycle",
        help="Advance to the next or previous account in persisted order.",
        description=(
            "Cycle the active account using registry order and prepare runtime auth immediately.\n"
            "This is the primary primitive for hotkeys and thin terminal adapters."
        ),
        formatter_class=formatter,
    )
    cycle_cmd.add_argument(
        "direction",
        nargs="?",
        choices=["next", "prev"],
        default="next",
        help="Cycle direction. Defaults to `next`.",
    )
    add_json_flag(cycle_cmd)

    inspect_cmd = sub.add_parser(
        "inspect",
        help="Show detailed information for one account.",
        description=(
            "Inspect a single account in detail, including auth health, usage state, and compatibility paths.\n"
            "By default this refreshes usage unless --cached is requested."
        ),
        formatter_class=formatter,
    )
    inspect_cmd.add_argument("account_id", help="Registered account id to inspect.")
    inspect_mode = inspect_cmd.add_mutually_exclusive_group()
    inspect_mode.add_argument(
        "--refresh",
        action="store_true",
        help="Force live usage refresh for the inspected account.",
    )
    inspect_mode.add_argument(
        "--cached",
        action="store_true",
        help="Use cached usage only; do not attempt a live refresh.",
    )
    add_json_flag(inspect_cmd)

    add_cmd = sub.add_parser(
        "add",
        help="Add a new account to the registry.",
        description=(
            "Create a new account entry from imported auth or a staged native login flow.\n\n"
            "Flow selection:\n"
            "  - --from-auth PATH: import an existing auth.json snapshot\n"
            "  - no --from-auth: run native `codex login` in an isolated staged home, let the operator log in,\n"
            "                    then capture the resulting auth and decode the canonical email from it\n\n"
            "Design notes:\n"
            "  - account_id is the stable internal identifier and is always required\n"
            "  - label is optional; if omitted it defaults to the decoded email\n"
            "  - --email is assertion-only safety validation, not the source of truth\n"
            "  - if browser login fails upstream with an org/workspace error (for example\n"
            "    `codex_cli_workspace_disabled`), dexctl will surface that as an upstream Codex workspace\n"
            "    limitation rather than pretending the account was added"
        ),
        formatter_class=formatter,
    )
    add_cmd.add_argument("account_id", help="New account id. Must match [a-z0-9-]+.")
    add_cmd.add_argument(
        "--email",
        help="Optional expected email. The decoded auth identity must match it exactly.",
    )
    add_cmd.add_argument(
        "--label",
        help="Optional display label. Defaults to the decoded email from the imported/logged-in auth.",
    )
    add_cmd.add_argument(
        "--device-auth",
        action="store_true",
        help="Use native `codex login --device-auth` instead of browser login. Only applies without --from-auth.",
    )
    add_cmd.add_argument(
        "--activate",
        action="store_true",
        help="Activate the new account immediately after creation.",
    )
    add_cmd.add_argument(
        "--from-auth",
        help="Import auth from an existing auth.json file instead of running native login.",
    )
    add_json_flag(add_cmd)

    remove_cmd = sub.add_parser(
        "remove",
        help="Remove an account from the registry.",
        description=(
            "Remove an account, optionally switching away first when it is active.\n\n"
            "Safety rules:\n"
            "  - refuses to remove the last remaining account\n"
            "  - requires --yes confirmation\n"
            "  - creates a backup by default before removing auth"
        ),
        formatter_class=formatter,
    )
    remove_cmd.add_argument("account_id", help="Registered account id to remove.")
    remove_cmd.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the removal. Required for destructive execution.",
    )
    remove_cmd.add_argument(
        "--switch-to",
        help="Explicit fallback account id if removing the currently active account.",
    )
    backup_group = remove_cmd.add_mutually_exclusive_group()
    backup_group.add_argument(
        "--keep-backup",
        action="store_true",
        help="Explicitly keep a backup before deletion. This is the default behavior.",
    )
    backup_group.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup creation before deletion.",
    )
    add_json_flag(remove_cmd)

    move_cmd = sub.add_parser(
        "move",
        help="Move one account within persisted cycle order.",
        description="Scriptable reorder primitive for changing account_order without the interactive UI.",
        formatter_class=formatter,
    )
    move_cmd.add_argument("account_id", help="Account id to reposition.")
    group = move_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--before", help="Insert before another account id.")
    group.add_argument("--after", help="Insert after another account id.")
    group.add_argument("--first", action="store_true", help="Move to the first position.")
    group.add_argument("--last", action="store_true", help="Move to the last position.")
    add_json_flag(move_cmd)

    reorder_cmd = sub.add_parser(
        "reorder",
        help="Open the interactive reorder view.",
        description=(
            "Interactive reorder UI.\n\n"
            "Keys:\n"
            "  j/k or arrows  move cursor or held item\n"
            "  Space          pick up / drop item\n"
            "  Enter          save\n"
            "  Esc or q       cancel"
        ),
        formatter_class=formatter,
    )
    add_json_flag(reorder_cmd)

    doctor_cmd = sub.add_parser(
        "doctor",
        help="Validate registry, auth, runtime, cache, and compatibility state.",
        description=(
            "Run a deep health check over dexctl state.\n\n"
            "doctor reports problems such as:\n"
            "  - missing or invalid primary auth\n"
            "  - auth/email mismatches\n"
            "  - runtime drift\n"
            "  - compatibility pointer drift\n"
            "  - legacy mirror drift\n"
            "  - missing native codex binary\n"
            "  - stale or corrupt usage cache"
        ),
        formatter_class=formatter,
    )
    doctor_cmd.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failure conditions for the command exit code.",
    )
    add_json_flag(doctor_cmd)

    runtime_cmd = sub.add_parser(
        "runtime",
        help="Runtime auth preparation, capture, and inspection commands.",
        description=(
            "Runtime commands manage the shared Codex home used by external launch adapters.\n"
            "These commands do not launch Codex themselves; they are intended to be called by wrappers."
        ),
        formatter_class=formatter,
    )
    runtime_sub = runtime_cmd.add_subparsers(
        dest="runtime_command",
        required=True,
        title="runtime commands",
        metavar="RUNTIME_COMMAND",
    )
    runtime_prepare = runtime_sub.add_parser(
        "prepare",
        help="Install selected account auth into the shared runtime home.",
        description=(
            "Prepare runtime state for a subsequent native Codex launch.\n\n"
            "This enforces file-backed auth in the runtime config, installs the selected account auth into\n"
            "the shared runtime home, and updates the compatibility pointer when enabled."
        ),
        formatter_class=formatter,
    )
    runtime_prepare.add_argument(
        "account_id",
        nargs="?",
        help="Account id to prepare. Defaults to the active account.",
    )
    add_json_flag(runtime_prepare)
    runtime_capture = runtime_sub.add_parser(
        "capture",
        help="Capture post-run runtime auth back to the correct registered account.",
        description=(
            "Inspect the shared runtime auth after a native Codex run and write it back only to the\n"
            "registered account whose decoded auth identity matches the runtime email."
        ),
        formatter_class=formatter,
    )
    add_json_flag(runtime_capture)
    runtime_status = runtime_sub.add_parser(
        "status",
        help="Inspect active-vs-runtime alignment without mutating state.",
        description=(
            "Report current runtime state, including runtime auth identity, active account, compatibility pointer,\n"
            "and whether the runtime config is valid."
        ),
        formatter_class=formatter,
    )
    add_json_flag(runtime_status)

    migrate_cmd = sub.add_parser(
        "migrate",
        help="Migration commands for importing legacy Codex account layouts.",
        description="Import legacy state into the dexctl control-plane model.",
        formatter_class=formatter,
    )
    migrate_sub = migrate_cmd.add_subparsers(
        dest="migrate_command",
        required=True,
        title="migration commands",
        metavar="MIGRATION_COMMAND",
    )
    migrate_legacy = migrate_sub.add_parser(
        "legacy",
        help="Import the current legacy shell/helper layout into dexctl.",
        description=(
            "Import legacy account state from ~/.codex-active-account, ~/.codex-*/auth.json,\n"
            "and ~/.codex-auth-vault/* into ~/.codex-account/.\n\n"
            "The migration prefers verified primary auth over mismatched mirror auth and prepares\n"
            "runtime auth for the imported active account."
        ),
        formatter_class=formatter,
    )
    migrate_legacy.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and report the migration plan without writing state.",
    )
    add_json_flag(migrate_legacy)

    return parser


def pick_switch_account(app: DexctlApp):
    registry = app.load_registry()
    listing = app.list_result(registry, mode="cached")
    try:
        chosen = render_ls_interactive(listing, app)
    except RuntimeError as exc:
        raise DexctlError("interactive_unavailable", str(exc)) from exc
    if chosen is None:
        raise DexctlError("cancelled", "switch cancelled", exit_code=1)
    return chosen


def interactive_reorder(app: DexctlApp):
    registry = app.load_registry()
    items = [
        PickerItem(
            key=account_id,
            lines=[
                f"{account_id}  {registry.accounts[account_id].label}",
                f"   {registry.accounts[account_id].email}",
            ],
        )
        for account_id in registry.account_order
    ]
    try:
        result = reorder_items(
            items,
            "Reorder Codex accounts",
            "j/k or arrows: move  Space: pick/drop  Enter: save  Esc/q: cancel",
        )
    except RuntimeError as exc:
        raise DexctlError("interactive_unavailable", str(exc)) from exc
    if result is None:
        raise DexctlError("cancelled", "reorder cancelled", exit_code=1)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = DexctlApp()
    as_json = getattr(args, "json", False)
    try:
        if args.command == "migrate":
            result = app.migrate_legacy(dry_run=args.dry_run)
            return emit(result, as_json=as_json, render=lambda r: f"Imported {len(r['accounts'])} accounts into {r['registry_path']}" if not r["dry_run"] else f"Would import {len(r['accounts'])} accounts into {r['registry_path']}")

        registry = app.load_registry()

        if args.command == "ls":
            mode = "refresh" if args.refresh else "cached" if args.cached else "auto"
            if getattr(args, "pick", False):
                result = app.list_result(registry, mode=mode)
                try:
                    chosen = render_ls_interactive(result, app)
                except RuntimeError as exc:
                    raise DexctlError("interactive_unavailable", str(exc)) from exc
                if chosen and chosen != registry.active_account_id:
                    with app.locked():
                        registry = app.load_registry()
                        app.switch_account(registry, chosen)
                return 0
            result = app.list_result(registry, mode=mode)
            if as_json:
                print(json.dumps({"status": "ok", "result": result}, indent=2))
                return 0
            render_ls(result, app)
            return 0
        if args.command == "current":
            return emit(app.current_result(registry), as_json=as_json, render=app.render_current)
        if args.command == "show":
            result = app.show_result(registry, mode="auto")
            if as_json:
                print(json.dumps({"status": "ok", "result": result}, indent=2))
                return 0
            render_show(result, app)
            return 0
        if args.command == "switch":
            account_id = args.account_id or pick_switch_account(app)
            with app.locked():
                registry = app.load_registry()
                return emit(app.switch_account(registry, account_id), as_json=as_json, render=lambda r: f"Active account: {r['active_account_id']}")
        if args.command == "cycle":
            with app.locked():
                registry = app.load_registry()
                return emit(app.cycle_account(registry, args.direction), as_json=as_json, render=lambda r: f"Active account: {r['active_account_id']}")
        if args.command == "inspect":
            mode = "refresh" if args.refresh else "cached" if args.cached else "refresh"
            return emit(app.inspect_result(registry, args.account_id, mode=mode), as_json=as_json, render=app.render_inspect)
        if args.command == "add":
            with app.locked():
                registry = app.load_registry()
                result = app.add_account(
                    registry,
                    args.account_id,
                    email=args.email,
                    label=args.label,
                    activate=args.activate,
                    device_auth=args.device_auth,
                    from_auth=args.from_auth,
                )
                return emit(result, as_json=as_json, render=lambda r: f"Added account: {r['account']['id']}")
        if args.command == "remove":
            with app.locked():
                registry = app.load_registry()
                result = app.remove_account(
                    registry,
                    args.account_id,
                    yes=args.yes,
                    switch_to=args.switch_to,
                    keep_backup=not args.no_backup,
                )
                return emit(result, as_json=as_json, render=lambda r: f"Removed account: {r['removed_account_id']}")
        if args.command == "move":
            with app.locked():
                registry = app.load_registry()
                result = app.move_account(
                    registry,
                    args.account_id,
                    before=args.before,
                    after=args.after,
                    first=args.first,
                    last=args.last,
                )
                return emit(result, as_json=as_json, render=lambda r: " -> ".join(r["account_order"]))
        if args.command == "reorder":
            new_order = interactive_reorder(app)
            with app.locked():
                registry = app.load_registry()
                result = app.reorder_accounts(registry, new_order)
            return emit(result, as_json=as_json, render=lambda r: " -> ".join(r["account_order"]))
        if args.command == "doctor":
            result = app.doctor(registry, strict=args.strict)
            exit_code = 0 if result["ok"] and not result["strict_failure"] else 1
            return emit(result, as_json=as_json, render=app.render_doctor, exit_code=exit_code)
        if args.command == "runtime":
            if args.runtime_command == "prepare":
                with app.locked():
                    registry = app.load_registry()
                    return emit(app.prepare_runtime(registry, args.account_id), as_json=as_json, render=lambda r: f"Prepared runtime for {r['account_id']}")
            if args.runtime_command == "capture":
                with app.locked():
                    registry = app.load_registry()
                    return emit(app.capture_runtime(registry), as_json=as_json, render=lambda r: f"Captured runtime auth for {r['account_id']}")
            return emit(app.runtime_status(registry), as_json=as_json, render=lambda r: json.dumps(r, indent=2))
        raise DexctlError("unsupported_command", "unsupported command")
    except DexctlError as exc:
        return emit_error(exc, as_json=as_json)


if __name__ == "__main__":
    raise SystemExit(main())
