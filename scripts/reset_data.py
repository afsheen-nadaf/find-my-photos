#!/usr/bin/env python3
"""Clear the album data but keep the stack, so the site and API URLs stay the same.

Deletes: every DynamoDB photo record, every face in the Rekognition collection, and any files left
under originals/ and thumbs/ in the photos bucket. Does NOT delete the stack, buckets or functions.

    python scripts/reset_data.py            # dry run: shows what it found
    python scripts/reset_data.py --yes      # actually deletes

Credentials come from the usual places (AWS_PROFILE, --profile, the default profile).
"""
import argparse
import sys


def physical_id(cfn, stack, logical_id):
    detail = cfn.describe_stack_resource(StackName=stack, LogicalResourceId=logical_id)["StackResourceDetail"]
    return detail["PhysicalResourceId"]


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def photo_ids(table):
    ids, kwargs = [], {"ProjectionExpression": "photo_id"}
    while True:
        page = table.scan(**kwargs)
        ids += [item["photo_id"] for item in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return ids
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def face_ids(rek, collection):
    ids = []
    for page in rek.get_paginator("list_faces").paginate(CollectionId=collection):
        ids += [face["FaceId"] for face in page["Faces"]]
    return ids


def s3_keys(s3, bucket, prefixes=("originals/", "thumbs/")):
    keys = []
    for prefix in prefixes:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys += [obj["Key"] for obj in page.get("Contents", [])]
    return keys


def reset(cfn, ddb, rek, s3, stack, delete):
    """Returns {'records': n, 'faces': n, 'files': n} found (and removed when delete is True)."""
    table = ddb.Table(physical_id(cfn, stack, "PhotosTable"))
    collection = physical_id(cfn, stack, "FaceCollection")
    bucket = physical_id(cfn, stack, "PhotosBucket")

    ids, faces, keys = photo_ids(table), face_ids(rek, collection), s3_keys(s3, bucket)
    if delete:
        with table.batch_writer() as batch:
            for pid in ids:
                batch.delete_item(Key={"photo_id": pid})
        for group in chunks(faces, 4096):
            rek.delete_faces(CollectionId=collection, FaceIds=group)
        for group in chunks(keys, 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in group], "Quiet": True})
    return {"records": len(ids), "faces": len(faces), "files": len(keys)}


def main():
    import boto3

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="find-my-photos")
    ap.add_argument("--region")
    ap.add_argument("--profile")
    ap.add_argument("--yes", action="store_true", help="really delete (default is a dry run)")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if not session.region_name:
        sys.exit("No region found. Pass --region ap-south-1.")
    account = session.client("sts").get_caller_identity()["Account"]
    print(f"Account {account}, region {session.region_name}, stack {args.stack}")

    clients = (session.client("cloudformation"), session.resource("dynamodb"), session.client("rekognition"), session.client("s3"))
    found = reset(*clients, args.stack, delete=False)
    print(f"Found {found['records']} photo records, {found['faces']} faces, {found['files']} files in S3.")
    if not args.yes:
        print("Dry run only. Run again with --yes to delete these.")
        return

    reset(*clients, args.stack, delete=True)
    left = reset(*clients, args.stack, delete=False)
    print(f"Done. Left: {left['records']} records, {left['faces']} faces, {left['files']} files.")
    if any(left.values()):
        sys.exit("Something is left over. Run the script again.")


if __name__ == "__main__":
    main()
