"""POST /search -> find every album photo containing the face in the uploaded selfie.

The selfie is handled in memory only: it is never written to S3 or added to the collection.
"""
import base64
import binascii
import json
import os

import boto3
from botocore.config import Config

# Sign against the regional endpoint: a new bucket's global URL redirects, and browsers reject redirects on CORS preflight.
REGION = os.environ["AWS_REGION"]
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
rekognition = boto3.client("rekognition")
dynamodb = boto3.resource("dynamodb")
BUCKET = os.environ["BUCKET"]
TABLE = os.environ["TABLE"]
COLLECTION_ID = os.environ["COLLECTION_ID"]


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _sign(key, download=False):
    params = {"Bucket": BUCKET, "Key": key}
    if download:
        params["ResponseContentDisposition"] = "attachment"
    return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=900)


def handler(event, context):
    try:
        raw = (json.loads(event.get("body") or "{}")).get("image", "")
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        image_bytes = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error, IndexError):
        return _resp(400, {"error": "Send the selfie as a base64-encoded JPEG or PNG."})
    if not image_bytes:
        return _resp(400, {"error": "No image received."})

    try:
        result = rekognition.search_faces_by_image(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": image_bytes},
            FaceMatchThreshold=85,
            MaxFaces=100,
            QualityFilter="AUTO",
        )
    except (rekognition.exceptions.InvalidParameterException, rekognition.exceptions.InvalidImageFormatException):
        return _resp(200, {"matches": [], "message": "No clear face found in that selfie. Try a well-lit, front-facing photo."})

    # Keep the best similarity per photo (one photo can contain several matching faces).
    best = {}
    for m in result["FaceMatches"]:
        pid = m["Face"]["ExternalImageId"]
        best[pid] = max(best.get(pid, 0), m["Similarity"])
    if not best:
        return _resp(200, {"matches": [], "message": "You don’t appear in any photos yet."})

    got = dynamodb.batch_get_item(RequestItems={TABLE: {"Keys": [{"photo_id": p} for p in best]}})
    matches = []
    for item in got["Responses"][TABLE]:
        matches.append(
            {
                "photoId": item["photo_id"],
                "similarity": round(float(best[item["photo_id"]]), 1),
                "thumbUrl": _sign(item["thumb_key"]) if item.get("thumb_key") else _sign(item["s3_key"]),
                "downloadUrl": _sign(item["s3_key"], download=True),
            }
        )
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return _resp(200, {"matches": matches})