#!/usr/bin/env python3
"""Backfill AllDebrid links for torrents already in the account.

cli_debrid only generates links at add time, so torrents added before that
behaviour existed never got any. This walks every magnet in the account,
unlocks the Ready ones and saves them to the account's Links list - the
"saved for later" section that AllDebrid's WebDAV exposes as links/.

Standalone by design: it talks to the AllDebrid API directly rather than
importing the application, because debrid/, database/ and routes/ form an
import cycle that only resolves under main.py's specific ordering. The only
application code it borrows is the video-file filter, loaded by file path.

It reads the API key from the same config.json the app uses and touches no
database. Re-runnable: unlocking and saving are both idempotent.

Dry run (default), makes no unlock or save calls:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py

Unlock and save everything:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py --execute

Try a few first:
    docker exec cli_debrid python /app/scripts/alldebrid_backfill_links.py --execute --limit 5
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = "https://api.alldebrid.com"
READY_STATUS_CODE = 4
RATE_LIMIT_INTERVAL = 0.084  # 12 requests/second

# AllDebrid magnet status codes, for reporting what was skipped.
STATUS_LABELS = {
    0: 'In Queue', 1: 'Downloading', 2: 'Compressing', 3: 'Uploading', 4: 'Ready',
    5: 'Error', 6: 'Virus', 7: 'Dead', 8: 'Error - No peer', 9: 'Error - Internal',
    10: 'Error - Limit reached', 11: 'Magnet conversion error', 15: 'Unavailable - No peer',
}


def _load_filters():
    """Load is_video_file/is_unwanted_file from the app without importing packages."""
    path = os.path.join(ROOT, "debrid", "common", "utils.py")
    spec = importlib.util.spec_from_file_location("_ad_backfill_utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_video_file, module.is_unwanted_file


is_video_file, is_unwanted_file = _load_filters()


def read_config() -> dict:
    config_path = os.path.join(os.environ.get("USER_CONFIG", "/user/config"), "config.json")
    with open(config_path) as fh:
        return json.load(fh)


class AllDebrid:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self._last_request = 0.0
        self.calls = 0

    def _request(self, method: str, endpoint: str, params=None, data=None):
        elapsed = time.time() - self._last_request
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        self._last_request = time.time()
        self.calls += 1

        params = dict(params or {})
        params.update({"agent": "cli_debrid_backfill", "apikey": self.api_key})
        url = f"{API_BASE}{endpoint}"

        response = self.session.request(method, url, params=params, data=data, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            error = payload.get("error", {})
            raise RuntimeError(f"{error.get('code', 'ERROR')}: {error.get('message', payload)}")
        return payload.get("data", {})

    def magnets(self) -> list:
        data = self._request("GET", "/v4.1/magnet/status")
        magnets = data.get("magnets", [])
        if isinstance(magnets, dict):
            magnets = [magnets]
        return magnets or []

    def files(self, torrent_id: str) -> list:
        data = self._request("GET", "/v4.1/magnet/status", params={"id": torrent_id})
        magnets = data.get("magnets", data)
        if isinstance(magnets, list):
            magnets = magnets[0] if magnets else {}
        return flatten_tree(magnets.get("files", []))

    def unlock(self, link: str):
        data = self._request("GET", "/v4/link/unlock", params={"link": link})
        if data.get("delayed"):
            return self._wait_delayed(str(data["delayed"]))
        return data.get("link")

    def _wait_delayed(self, delayed_id: str, attempts: int = 6):
        for attempt in range(attempts):
            time.sleep(min(2 + attempt, 10))
            data = self._request("GET", "/v4/link/delayed", params={"id": delayed_id})
            if data.get("status") == 2:
                return data.get("link")
            if data.get("status") == 3:
                return None
        return None

    def save(self, links: list) -> bool:
        if not links:
            return False
        self._request("POST", "/v4/user/links/save", data={"links[]": links})
        return True


def flatten_tree(nodes: list, prefix: str = "") -> list:
    """Flatten AllDebrid's nested file tree into {path, bytes, link} dicts."""
    files = []
    for node in nodes:
        name = node.get("n", "")
        if "e" in node:
            subpath = f"{prefix}/{name}" if prefix else name
            files.extend(flatten_tree(node["e"], subpath))
        else:
            files.append({
                "path": f"{prefix}/{name}" if prefix else name,
                "bytes": node.get("s", 0),
                "link": node.get("l", ""),
            })
    return files


