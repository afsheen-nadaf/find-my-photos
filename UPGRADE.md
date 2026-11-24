# Upgrade guide: fix the album, add search by description

## What was wrong

**Event photos not showing.** `template.yaml` had a lifecycle rule that deleted everything in the photos
bucket after 14 days, including `originals/` and `thumbs/`. DynamoDB records and Rekognition faces have no
expiry, so the album kept listing photos whose files were gone. Confirm it (takes a minute):

    ACCT=$(aws sts get-caller-identity --query Account --output text)
    aws s3 ls s3://fmp-photos-$ACCT-ap-south-1/originals/ --region ap-south-1 | head
    aws s3 ls s3://fmp-photos-$ACCT-ap-south-1/thumbs/    --region ap-south-1 | head

Empty output means the files expired. The rule is removed in this patch.

**Face count of 78.** Not a bug in the count: Rekognition indexes every face in every photo, so one
person in 10 photos adds 10. The label now says "face appearances" so it is honest. A true "6 or 7
people" number needs face clustering, which this patch does not include.

**Also fixed:** `IndexPhoto` now skips photos it has already indexed, so a repeated S3 event cannot store
every face twice in the collection.

## What was added

- `EmbedPhoto` Lambda: when a thumbnail is saved, embeds it with Bedrock Titan Multimodal Embeddings
  (384 dims) and stores Rekognition labels beside it.
- `TextSearch` Lambda and `POST /search-text`: embeds the query and ranks photos by cosine similarity.
  Modes: `semantic`, `keyword` (label baseline for the eval), `hybrid`.
- A "Describe the photo" box on the site.
- `eval/label.html` (label queries by clicking photos) and `eval/eval_search.py` (recall@k, hit@k, MRR for
  keyword vs semantic vs hybrid).
- `tests/test_upgrade.py`: 20 offline tests with fakes for AWS.

## Steps

1. Copy everything in this patch over your project folder, overwriting. It does not touch `frontend/config.js`.
2. Reset, because the old records and faces point at deleted files:

       ./teardown.sh
       ./deploy.sh

   The site URL and the API URL will both change, and `deploy.sh` rewrites `config.js` for you.
3. Open the new site and upload your 23 photos again (you need your local copies, the originals are gone).
   Thumbnails appear first, then embeddings a few seconds later.
4. Check embeddings landed:

       TABLE=$(aws cloudformation describe-stack-resource --stack-name find-my-photos \
         --logical-resource-id PhotosTable --query StackResourceDetail.PhysicalResourceId \
         --output text --region ap-south-1)
       aws dynamodb scan --table-name $TABLE --region ap-south-1 \
         --projection-expression "photo_id, labels" --max-items 3

   Every item should have a non-empty `labels` list. If not, read the EmbedPhotoFunction logs in CloudWatch.
5. Try "Describe the photo" on the site. If it errors with an access-denied message about the model, open
   Bedrock in the console, go to model access in the Mumbai region, and enable Titan Multimodal Embeddings G1.
6. Evaluate: open `eval/label.html` in your browser, add 20 to 30 queries with the photos that truly match
   each one, download `queries.json` into `eval/`, then run:

       python3 eval/eval_search.py

   Keep the table it prints. That is your resume number.

## What is not verified

The handlers, ranking, eval metrics and stream-skip logic are tested offline, and the template passes
cfn-lint. Nothing here has been run against real AWS, so the first deploy is the real test of the Bedrock
call and the stream trigger.
