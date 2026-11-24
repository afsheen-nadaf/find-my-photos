#!/usr/bin/env python3
"""LLM-as-a-Judge evaluation over eval/results.json using Gemini.

    python eval/eval_llm.py
    python eval/eval_llm.py --results eval/results.json --out eval/llm_judge_results.json
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODES = ("keyword", "semantic", "hybrid")
JUDGE_MODEL = "gemini-3.8-flash"


def load_env_key(env_path):
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("GEMINI_API_KEY not found in environment or .env")


def api_url_from_config(path):
    match = re.search(r'API_URL:\s*"([^"]+)"', Path(path).read_text(encoding="utf-8"))
    if not match:
        sys.exit(f"Could not find API_URL in {path}")
    return match.group(1).rstrip("/")


def fetch_photo_labels(api_url):
    req = urllib.request.Request(f"{api_url}/photos", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        photos = json.load(resp).get("photos", [])
    return {p["photoId"]: p.get("labels") or [] for p in photos}


def call_gemini_json(api_key, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{JUDGE_MODEL}:generateContent"
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as err:
        sys.exit(f"Gemini judge failed: HTTP {err.code} {err.read().decode()[:250]}")
    text = out["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", help="API base URL (default: read from frontend/config.js)")
    ap.add_argument("--results", default=str(here / "results.json"))
    ap.add_argument("--out", default=str(here / "llm_judge_results.json"))
    args = ap.parse_args()

    api_key = load_env_key(here.parent / ".env")
    api_url = (args.api or api_url_from_config(here.parent / "frontend" / "config.js")).rstrip("/")
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    labels_by_id = fetch_photo_labels(api_url)

    mode_scores = {m: [] for m in MODES}
    judged_queries = []

    for q_item in results["queries"]:
        query = q_item["q"]
        q_report = {"q": query, "modes": {}}

        for mode in MODES:
            top_ids = q_item[mode]["top"]
            if not top_ids:
                mode_scores[mode].append(1.0)
                q_report["modes"][mode] = {"avg_score": 1.0, "ratings": [], "note": "No results returned."}
                continue

            candidates = [
                {"rank": i, "photoId": pid, "labels": labels_by_id.get(pid, [])}
                for i, pid in enumerate(top_ids, start=1)
            ]
            prompt = (
                "You are an impartial search quality evaluator for a photo album app.\n"
                f"User search query: {query!r}\n"
                f"Retrieved top results (with auto-detected image tags): {json.dumps(candidates)}\n\n"
                "Rate each candidate's relevance to the query on a 1 to 3 scale:\n"
                "  3 = Strong/exact match based on the photo's tags\n"
                "  2 = Partial or plausible match (related context, e.g. 'person' for 'girl')\n"
                "  1 = Irrelevant or no results\n"
                "Return JSON with schema: "
                '{"ratings": [{"rank": 1, "photoId": "...", "score": 3, "reason": "..."}], "summary": "1 sentence takeaway"}'
            )
            verdict = call_gemini_json(api_key, prompt)
            ratings = verdict.get("ratings", [])
            avg = sum(r.get("score", 1) for r in ratings) / len(ratings) if ratings else 1.0
            mode_scores[mode].append(avg)
            q_report["modes"][mode] = {
                "avg_score": round(avg, 2),
                "ratings": ratings,
                "summary": verdict.get("summary", ""),
            }

        judged_queries.append(q_report)
        print(
            f"Judged {query!r:24} -> keyword={q_report['modes']['keyword']['avg_score']:.2f}, "
            f"semantic={q_report['modes']['semantic']['avg_score']:.2f}, "
            f"hybrid={q_report['modes']['hybrid']['avg_score']:.2f}"
        )

    summary = {m: round(sum(vals) / len(vals), 3) for m, vals in mode_scores.items()}

    # Generate overall error analysis from Gemini
    analysis_prompt = (
        "Summarize these LLM-as-a-Judge search evaluation results in 3 short bullet points for a student project report. "
        "Explain why semantic/hybrid beat keyword and mention any failure patterns:\n"
        f"{json.dumps({'summary_1_to_3_scale': summary, 'queries': judged_queries})}"
    )
    overall = call_gemini_json(api_key, analysis_prompt)

    print("\n=== LLM-as-a-Judge Summary (1.0 to 3.0 scale) ===")
    print("| Method | Mean Judge Relevance (1–3) |")
    print("|---|---|")
    for m in MODES:
        print(f"| {m} | {summary[m]:.2f} / 3.00 |")

    Path(args.out).write_text(
        json.dumps({"judge_model": JUDGE_MODEL, "summary": summary, "analysis": overall, "queries": judged_queries}, indent=2),
        encoding="utf-8",
    )
    print(f"\nFull LLM judge report written to {args.out}")


if __name__ == "__main__":
    main()