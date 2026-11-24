"""POST /search-text -> find album photos that match a text description."""
import json
import math
import os
import urllib.error
import urllib.request

import boto3
from botocore.config import Config

import ranking

REGION = os.environ["AWS_REGION"]
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
BUCKET = os.environ["BUCKET"]
MODEL_ID = os.environ.get("EMBED_MODEL_ID", "gemini-embedding-001")
DIMS = int(os.environ.get("EMBED_DIMS", "384"))
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_ID}:embedContent"
MODES = ("semantic", "keyword", "hybrid")
MAX_QUERY_CHARS = 200


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _sign(key, download=False):
    params = {"Bucket": BUCKET, "Key": key}
    if download:
        params["ResponseContentDisposition"] = "attachment"
    return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=900)


def embed_query(text):
    payload = json.dumps(
        {
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": DIMS,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL,
        data=payload,
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {err.code}: {detail}") from err

    vec = out["embedding"]["values"]
    if len(vec) != DIMS:
        raise RuntimeError(f"Expected {DIMS}-dim embedding, got {len(vec)}")
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def load_items():
    items, kwargs = [], {
        "ProjectionExpression": "#p, #s, #t, #e, #l",
        "ExpressionAttributeNames": {"#p": "photo_id", "#s": "s3_key", "#t": "thumb_key", "#e": "embedding", "#l": "labels"},
    }
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handler(event, context):
    try:
        data = json.loads(event.get("body") or "{}")
        query = str(data.get("query", "")).strip()
        k = int(data.get("k", 12))
    except (ValueError, TypeError):
        return _resp(400, {"error": "Send JSON like {\"query\": \"a birthday cake\"}."})
    mode = data.get("mode", "hybrid")
    if not query:
        return _resp(400, {"error": "Type what you are looking for first."})
    if len(query) > MAX_QUERY_CHARS:
        return _resp(400, {"error": f"Keep the description under {MAX_QUERY_CHARS} characters."})
    if mode not in MODES:
        return _resp(400, {"error": f"mode must be one of {', '.join(MODES)}."})
    k = max(1, min(k, 50))

    items = load_items()
    by_id = {i["photo_id"]: i for i in items}
    embedded = [i for i in items if i.get("embedding")]
    pending = len(items) - len(embedded)

    if mode in ("semantic", "hybrid") and not embedded:
        msg = "No photos are searchable yet. Embeddings appear a few seconds after the thumbnails."
        return _resp(200, {"mode": mode, "query": query, "matches": [], "searched": 0, "pending": pending, "message": msg})

    ranked_semantic = ranking.rank_semantic(embed_query(query), embedded) if mode in ("semantic", "hybrid") else []
    ranked_keyword = ranking.rank_keyword(query, items) if mode in ("keyword", "hybrid") else []
    if mode == "semantic":
        ranked = ranked_semantic
    elif mode == "keyword":
        ranked = ranked_keyword
    else:
        ranked = ranking.fuse([ranked_semantic, ranked_keyword], weights=(2.5, 1.0))

    matches = []
    for pid, score in ranked[:k]:
        item = by_id[pid]
        thumb = item.get("thumb_key") or item["s3_key"]
        matches.append(
            {
                "photoId": pid,
                "score": round(float(score), 4),
                "thumbUrl": _sign(thumb),
                "downloadUrl": _sign(item["s3_key"], download=True),
                "labels": list(item.get("labels") or [])[:8],
            }
        )
    return _resp(200, {"mode": mode, "query": query, "matches": matches, "searched": len(embedded), "pending": pending})