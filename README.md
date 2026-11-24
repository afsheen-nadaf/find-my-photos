# Find My Photos

Serverless event album on AWS: upload event photos, then find yourself with a selfie or search by natural-language description.
Built with AWS Lambda (Python 3.12), S3, DynamoDB (with Streams), API Gateway, Amazon Rekognition, and Google Gemini Embeddings (`gemini-embedding-001`), defined in AWS SAM.

## Handlers
| Function | Trigger | Job |
|---|---|---|
| GetUploadUrl | POST /upload-url | Generate presigned S3 upload URL |
| IndexPhoto | S3 ObjectCreated (`originals/`) | Run Rekognition `IndexFaces`, save initial record in DynamoDB |
| Thumbnail | DynamoDB Stream (INSERT) | Create 480px JPEG thumbnail with Pillow |
| EmbedPhoto | DynamoDB Stream (MODIFY, once thumbnail exists) | Extract Rekognition `DetectLabels` tags and generate 384-dim `gemini-embedding-001` (`RETRIEVAL_DOCUMENT`) vector |
| TextSearch | POST /search-text | Embed text query (`RETRIEVAL_QUERY`) and rank photos using weighted Reciprocal Rank Fusion (`hybrid`, `semantic`, or `keyword`) |
| ListPhotos | GET /photos | Return album gallery with signed thumbnail URLs |
| FindMyPhotos | POST /search | Run Rekognition `SearchFacesByImage` to return matching photos for a selfie |

## Search by Description & Evaluation
`POST /search-text` accepts `{"query": "...", "k": 12, "mode": "hybrid"}` (defaulting to weighted `hybrid` fusion combining semantic cosine similarity and Rekognition label matching).

* **Retrieval Benchmark:** Label ground-truth queries in `eval/label.html`, save to `eval/queries.json`, and run:
  ```bash
  python3 eval/eval_search.py --k 5