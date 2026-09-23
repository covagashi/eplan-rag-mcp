#!/usr/bin/env python3
"""
Ingest the eplan_wiki_scraper.py output into the eplan-wiki-2027 D1 database
via the Worker's POST /admin/ingest endpoint (bound parameters -- avoids the
SQLITE_TOOBIG limit that `wrangler d1 execute --file` hits on large inlined
SQL string literals; some bundled class files run past 100KB).

Skips _index.md (folder link lists) and _symbol_index.md -- neither has
real prose worth full-text indexing.

Usage:
    python ingest.py --url https://eplan-wiki-2027.<subdomain>.workers.dev --token <INGEST_TOKEN>
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_WIKI_DIR = r"D:\3_workbench\Christian\covaga\scrapping_eplan\eplan_api_wiki_2027"
SKIP_NAMES = {"_index.md", "_symbol_index.md"}


def parse_file(path: str, rel_path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    title = ""
    m = re.match(r"^#\s+(.+?)\s*$", text, re.M)
    if m:
        title = m.group(1)
    if not title:
        title = os.path.splitext(os.path.basename(rel_path))[0]

    breadcrumb = ""
    m = re.search(r"^\*\*Path:\*\*\s*(.+?)\s*$", text, re.M)
    if m:
        breadcrumb = m.group(1)

    source_url = ""
    m = re.search(r"^\*\*Source:\*\*\s*(\S+)\s*$", text, re.M)
    if m:
        source_url = m.group(1)

    kind = "standalone"
    if rel_path.startswith("API Reference"):
        kind = "bundle"

    return {
        "path": rel_path.replace("\\", "/"),
        "title": title,
        "kind": kind,
        "breadcrumb": breadcrumb,
        "source_url": source_url,
        "content": text,
        "size": len(text.encode("utf-8")),
    }


def collect(wiki_dir: str) -> list:
    rows = []
    for root, _dirs, files in os.walk(wiki_dir):
        if root.endswith("logs"):
            continue
        for name in files:
            if not name.endswith(".md") or name in SKIP_NAMES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, wiki_dir)
            rows.append(parse_file(full, rel))
    return rows


def post_batch(url: str, token: str, rows: list, retries: int = 3):
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/admin/ingest",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # Cloudflare's default bot-fight WAF rule blocks the default
            # Python-urllib/x.y User-Agent outright (error 1010).
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) eplan-wiki-ingest/1.0",
        },
        method="POST",
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if attempt == retries:
                raise RuntimeError(f"HTTP {e.code}: {body}")
            time.sleep(2 * attempt)
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki-dir", default=DEFAULT_WIKI_DIR)
    ap.add_argument("--url", required=True, help="Worker base URL, e.g. https://eplan-wiki-2027.<sub>.workers.dev")
    ap.add_argument("--token", required=True, help="INGEST_TOKEN")
    ap.add_argument("--batch-size", type=int, default=15, help="rows per POST -- keep small, some rows are >100KB")
    ap.add_argument("--workers", type=int, default=4, help="concurrent POSTs in flight (batches are independent: INSERT OR REPLACE keyed by path)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.wiki_dir):
        print(f"ERROR: wiki dir not found: {args.wiki_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {args.wiki_dir} ...")
    rows = collect(args.wiki_dir)
    total_bytes = sum(r["size"] for r in rows)
    print(f"Found {len(rows)} files, {total_bytes / 1024 / 1024:.1f} MB total")

    if args.dry_run:
        for r in rows[:5]:
            print(f"  {r['kind']:10s} {r['path']}  ({r['size']} bytes)  title={r['title']!r}")
        return

    batches = [rows[i : i + args.batch_size] for i in range(0, len(rows), args.batch_size)]
    inserted = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(post_batch, args.url, args.token, batch) for batch in batches]
        try:
            for fut in as_completed(futures):
                inserted += fut.result().get("inserted", 0)
                elapsed = time.time() - t0
                print(f"  [{inserted}/{len(rows)}] ({elapsed:.0f}s)")
        except BaseException:
            for f in futures:
                f.cancel()
            raise

    print(f"\nDone. Inserted {inserted}/{len(rows)} rows in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
