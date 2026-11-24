"""DynamoDB Stream trigger (INSERT only): build a thumbnail for each newly indexed photo."""
import io
import os

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
BUCKET = os.environ["BUCKET"]


def handler(event, context):
    for record in event["Records"]:
        if record["eventName"] != "INSERT":
            continue
        item = record["dynamodb"]["NewImage"]
        photo_id = item["photo_id"]["S"]
        key = item["s3_key"]["S"]

        try:
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        except ClientError as err:
            if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
                # Original was deleted before we got to it: skip, don't retry forever
                print(f"Skipping {photo_id}: original {key} no longer exists")
                continue
            raise
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(body))).convert("RGB")
        img.thumbnail((480, 480))

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        thumb_key = f"thumbs/{photo_id}.jpg"
        s3.put_object(Bucket=BUCKET, Key=thumb_key, Body=buf.getvalue(), ContentType="image/jpeg")

        try:
            table.update_item(
                Key={"photo_id": photo_id},
                UpdateExpression="SET thumb_key = :t",
                ConditionExpression="attribute_exists(photo_id)",  # never resurrect a deleted record
                ExpressionAttributeValues={":t": thumb_key},
            )
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            print(f"Skipping {photo_id}: record was deleted")
            continue
        print(f"Thumbnail created for {photo_id}")