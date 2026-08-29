"""The `snowline` operator CLI (console-script entry point).

Two operator surfaces live here today:

- `snowline replicate ...` — replication pairing + seeding
  (replication-continuity §5/§7, issue #82). Deliberately CLI, not MCP: agents
  never manage replication plumbing (§5).
- `snowline release ...` — cutting the packaged stable channel's release trains
  (macOS distribution spec §2/§4, issue #202). Host-side by design; see
  `snowline_platform.release.cutter` for why it is not a set of CI workflows.

Both are thin argparse shells over pure libraries — `replication_pairing` /
`replication_seed`, and `release.model` / `release.cutter`. Adding a subcommand is
a new `add_parser` here plus a handler.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from snowline_platform import release as release_lib
from snowline_platform import replication_pairing as pairing
from snowline_platform import replication_seed as seed

DEFAULT_LOCAL_PLATFORM_URL = "http://127.0.0.1:8848"

# Where `release/components.json` is looked for when --config is not given.
RELEASE_CONFIG_RELPATH = Path("release/components.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snowline", description="Snowline operator CLI")
    sub = parser.add_subparsers(dest="group", required=True)
    _build_replicate(sub.add_parser("replicate", help="replication pairing + seeding (§5/§7)"))
    _build_release(sub.add_parser("release", help="cut packaged release trains (§2/§4)"))
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (pairing.PairingError, seed.SeedError, release_lib.ReleaseError) as exc:
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


def _build_release(p: argparse.ArgumentParser) -> None:
    rsub = p.add_subparsers(dest="command", required=True)

    cut = rsub.add_parser(
        "cut",
        help="build, smoke-test, tag and publish a release train",
        description=(
            "Cut a release train HOST-SIDE from local checkouts (item #202: no "
            "GitHub Actions in plugin repos). Builds each component's wheels "
            "from a throwaway worktree at its blessed main sha, exports "
            "per-service locks, boots every service from its wheel against an "
            "empty database (spec §2.1), writes release/train.json, then tags "
            "each repo and publishes its own assets. Safe to re-run after a "
            "partial failure: existing tags and releases are reported and "
            "skipped, never duplicated."
        ),
    )
    cut.add_argument("--version", required=True, help="the train version, e.g. v0.1.0")
    cut.add_argument(
        "--respin", default=None, metavar="COMPONENT",
        help="PATCH respin: rebuild and re-tag ONLY this component; every other "
        "service keeps its previous manifest entry, tag included (spec §4)",
    )
    cut.add_argument("--skip-tests", action="store_true", help="skip the per-repo test runs")
    cut.add_argument(
        "--skip-smoke", action="store_true",
        help="skip the §2.1 wheel-boot smoke tests (they need local Postgres)",
    )
    cut.add_argument(
        "--dry-run", action="store_true",
        help="preflight + plan only; builds, tags, releases and databases are untouched",
    )
    _add_release_common(cut)
    cut.add_argument(
        "--out", default=None,
        help="build directory (default: <platform checkout>/.release-build/<version>)",
    )
    cut.set_defaults(handler=_cmd_release_cut)

    status = rsub.add_parser(
        "status",
        help="show the current train vs the checkouts' HEADs",
        description="What release/train.json records, and what a cut would pick up now.",
    )
    _add_release_common(status)
    status.set_defaults(handler=_cmd_release_status)


def _add_release_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config", default=os.environ.get("SNOWLINE_RELEASE_CONFIG"),
        help="path to release/components.json (default: found upward from cwd)",
    )
    p.add_argument(
        "--checkout", action="append", default=[], metavar="NAME=PATH",
        help="override a component's checkout path; repeatable",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="echo every command as it runs"
    )


def _find_release_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        found = candidate / RELEASE_CONFIG_RELPATH
        if found.exists():
            return found
    # Report the miss against cwd so the error names a real path.
    return here / RELEASE_CONFIG_RELPATH


def _release_context(args):
    config = release_lib.load_config(_find_release_config(args.config))
    overrides: dict[str, str] = {}
    for pair in args.checkout:
        if "=" not in pair:
            raise release_lib.ReleaseError(f"--checkout wants NAME=PATH, got {pair!r}")
        name, _, path = pair.partition("=")
        overrides[name] = path
    # An override naming no component is a typo, and a silently ignored one
    # means the cut proceeds against the DEFAULT checkout — potentially
    # blessing the wrong sha (#207 review). Refuse loudly instead.
    known = {comp.name for comp in config.components}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise release_lib.ReleaseError(
            f"--checkout names unknown component(s) {', '.join(unknown)} — "
            f"components are: {', '.join(sorted(known))}"
        )
    checkouts = {
        comp.name: release_lib.resolve_checkout(config, comp, overrides)
        for comp in config.components
    }
    return config, checkouts


def _cmd_release_cut(args) -> int:
    from snowline_platform.release import runner as release_runner

    config, checkouts = _release_context(args)
    out = (
        Path(args.out).expanduser().resolve()
        if args.out
        else checkouts[config.manifest_component.name] / ".release-build" / args.version
    )
    runner = release_runner.Runner(report=print, dry_run=args.dry_run, verbose=args.verbose)
    release_lib.cut(
        config,
        args.version,
        checkouts=checkouts,
        out_dir=out,
        runner=runner,
        respin=args.respin,
        skip_tests=args.skip_tests,
        skip_smoke=args.skip_smoke,
        report=print,
    )
    return 0


def _cmd_release_status(args) -> int:
    from snowline_platform.release import runner as release_runner

    config, checkouts = _release_context(args)
    runner = release_runner.Runner(report=print, verbose=args.verbose)
    return release_lib.status(config, checkouts=checkouts, runner=runner, report=print)


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
