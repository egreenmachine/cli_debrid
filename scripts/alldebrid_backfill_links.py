#!/usr/bin/env python3
"""Backfill AllDebrid direct download links for torrents already in the account.

cli_debrid only unlocks a torrent's links at add time, so torrents that were
added before that behaviour existed never had links generated. This walks every
magnet in the account and unlocks the ones that are Ready, using the same code
path the application uses.

Unlocking happens on AllDebrid's side, so running this out-of-process has the
same effect on the account as the application doing it. It does not populate the
running app's in-memory link cache, and it does not touch the cli_debrid
database at all.

Re-runnable: unlocking an already-unlocked link is harmless.

Dry run (default) - shows what would be unlocked, makes no unlock calls:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py

Unlock everything:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py --execute

Try a handful first:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py --execute --limit 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from debrid.alldebrid.api import make_request, MAGNET_STATUS_CODES  # noqa: E402
from debrid.alldebrid.client import AllDebridProvider  # noqa: E402
from debrid.common import is_unwanted_file, is_video_file  # noqa: E402

READY_STATUS_CODE = 4


def fetch_magnets(provider: AllDebridProvider) -> list:
    """Return every magnet in the account."""
    result = make_request('GET', '/v4.1/magnet/status', provider.api_key, use_query_auth=True)
    if not result or result.get('status') != 'success':
        raise RuntimeError(f"Could not list magnets: {result}")

    magnets = result.get('data', {}).get('magnets', [])
    # v4.1 returns a dict for a single magnet, a list for several
    if isinstance(magnets, dict):
        magnets = [magnets]
    return magnets or []


def count_candidates(provider: AllDebridProvider, torrent_id: str, video_only: bool) -> int:
    """Count the files that would be unlocked, without unlocking anything."""
    info = provider.get_torrent_info(torrent_id)
    if not info:
        return 0

    files = info.get('files', [])
    linked = [f for f in files if f.get('link')]
    if not video_only:
        return len(linked)

    videos = [
        f for f in linked
        if is_video_file(f.get('path', '') or f.get('name', ''))
        and not is_unwanted_file(f.get('path', '') or f.get('name', ''))
    ]
    # generate_download_links() falls back to every linked file when the video
    # filter matches nothing, so mirror that here.
    return len(videos) if videos else len(linked)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true",
                        help="Actually unlock. Without this the script only reports.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N ready torrents.")
    parser.add_argument("--include-non-video", action="store_true",
                        help="Unlock every file, not just video files.")
    parser.add_argument("--json-out", default=None,
                        help="Write a per-torrent report to this path.")
    parser.add_argument("--verbose", action="store_true",
                        help="Show cli_debrid's own debrid logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    provider = AllDebridProvider()
    try:
        magnets = fetch_magnets(provider)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    ready = [m for m in magnets if m.get('statusCode') == READY_STATUS_CODE]

    skipped: dict[str, int] = {}
    for m in magnets:
        code = m.get('statusCode')
        if code != READY_STATUS_CODE:
            label = MAGNET_STATUS_CODES.get(code, f"Unknown ({code})")
            skipped[label] = skipped.get(label, 0) + 1

    print(f"{len(magnets)} magnet(s) in account: {len(ready)} Ready, {len(magnets) - len(ready)} not.")
    for label, count in sorted(skipped.items()):
        print(f"  skipping {count} x {label}")

    if args.limit is not None:
        ready = ready[:args.limit]
        print(f"Limited to the first {len(ready)} ready torrent(s).")

    if not ready:
        print("Nothing to do.")
        return 0

    if not args.execute:
        print("\nDRY RUN - no links will be unlocked. Re-run with --execute.\n")

    video_only = not args.include_non_video
    report = []
    total_links = 0
    failures = 0
    started = time.time()

    for idx, magnet in enumerate(ready, start=1):
        torrent_id = str(magnet.get('id', ''))
        name = magnet.get('filename', '(unnamed)')
        prefix = f"[{idx}/{len(ready)}]"

        try:
            if args.execute:
                links = provider.generate_download_links(
                    torrent_id, video_only=video_only
                )
                count = len(links)
                if count:
                    print(f"{prefix} {count} link(s): {name}")
                else:
                    failures += 1
                    print(f"{prefix} NO LINKS GENERATED: {name}")
                report.append({
                    "id": torrent_id, "name": name, "links_generated": count,
                    "links": [link_info["direct_link"] for link_info in links],
                })
            else:
                count = count_candidates(provider, torrent_id, video_only)
                print(f"{prefix} would unlock {count} file(s): {name}")
                report.append({"id": torrent_id, "name": name, "would_unlock": count})

            total_links += count

        except KeyboardInterrupt:
            print("\nInterrupted. Progress so far is kept - the script is re-runnable.")
            break
        except Exception as e:
            failures += 1
            print(f"{prefix} ERROR on {name}: {e}", file=sys.stderr)
            report.append({"id": torrent_id, "name": name, "error": str(e)})

    elapsed = time.time() - started
    verb = "Generated" if args.execute else "Would generate"
    print(f"\n{verb} {total_links} link(s) across {len(report)} torrent(s) in {elapsed:.0f}s.")
    if failures:
        print(f"{failures} torrent(s) failed - see above.", file=sys.stderr)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"Report written to {args.json_out}")

    return 1 if failures and args.execute else 0


if __name__ == "__main__":
    sys.exit(main())
