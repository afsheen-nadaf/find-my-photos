#!/usr/bin/env python3
"""Evaluate text search: embeddings vs the Rekognition-label keyword baseline.

    python3 eval/eval_search.py                       # reads API_URL from frontend/config.js
    python3 eval/eval_search.py --queries eval/queries.json --k 5

queries.json is what eval/label.html produces:
    {"queries": [{"q": "a birthday cake", "relevant": ["<photoId>", ...]}, ...]}

Metrics (per query, then averaged):
    recall@k  share of the relevant photos that appear in the top k
    hit@k     1 if at least one relevant photo is in the top k, else 0
    MRR       1 / rank of the first relevant photo (0 if none is returned)
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODES = ("keyword", "semantic", "hybrid")


def metrics(ranked_ids, relevant, k):
    """ranked_ids: photo ids best first. relevant: iterable of relevant ids (non-empty)."""
    relevant = set(relevant)
    top = ranked_ids[:k]
    found = [pid for pid in top if pid in relevant]
    first = next((i for i, pid in enumerate(ranked_ids, start=1) if pid in relevant), None)
    return {
        "recall": len(set(found)) / len(relevant),
        "hit": 1.0 if found else 0.0,
        "mrr": 1.0 / first if first else 0.0,
    }


def average(rows):
    return {key: sum(r[key] for r in rows) / len(rows) for key in ("recall", "hit", "mrr")}


def api_url_from_config(path):
    text = Path(path).read_text()
    match = re.search(r'API_URL:\s*"([^"]+)"', text)
    if not match:
        sys.exit(f"Could not find API_URL in {path}. Pass --api https://...")
    return match.group(1).rstrip("/")


def search(api, query, mode, k):
    req = urllib.request.Request(
        f"{api}/search-text",
        data=json.dumps({"query": query, "mode": mode, "k": k}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        sys.exit(f"{mode} search failed for {query!r}: HTTP {err.code} {err.read().decode()[:200]}")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", help="API base URL (default: read from frontend/config.js)")
    ap.add_argument("--queries", default=str(here / "queries.json"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=str(here / "results.json"))
    args = ap.parse_args()

    api = (args.api or api_url_from_config(here.parent / "frontend" / "config.js")).rstrip("/")
    queries = json.loads(Path(args.queries).read_text())["queries"]
    queries = [q for q in queries if q.get("q") and q.get("relevant")]
    if not queries:
        sys.exit("No usable queries (each needs text and at least one relevant photo).")

    per_mode = {m: [] for m in MODES}
    detail = []
    for item in queries:
        row = {"q": item["q"], "relevant": item["relevant"]}
        for mode in MODES:
            ids = [m["photoId"] for m in search(api, item["q"], mode, max(args.k, 20))["matches"]]
            scores = metrics(ids, item["relevant"], args.k)
            per_mode[mode].append(scores)
            row[mode] = {"top": ids[: args.k], **scores}
        detail.append(row)

    k = args.k
    print(f"\n{len(queries)} queries, k={k}\n")
    print(f"| Method | recall@{k} | hit@{k} | MRR |")
    print("|---|---|---|---|")
    summary = {}
    for mode in MODES:
        avg = average(per_mode[mode])
        summary[mode] = avg
        print(f"| {mode} | {avg['recall']:.2f} | {avg['hit']:.2f} | {avg['mrr']:.2f} |")

    Path(args.out).write_text(json.dumps({"k": k, "n_queries": len(queries), "summary": summary, "queries": detail}, indent=2))
    print(f"\nPer-query detail written to {args.out}")


if __name__ == "__main__":
    main()
