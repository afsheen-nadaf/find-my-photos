# Find My Photos

Serverless event album on AWS: upload event photos, then find yourself with a selfie.
Lambda (Python 3.12) + S3 + DynamoDB + API Gateway + Rekognition, defined in AWS SAM.

## Handlers
| Function | Trigger | Job |
|---|---|---|
| GetUploadUrl | POST /upload-url | Presigned S3 upload URL |
| IndexPhoto | S3 ObjectCreated (originals/) | Rekognition IndexFaces, save record |
| Thumbnail | DynamoDB Stream (INSERT) | Create thumbnail with Pillow |
| ListPhotos | GET /photos | Gallery with signed thumbnail URLs |
| FindMyPhotos | POST /search | SearchFacesByImage, return matching photos |
| EmbedPhoto | DynamoDB Stream (MODIFY, once the thumbnail exists) | Titan multimodal embedding + Rekognition labels, stored on the record |
| TextSearch | POST /search-text | Embed the text query, rank photos by cosine similarity (`semantic`, `keyword` baseline or `hybrid`) |

## Search by description and evaluation
`POST /search-text` takes `{"query": "...", "k": 12, "mode": "semantic"}`. To measure it, label queries in
`eval/label.html`, save `eval/queries.json`, then run `python3 eval/eval_search.py`: it compares embeddings
with a Rekognition-label keyword baseline on recall@k, hit@k and MRR. Offline tests: `python3 -I tests/test_upgrade.py`.
See UPGRADE.md for the redeploy steps.

## Deploy
Prereqs: AWS CLI (configured, in a region where Rekognition is available), SAM CLI, Docker.

    ./deploy.sh            # build, deploy, upload frontend, prints the site URL

No Docker? Install Lambda-compatible Pillow into the function folder, then build without the container:

    rm src/thumbnail/requirements.txt
    pip install --platform manylinux2014_x86_64 --target src/thumbnail \
        --implementation cp --python-version 3.12 --only-binary=:all: Pillow
    sam build   # then run the sam deploy / frontend steps from deploy.sh

## Tear down (do this after recording)
    ./teardown.sh          # empties buckets, deletes the stack and the face collection

## Notes
- Set an AWS Budget alarm (for example, $5) before you start.
- Skip reserved concurrency on a new account; its low quota can reject the setting.
- Only use photos of people who agreed to be in the demo.
