"""POST /upload-url -> presigned S3 PUT URL so the browser uploads straight to S3."""
import json
import os
import uuid

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
BUCKET = os.environ["BUCKET"]
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}  # formats Rekognition accepts


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    try:
        data = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "Request body must be valid JSON."})

    content_type = data.get("contentType", "")
    if content_type not in EXTENSIONS:
        return _resp(400, {"error": "Only JPEG and PNG images are supported."})

    photo_id = uuid.uuid4().hex  # also used as the Rekognition ExternalImageId
    key = f"originals/{photo_id}.{EXTENSIONS[content_type]}"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=300,
    )
    return _resp(200, {"photoId": photo_id, "uploadUrl": url})