def select_files(files: list, video_only: bool) -> list:
    """Mirror the provider's selection: video files, else every linked file."""
    linked = [f for f in files if f.get("link")]
    if not video_only:
        return linked
    videos = [f for f in linked
              if is_video_file(f["path"]) and not is_unwanted_file(f["path"])]
    return videos or linked


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true",
                        help="Actually unlock and save. Without this the script only reports.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N ready torrents.")
    parser.add_argument("--include-non-video", action="store_true",
                        help="Process every file, not just video files.")
    parser.add_argument("--save-only", action="store_true",
                        help="Skip unlocking; only save links to the account. Halves the API "
                             "calls, and saving is what populates the Links folder.")
    parser.add_argument("--json-out", default=None, help="Write a per-torrent report here.")
    args = parser.parse_args()

    try:
        config = read_config()
    except Exception as e:
        print(f"ERROR: could not read config.json: {e}", file=sys.stderr)
        return 1

    section = config.get("Debrid Provider", {})
    provider, api_key = section.get("provider", ""), section.get("api_key", "")
    if not api_key:
        print("ERROR: no Debrid Provider api_key in config.json", file=sys.stderr)
        return 1
    if provider.lower().replace("-", "") != "alldebrid":
        print(f"WARNING: configured provider is {provider!r}, not AllDebrid. "
              f"Using its API key against AllDebrid anyway.", file=sys.stderr)

    client = AllDebrid(api_key)
    try:
        magnets = client.magnets()
    except Exception as e:
        print(f"ERROR: could not list magnets: {e}", file=sys.stderr)
        return 1

    ready = [m for m in magnets if m.get("statusCode") == READY_STATUS_CODE]
    skipped: dict = {}
    for m in magnets:
        code = m.get("statusCode")
        if code != READY_STATUS_CODE:
            label = STATUS_LABELS.get(code, f"Unknown ({code})")
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
        print("\nDRY RUN - nothing will be unlocked or saved. Re-run with --execute.\n")

    video_only = not args.include_non_video
    report, total_files, unlocked, saved, failures = [], 0, 0, 0, 0
    started = time.time()

    for idx, magnet in enumerate(ready, start=1):
        torrent_id, name = str(magnet.get("id", "")), magnet.get("filename", "(unnamed)")
        prefix = f"[{idx}/{len(ready)}]"
        try:
            chosen = select_files(client.files(torrent_id), video_only)
            total_files += len(chosen)
            if not chosen:
                print(f"{prefix} no linked files: {name}")
                report.append({"id": torrent_id, "name": name, "files": 0})
                continue

            if not args.execute:
                print(f"{prefix} would process {len(chosen)} file(s): {name}")
                report.append({"id": torrent_id, "name": name, "files": len(chosen)})
                continue

            direct = []
            if not args.save_only:
                for f in chosen:
                    link = client.unlock(f["link"])
                    if link:
                        direct.append(link)
                unlocked += len(direct)

            client.save([f["link"] for f in chosen])
            saved += len(chosen)
            print(f"{prefix} saved {len(chosen)} link(s)"
                  f"{f', unlocked {len(direct)}' if direct else ''}: {name}")
            report.append({"id": torrent_id, "name": name, "files": len(chosen),
                           "unlocked": len(direct), "saved": len(chosen)})

        except KeyboardInterrupt:
            print("\nInterrupted. Work so far stands - the script is re-runnable.")
            break
        except Exception as e:
            failures += 1
            print(f"{prefix} ERROR on {name}: {e}", file=sys.stderr)
            report.append({"id": torrent_id, "name": name, "error": str(e)})

    elapsed = time.time() - started
    if args.execute:
        print(f"\nSaved {saved} link(s), unlocked {unlocked}, across {len(report)} torrent(s) "
              f"in {elapsed:.0f}s ({client.calls} API calls).")
    else:
        print(f"\nWould process {total_files} file(s) across {len(report)} torrent(s). "
              f"That is roughly {total_files * (1 if args.save_only else 2)} API calls.")
    if failures:
        print(f"{failures} torrent(s) failed - see above.", file=sys.stderr)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"Report written to {args.json_out}")

    return 1 if failures and args.execute else 0


if __name__ == "__main__":
    sys.exit(main())
