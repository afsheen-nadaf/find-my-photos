"""GET /photos -> every photo in the album with a signed thumbnail URL."""
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
table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
BUCKET = os.environ["BUCKET"]


def _sign(key):
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=900)


def handler(event, context):
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    items.sort(key=lambda i: i["uploaded_at"], reverse=True)
    photos = [
        {
            "photoId": i["photo_id"],
            "faceCount": int(i["face_count"]),
            "status": i["status"],
            "thumbUrl": _sign(i["thumb_key"]) if i.get("thumb_key") else None,  # None = still processing
        }
        for i in items
    ]
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"photos": photos})}