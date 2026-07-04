"""The `snowline` operator CLI (console-script entry point).

Today it hosts the replication operator surface (replication-continuity §5/§7,
issue #82) under `snowline replicate ...`. Pairing and seeding are deliberately
CLI steps, not MCP surfaces — agents never manage replication plumbing (§5) — and
the CLI is a thin argparse shell over the pure libraries `replication_pairing`
and `replication_seed`, which do the real work over the SDK's tailnet-gated
admin surface + Postgres. Adding a subcommand is a new `add_parser` here plus a
handler.
"""

from __future__ import annotations

import argparse
import os
import sys

from snowline_platform import replication_pairing as pairing
from snowline_platform import replication_seed as seed

DEFAULT_LOCAL_PLATFORM_URL = "http://127.0.0.1:8848"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snowline", description="Snowline operator CLI")
    sub = parser.add_subparsers(dest="group", required=True)
    _build_replicate(sub.add_parser("replicate", help="replication pairing + seeding (§5/§7)"))
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (pairing.PairingError, seed.SeedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_replicate(p: argparse.ArgumentParser) -> None:
    rsub = p.add_subparsers(dest="command", required=True)

    pair = rsub.add_parser(
        "pair",
        help="pair this instance with a peer over the replication-admin surface",
        description=(
            "Run the §5 receiver-mints handshake in BOTH directions for every "
            "participant opted into replication on both instances (plus the "
            "platform's own scope stream). Runs ONCE per pair."
        ),
    )
    pair.add_argument("peer_url", help="the peer platform's base URL (tailnet address)")
    pair.add_argument(
        "--local-url",
        default=os.environ.get("SNOWLINE_LOCAL_PLATFORM_URL", DEFAULT_LOCAL_PLATFORM_URL),
        help="this instance's platform base URL (default: loopback :8848, §5.1)",
    )
    pair.add_argument(
        "--local-instance",
        default=os.environ.get("SNOWLINE_INSTANCE_ID"),
        help="this instance's SNOWLINE_INSTANCE_ID (e.g. 'roam'); defaults to the env",
    )
    pair.add_argument(
        "--peer-instance", required=True,
        help="the peer instance's SNOWLINE_INSTANCE_ID (e.g. 'primary')",
    )
    pair.add_argument(
        "--peer-host",
        default=None,
        help="the peer's tailnet host to reach its plugins on (§4.1); defaults "
        "to the peer URL's host. Plugin loopback base_urls in the peer registry "
        "are rewritten onto this host, port preserved (the runbook's serve "
        "posture maps each service's port 1:1 tailnet->loopback)",
    )
    pair.add_argument(
        "--dry-run", action="store_true",
        help="discover + plan (warnings, refusals) without driving the handshake",
    )
    pair.set_defaults(handler=_cmd_pair)

    seed_p = rsub.add_parser(
        "seed",
        help="stand up / re-seed a spoke from a primary snapshot (§7)",
        description=(
            "Seed a spoke per §7 (order load-bearing): prime → dump → scrub → "
            "inject (steps 1-3). BOOT the spoke, then re-run with --reverse-pair "
            "for step 4. Use --reseed for a fresh-epoch re-seed (checks both §7 "
            "step-5 preconditions and retires the old streams first)."
        ),
    )
    seed_p.add_argument("--config", required=True, help="path to the seed config JSON")
    seed_p.add_argument(
        "--reverse-pair", action="store_true",
        help="§7 step 4: pair the reverse (spoke->primary) direction after boot",
    )
    seed_p.add_argument(
        "--reseed", action="store_true",
        help="re-seed under a fresh epoch: check preconditions + retire old streams first",
    )
    seed_p.set_defaults(handler=_cmd_seed)

    check = rsub.add_parser(
        "reseed-check",
        help="check the two §7 re-seed preconditions without seeding",
    )
    check.add_argument("--config", required=True, help="path to the seed config JSON")
    check.set_defaults(handler=_cmd_reseed_check)


def _client():
    import httpx

    # follow_redirects so a trailing-slash / serve front doesn't break a POST;
    # a generous timeout because pg-adjacent admin calls can be slow under load.
    return httpx.Client(timeout=30.0, follow_redirects=True)


def _cmd_pair(args) -> int:
    if not args.local_instance:
        print(
            "error: --local-instance not given and SNOWLINE_INSTANCE_ID is unset",
            file=sys.stderr,
        )
        return 1
    from urllib.parse import urlsplit

    peer_host = args.peer_host or urlsplit(args.peer_url).hostname
    with _client() as client:
        local = pairing.discover_participants(client, args.local_url, args.local_instance)
        peer = pairing.discover_participants(
            client, args.peer_url, args.peer_instance, reachable_host=peer_host
        )
        print(
            f"discovered {sorted(local)} on local ({args.local_instance}), "
            f"{sorted(peer)} on peer ({args.peer_instance})"
        )
        if args.dry_run:
            plan = pairing.plan_pairing(local, peer)
            for note in plan.notes:
                print(note)
            print(
                f"dry-run: would pair {plan.to_pair}; one-sided {plan.one_sided}; "
                f"refused {plan.refused}"
            )
            return 1 if plan.refused else 0
        pairing.pair(client, local, peer, report=print)
    return 0


def _cmd_seed(args) -> int:
    with _client() as client:
        cfg = seed.load_seed_config(client, args.config)
        if args.reverse_pair:
            seed.run_reverse_pair(client, cfg, report=print)
            return 0
        if args.reseed:
            print("re-seed: checking §7 step-5 preconditions before touching state")
            seed.check_reseed_preconditions(client, cfg, report=print)
            seed.retire_old_streams(client, cfg, report=print)
        seed.run_seed(client, cfg, report=print)
    return 0


def _cmd_reseed_check(args) -> int:
    with _client() as client:
        cfg = seed.load_seed_config(client, args.config)
        seed.check_reseed_preconditions(client, cfg, report=print)
    return 0
