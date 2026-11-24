"""DynamoDB Stream trigger (MODIFY): once a photo has its thumbnail, tag and embed it."""
import json
import math
import os
import struct
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
rekognition = boto3.client("rekognition")
table = boto3.resource("dynamodb").Table(os.environ["TABLE"])

BUCKET = os.environ["BUCKET"]
MODEL_ID = os.environ.get("EMBED_MODEL_ID", "gemini-embedding-001")
DIMS = int(os.environ.get("EMBED_DIMS", "384"))
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_ID}:embedContent"


def embed_document(text):
    payload = json.dumps(
        {
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_DOCUMENT",
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
    # L2-normalize truncated Matryoshka vectors for accurate cosine similarity
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def pack(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


def detect_labels(image_bytes):
    """Label names plus their parents, lower-cased and de-duplicated, in a stable order."""
    result = rekognition.detect_labels(Image={"Bytes": image_bytes}, MaxLabels=25, MinConfidence=70)
    names = []
    for label in result["Labels"]:
        for name in [label["Name"]] + [p["Name"] for p in label.get("Parents", [])]:
            name = name.lower()
            if name not in names:
                names.append(name)
    return names


def handler(event, context):
    for record in event["Records"]:
        if record["eventName"] != "MODIFY":
            continue
        item = record["dynamodb"]["NewImage"]
        if "thumb_key" not in item or "embedding" in item:
            continue
        photo_id = item["photo_id"]["S"]
        thumb_key = item["thumb_key"]["S"]

        try:
            image = s3.get_object(Bucket=BUCKET, Key=thumb_key)["Body"].read()
        except ClientError as err:
            if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
                print(f"Skipping {photo_id}: thumbnail {thumb_key} no longer exists")
                continue
            raise

        labels = detect_labels(image)
        caption = f"Photo showing: {', '.join(labels)}" if labels else "Event photograph"
        vector = embed_document(caption)

        try:
            table.update_item(
                Key={"photo_id": photo_id},
                UpdateExpression="SET embedding = :e, embed_model = :m, labels = :l",
                ConditionExpression="attribute_exists(photo_id)",
                ExpressionAttributeValues={":e": pack(vector), ":m": MODEL_ID, ":l": labels},
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            print(f"Skipping {photo_id}: record was deleted")
            continue
        print(f"Embedded {photo_id} ({len(vector)} dims, {len(labels)} labels)")