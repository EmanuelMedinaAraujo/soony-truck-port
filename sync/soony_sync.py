#!/usr/bin/env python3
"""Soony portal sync: poll the file listing, download only what changed, log events.

The portal exposes GET /api/v1/files -> {count, files:[{path, bytes, sha256, url}]}.
That listing is the cheap "hash check": we diff it against our last manifest and
only fetch files whose sha256 changed. Nothing is ever overwritten blindly:

  data/blobs/<sha256>          content-addressed store (every version ever seen)
  data/current/<path>          materialised latest tree (what the agents read)
  data/manifest.json           last seen listing {path: {sha256, bytes, first_seen, last_changed}}
  data/changes.jsonl           append-only event log (added / modified / removed, + CSV row diffs)

Usage:
  SOONY_TOKEN=... python3 sync/soony_sync.py            # one sync pass
  SOONY_TOKEN=... python3 sync/soony_sync.py --watch 90 # poll every 90 s
  python3 sync/soony_sync.py --seed-zip soony-data.zip  # bootstrap from a downloaded archive
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = os.environ.get(
    "SOONY_BASE", "https://start-hack-portal-cxh3crbdaef2f8gt.westeurope-01.azurewebsites.net"
)
ROOT = Path(os.environ.get("SOONY_DATA", Path(__file__).resolve().parent.parent / "data"))
BLOBS, CURRENT = ROOT / "blobs", ROOT / "current"
MANIFEST, CHANGES = ROOT / "manifest.json", ROOT / "changes.jsonl"
MAX_ROW_EVENTS = 200  # cap per file so a reformatted CSV doesn't flood the log


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _get(url: str, token: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network blips during a hackathon are normal
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} for {url}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def safe_path(rel: str) -> Path:
    """Refuse paths that would escape data/current (archive/listing content is untrusted)."""
    p = (CURRENT / rel).resolve()
    if not str(p).startswith(str(CURRENT.resolve()) + os.sep):
        raise ValueError(f"unsafe path from portal: {rel!r}")
    return p


def store_blob(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    b = BLOBS / h
    if not b.exists():
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_bytes(content)
    return h


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(m: dict) -> None:
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
    tmp.replace(MANIFEST)


def log_event(ev: dict) -> None:
    CHANGES.parent.mkdir(parents=True, exist_ok=True)
    with CHANGES.open("a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def csv_row_diff(old: bytes, new: bytes) -> list[dict]:
    """Row-level diff keyed by the first column (shipment_id, gr_number, ...)."""
    def rows(b: bytes):
        r = csv.DictReader(io.StringIO(b.decode("utf-8-sig", errors="replace")))
        key = r.fieldnames[0] if r.fieldnames else None
        return key, {row[key]: row for row in r if key and row.get(key)}

    k_old, a = rows(old)
    k_new, b = rows(new)
    if k_old != k_new:
        return [{"kind": "schema_changed", "old_key": k_old, "new_key": k_new}]
    out = []
    for k in b.keys() - a.keys():
        out.append({"kind": "row_added", "key": k, "row": b[k]})
    for k in a.keys() - b.keys():
        out.append({"kind": "row_removed", "key": k, "row": a[k]})
    for k in a.keys() & b.keys():
        ch = {f: [a[k].get(f), b[k].get(f)] for f in set(a[k]) | set(b[k]) if a[k].get(f) != b[k].get(f)}
        if ch:
            out.append({"kind": "row_changed", "key": k, "fields": ch})
    return out


def apply_file(path: str, content: bytes, sha: str, size: int, manifest: dict, ts: str, source: str) -> dict | None:
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError(f"checksum mismatch for {path}")
    prev = manifest.get(path)
    store_blob(content)
    dst = safe_path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(content)
    if prev and prev["sha256"] == sha:
        return None
    ev = {"ts": ts, "source": source, "path": path, "sha256": sha, "bytes": size,
          "kind": "modified" if prev else "added"}
    if prev:
        ev["prev_sha256"] = prev["sha256"]
        if path.endswith(".csv"):
            old_blob = BLOBS / prev["sha256"]
            if old_blob.exists():
                diff = csv_row_diff(old_blob.read_bytes(), content)
                ev["row_changes"] = diff[:MAX_ROW_EVENTS]
                ev["row_changes_total"] = len(diff)
    manifest[path] = {"sha256": sha, "bytes": size,
                      "first_seen": prev["first_seen"] if prev else ts, "last_changed": ts}
    return ev


def sync_once(token: str) -> list[dict]:
    ts = now()
    manifest = load_manifest()
    listing = json.loads(_get(f"{BASE}/api/v1/files", token))
    remote = {f["path"]: f for f in listing["files"]}
    events = []
    for path, f in sorted(remote.items()):
        if manifest.get(path, {}).get("sha256") == f["sha256"]:
            continue  # unchanged: the whole point, no download
        url = f.get("url") or f"{BASE}/api/v1/files/{urllib.parse.quote(path)}"
        content = _get(url, token, timeout=120)
        ev = apply_file(path, content, f["sha256"], f["bytes"], manifest, ts, "api")
        if ev:
            events.append(ev)
    for path in sorted(set(manifest) - set(remote)):
        events.append({"ts": ts, "source": "api", "path": path, "kind": "removed",
                       "prev_sha256": manifest[path]["sha256"]})
        manifest.pop(path)
        try:
            safe_path(path).unlink()
        except FileNotFoundError:
            pass
    for ev in events:
        log_event(ev)
    save_manifest(manifest)
    return events


def seed_from_zip(zip_path: str) -> list[dict]:
    ts, manifest, events = now(), load_manifest(), []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            rel = name.split("/", 1)[1] if name.startswith("soony-data/") else name
            content = z.read(info)
            sha = hashlib.sha256(content).hexdigest()
            ev = apply_file(rel, content, sha, len(content), manifest, ts, "zip")
            if ev:
                events.append(ev)
    for ev in events:
        log_event(ev)
    save_manifest(manifest)
    return events


def summarize(events: list[dict]) -> str:
    if not events:
        return "no changes"
    by = {}
    for e in events:
        by.setdefault(e["kind"], []).append(e["path"])
    return "; ".join(f"{k}: {len(v)}" for k, v in by.items())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="poll forever at this interval")
    ap.add_argument("--seed-zip", metavar="ZIP", help="bootstrap from a downloaded archive.zip")
    a = ap.parse_args()
    if a.seed_zip:
        print(f"[{now()}] seed: {summarize(seed_from_zip(a.seed_zip))}")
        return
    token = os.environ.get("SOONY_TOKEN")
    if not token:
        sys.exit("set SOONY_TOKEN (team token from portal Settings)")
    while True:
        try:
            ev = sync_once(token)
            print(f"[{now()}] {summarize(ev)}", flush=True)
            for e in ev:
                print(f"   {e['kind']:9} {e['path']}" + (f"  ({e.get('row_changes_total')} row changes)" if e.get("row_changes_total") else ""), flush=True)
        except Exception as e:
            print(f"[{now()}] sync failed: {e}", file=sys.stderr, flush=True)
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
