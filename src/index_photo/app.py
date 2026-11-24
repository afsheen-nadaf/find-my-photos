"""S3 trigger (originals/): index every face in the photo, then record it in DynamoDB."""
import os
import urllib.parse
from datetime import datetime, timezone

import boto3

rekognition = boto3.client("rekognition")
table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
COLLECTION_ID = os.environ["COLLECTION_ID"]


def handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        photo_id = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        # S3 can deliver an event twice. Indexing twice would store every face twice in the collection.
        if table.get_item(Key={"photo_id": photo_id}).get("Item"):
            print(f"Skipping {key}: already indexed")
            continue

        try:
            result = rekognition.index_faces(
                CollectionId=COLLECTION_ID,
                Image={"S3Object": {"Bucket": bucket, "Name": key}},
                ExternalImageId=photo_id,  # links each face back to its photo
                MaxFaces=100,
                QualityFilter="AUTO",  # skip blurry or tiny faces
                DetectionAttributes=[],
            )
        except rekognition.exceptions.InvalidImageFormatException:
            print(f"Skipping {key}: not a valid JPEG/PNG")
            continue

        face_count = len(result["FaceRecords"])
        table.put_item(
            Item={
                "photo_id": photo_id,
                "s3_key": key,
                "face_count": face_count,
                "status": "INDEXED",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        print(f"Indexed {face_count} face(s) from {key}")
