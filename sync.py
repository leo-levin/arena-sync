#!/usr/bin/env python3
"""Are.na group-to-ALL auto-mirror sync."""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.are.na/v3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)


class ArenaClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{BASE_URL}{path}"
        for attempt in range(5):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset - time.time(), 1)
                log.warning("Rate limited. Waiting %.0fs before retry.", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Request to {url} failed after retries.")


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        with open(p) as f:
            return json.load(f)
    return {"channels": {}, "mirrored_block_ids": []}


def save_state(state: dict, path: str):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def discover_channels(client: ArenaClient, group_id: str, max_pages: int) -> list[dict]:
    """Return list of {id, slug} for channels created by the group."""
    channels = []
    for page in range(1, max_pages + 1):
        data = client.get(f"/groups/{group_id}/contents", type="Channel", per=100, page=page)
        items = data.get("data", [])
        for item in items:
            if item.get("type") == "Channel":
                channels.append({"id": item["id"], "slug": item.get("slug", str(item["id"]))})
        if not data.get("meta", {}).get("has_more_pages"):
            break
    return channels


def fetch_new_blocks(
    client: ArenaClient,
    channel_id: int,
    checkpoint: dict | None,
    max_pages: int,
) -> list[dict]:
    """Return candidate blocks newer than checkpoint, newest first.

    Each item: {"id": int, "connected_at": str}
    Stops paginating once items are older than checkpoint.
    """
    checkpoint_ts = checkpoint["last_seen_connected_at"] if checkpoint else None
    seen_ids_at_ts = set(checkpoint.get("last_seen_ids_at_timestamp", [])) if checkpoint else set()

    candidates = []
    for page in range(1, max_pages + 1):
        data = client.get(
            f"/channels/{channel_id}/contents",
            sort="position_desc",
            per=100,
            page=page,
        )
        items = data.get("data", [])
        stop = False
        for item in items:
            # Skip channels embedded in channel contents
            if item.get("type") == "Channel":
                continue
            connection = item.get("connection", {})
            connected_at = connection.get("connected_at")
            if not connected_at:
                continue
            block_id = str(item["id"])

            if checkpoint_ts is None:
                # First run — collect to establish checkpoint, don't mirror
                candidates.append({"id": block_id, "connected_at": connected_at})
                continue

            if connected_at > checkpoint_ts:
                candidates.append({"id": block_id, "connected_at": connected_at})
            elif connected_at == checkpoint_ts:
                if block_id not in seen_ids_at_ts:
                    candidates.append({"id": block_id, "connected_at": connected_at})
            else:
                stop = True
                break

        if stop or not data.get("meta", {}).get("has_more_pages"):
            break

    return candidates


def mirror_block(
    client: ArenaClient,
    block_id: str,
    all_channel_id: int,
    dry_run: bool,
) -> bool:
    if dry_run:
        log.info("[DRY RUN] Would mirror block %s → channel %s", block_id, all_channel_id)
        return True
    try:
        client.post(
            "/connections",
            {"connectable_id": int(block_id), "connectable_type": "Block", "channel_ids": [all_channel_id]},
        )
        return True
    except requests.HTTPError as e:
        log.error("Failed to mirror block %s: %s", block_id, e)
        return False


def run_sync(config: dict, state: dict, state_path: str):
    token = os.environ.get("ARENA_TOKEN")
    if not token:
        raise RuntimeError("ARENA_TOKEN environment variable not set.")

    client = ArenaClient(token)
    group_id = config["group_id"]
    all_channel_id = config["all_channel_id"]
    excluded_ids = set(str(x) for x in config.get("excluded_channel_ids", []))
    max_pages = config.get("poll_recent_pages_per_channel", 2)
    discovery_pages = config.get("group_discovery_pages", 5)
    dry_run = config.get("dry_run", False)

    mirrored = set(str(x) for x in state.get("mirrored_block_ids", []))

    log.info("Run start. Group: %s, ALL channel: %s", group_id, all_channel_id)

    channels = discover_channels(client, group_id, discovery_pages)
    log.info("Discovered %d channel(s).", len(channels))

    # Exclude ALL channel and explicitly excluded IDs
    eligible = [
        ch for ch in channels
        if str(ch["id"]) != str(all_channel_id) and str(ch["id"]) not in excluded_ids
    ]
    skipped = len(channels) - len(eligible)
    log.info("Eligible: %d, Skipped: %d", len(eligible), skipped)

    total_mirrored = 0
    total_dupes = 0
    total_candidates = 0

    for ch in eligible:
        ch_id = ch["id"]
        ch_slug = ch["slug"]
        checkpoint = state["channels"].get(str(ch_id))
        is_first_run = checkpoint is None

        try:
            candidates = fetch_new_blocks(client, ch_id, checkpoint, max_pages)
        except Exception as e:
            log.error("Error fetching channel %s (%s): %s", ch_slug, ch_id, e)
            continue

        if is_first_run:
            # Establish checkpoint, mirror nothing
            if candidates:
                newest_ts = candidates[0]["connected_at"]
                ids_at_newest = [c["id"] for c in candidates if c["connected_at"] == newest_ts]
                state["channels"][str(ch_id)] = {
                    "last_seen_connected_at": newest_ts,
                    "last_seen_ids_at_timestamp": ids_at_newest,
                }
                log.info("Initialized checkpoint for channel %s (%s): %s", ch_slug, ch_id, newest_ts)
            else:
                state["channels"][str(ch_id)] = {
                    "last_seen_connected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last_seen_ids_at_timestamp": [],
                }
                log.info("No content found for channel %s (%s), set empty checkpoint.", ch_slug, ch_id)
            save_state(state, state_path)
            continue

        total_candidates += len(candidates)
        new_checkpoint_ts = checkpoint["last_seen_connected_at"]
        new_checkpoint_ids = set(checkpoint.get("last_seen_ids_at_timestamp", []))
        channel_mirrored = 0
        channel_dupes = 0

        for block in candidates:
            block_id = block["id"]
            connected_at = block["connected_at"]

            if block_id in mirrored:
                channel_dupes += 1
                total_dupes += 1
                # Still advance checkpoint
            else:
                success = mirror_block(client, block_id, all_channel_id, dry_run)
                if not success:
                    log.warning("Stopping channel %s after write failure on block %s.", ch_slug, block_id)
                    break
                mirrored.add(block_id)
                channel_mirrored += 1
                total_mirrored += 1
                log.info("Mirrored block %s from channel %s.", block_id, ch_slug)

            # Advance checkpoint candidate
            if connected_at > new_checkpoint_ts:
                new_checkpoint_ts = connected_at
                new_checkpoint_ids = {block_id}
            elif connected_at == new_checkpoint_ts:
                new_checkpoint_ids.add(block_id)

        state["channels"][str(ch_id)] = {
            "last_seen_connected_at": new_checkpoint_ts,
            "last_seen_ids_at_timestamp": list(new_checkpoint_ids),
        }
        state["mirrored_block_ids"] = list(mirrored)
        save_state(state, state_path)

        log.info(
            "Channel %s: %d candidate(s), %d mirrored, %d dupe(s).",
            ch_slug, len(candidates), channel_mirrored, channel_dupes,
        )

    log.info(
        "Run complete. Candidates: %d, Mirrored: %d, Dupes skipped: %d.",
        total_candidates, total_mirrored, total_dupes,
    )


def main():
    parser = argparse.ArgumentParser(description="Are.na group-to-ALL sync")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument("--state", default="state.json", help="Path to state JSON")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    state = load_state(args.state)
    run_sync(config, state, args.state)


if __name__ == "__main__":
    main()
