"""Offline tests for the embedding upgrade. Run:  python3 -I tests/test_upgrade.py

Bedrock, S3, DynamoDB and Rekognition are replaced with small fakes, so nothing here touches AWS.
These check the logic (ranking, skipping, validation, metrics), not that the real services accept
the requests: that part is only proven by a deployed smoke test.
"""
import importlib.util
import json
import os
import struct
import sys
import unittest
from pathlib import Path

from botocore.exceptions import ClientError
from boto3.dynamodb.types import Binary

ROOT = Path(__file__).resolve().parent.parent
os.environ.update(
    AWS_REGION="ap-south-1", AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
    TABLE="photos", BUCKET="bucket", EMBED_DIMS="4", EMBED_MODEL_ID="amazon.titan-embed-image-v1",
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(ROOT / "src" / "text_search"))
import ranking  # noqa: E402

text_search = load("text_search_app", ROOT / "src" / "text_search" / "app.py")
embed_photo = load("embed_photo_app", ROOT / "src" / "embed_photo" / "app.py")
eval_search = load("eval_search", ROOT / "eval" / "eval_search.py")


def pack(vec):
    return Binary(struct.pack(f"<{len(vec)}f", *vec))


class Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeBedrock:
    def __init__(self, vector):
        self.vector, self.calls = vector, []

    def invoke_model(self, modelId, body, contentType, accept):
        self.calls.append((modelId, json.loads(body)))
        return {"body": Body(json.dumps({"embedding": self.vector, "inputTextTokenCount": 3}).encode())}


class FakeS3:
    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed/{Params['Key']}?dl={'ResponseContentDisposition' in Params}"

    def get_object(self, Bucket, Key):
        return {"Body": Body(b"jpeg-bytes")}


class FakeTable:
    def __init__(self, pages=None):
        self.pages, self.updates, self.scans = pages or [], [], []

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        i = len(self.scans) - 1
        page = {"Items": self.pages[i]}
        if i < len(self.pages) - 1:
            page["LastEvaluatedKey"] = {"photo_id": "x"}
        return page

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


def photo(pid, vec=None, labels=None, thumb=True):
    item = {"photo_id": pid, "s3_key": f"originals/{pid}.jpg"}
    if thumb:
        item["thumb_key"] = f"thumbs/{pid}.jpg"
    if vec is not None:
        item["embedding"] = pack(vec)
    if labels is not None:
        item["labels"] = labels
    return item


def call(body, **extra):
    event = {"body": json.dumps(body) if not isinstance(body, str) else body}
    return text_search.handler(event, None)


class RankingTests(unittest.TestCase):
    def test_cosine_basics(self):
        self.assertAlmostEqual(ranking.cosine([1, 0], [2, 0]), 1.0)
        self.assertAlmostEqual(ranking.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(ranking.cosine([0, 0], [1, 1]), 0.0)

    def test_unpack_roundtrip_binary_and_bytes(self):
        raw = struct.pack("<4f", 1.5, -2.0, 0.0, 3.25)
        self.assertEqual(list(ranking.unpack(Binary(raw))), [1.5, -2.0, 0.0, 3.25])
        self.assertEqual(list(ranking.unpack(raw)), [1.5, -2.0, 0.0, 3.25])

    def test_keyword_score_handles_plurals_and_stopwords(self):
        labels = ["person", "dog", "animal", "pet"]
        self.assertEqual(ranking.keyword_score("photos of dogs", labels), 1)
        self.assertEqual(ranking.keyword_score("a dog and a person", labels), 2)
        self.assertEqual(ranking.keyword_score("the photo of someone", labels), 0)

    def test_rank_keyword_drops_zero_scores(self):
        items = [photo("a", labels=["cake"]), photo("b", labels=["tree"]), photo("c")]
        self.assertEqual(ranking.rank_keyword("birthday cake", items), [("a", 1)])

    def test_fuse_rewards_agreement(self):
        fused = ranking.fuse([[("a", 1), ("b", 1)], [("b", 1), ("c", 1)]])
        self.assertEqual(fused[0][0], "b")


class TextSearchTests(unittest.TestCase):
    def setUp(self):
        text_search.s3 = FakeS3()
        text_search.bedrock = FakeBedrock([1, 0, 0, 0])
        text_search.table = FakeTable(
            [[photo("A", [1, 0, 0, 0], ["dog"]), photo("B", [0, 1, 0, 0], ["tree"])],
             [photo("C", [0.9, 0.1, 0, 0], ["dog", "pet"]), photo("D", thumb=False)]]
        )

    def test_semantic_orders_by_cosine_and_paginates(self):
        out = json.loads(call({"query": "a dog", "k": 10})["body"])
        self.assertEqual([m["photoId"] for m in out["matches"]], ["A", "C", "B"])
        self.assertEqual(out["searched"], 3)
        self.assertEqual(out["pending"], 1)  # D has no embedding yet
        self.assertEqual(len(text_search.table.scans), 2)  # followed LastEvaluatedKey
        self.assertTrue(out["matches"][0]["score"] > out["matches"][1]["score"] > out["matches"][2]["score"])
        self.assertIn("dl=True", out["matches"][0]["downloadUrl"])

    def test_query_is_embedded_as_text_with_configured_dims(self):
        call({"query": "a dog"})
        model, body = text_search.bedrock.calls[0]
        self.assertEqual(model, "amazon.titan-embed-image-v1")
        self.assertEqual(body, {"inputText": "a dog", "embeddingConfig": {"outputEmbeddingLength": 4}})

    def test_k_limits_results(self):
        out = json.loads(call({"query": "a dog", "k": 2})["body"])
        self.assertEqual(len(out["matches"]), 2)

    def test_keyword_mode_does_not_call_bedrock(self):
        out = json.loads(call({"query": "dogs", "mode": "keyword"})["body"])
        self.assertEqual([m["photoId"] for m in out["matches"]], ["A", "C"])
        self.assertEqual(text_search.bedrock.calls, [])

    def test_hybrid_mode_returns_results(self):
        out = json.loads(call({"query": "dog", "mode": "hybrid"})["body"])
        self.assertEqual(out["matches"][0]["photoId"], "A")

    def test_bad_input_is_rejected_with_400(self):
        for body in ({"query": ""}, {"query": "x" * 201}, {"query": "dog", "mode": "nope"}, {"query": "dog", "k": "abc"}, "not json"):
            self.assertEqual(call(body)["statusCode"], 400, body)

    def test_nothing_embedded_yet(self):
        text_search.table = FakeTable([[photo("D", thumb=False)]])
        resp = call({"query": "dog"})
        out = json.loads(resp["body"])
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(out["matches"], [])
        self.assertEqual(out["pending"], 1)
        self.assertEqual(text_search.bedrock.calls, [])


def stream(event_name, **new_image):
    image = {"photo_id": {"S": "p1"}}
    image.update(new_image)
    return {"eventName": event_name, "dynamodb": {"NewImage": image}}


class FakeRekognition:
    def __init__(self, fail=False):
        self.fail = fail

    def detect_labels(self, Image, MaxLabels, MinConfidence):
        if self.fail:
            raise ClientError({"Error": {"Code": "InvalidImageFormatException", "Message": "bad"}}, "DetectLabels")
        return {"Labels": [
            {"Name": "Dog", "Parents": [{"Name": "Animal"}, {"Name": "Pet"}]},
            {"Name": "Animal", "Parents": []},
            {"Name": "Grass", "Parents": [{"Name": "Plant"}]},
        ]}


class EmbedPhotoTests(unittest.TestCase):
    def setUp(self):
        embed_photo.s3 = FakeS3()
        embed_photo.bedrock = FakeBedrock([0.5, 0.5, 0.5, 0.5])
        embed_photo.rekognition = FakeRekognition()
        embed_photo.table = FakeTable()

    def run_records(self, *records):
        embed_photo.handler({"Records": list(records)}, None)

    def test_skips_records_it_should_not_act_on(self):
        self.run_records(
            stream("INSERT", s3_key={"S": "originals/p1.jpg"}),                           # new photo, no thumbnail yet
            stream("MODIFY", s3_key={"S": "originals/p1.jpg"}),                           # modified, still no thumbnail
            stream("MODIFY", thumb_key={"S": "thumbs/p1.jpg"}, embedding={"B": "AAAA"}),  # our own write coming back
        )
        self.assertEqual(embed_photo.table.updates, [])
        self.assertEqual(embed_photo.bedrock.calls, [])

    def test_embeds_once_thumbnail_exists(self):
        self.run_records(stream("MODIFY", thumb_key={"S": "thumbs/p1.jpg"}))
        self.assertEqual(len(embed_photo.table.updates), 1)
        update = embed_photo.table.updates[0]
        values = update["ExpressionAttributeValues"]
        self.assertEqual(update["Key"], {"photo_id": "p1"})
        self.assertEqual(struct.unpack("<4f", values[":e"]), (0.5, 0.5, 0.5, 0.5))
        self.assertEqual(values[":l"], ["dog", "animal", "pet", "grass", "plant"])  # parents included, no duplicates
        self.assertIn("attribute_exists(photo_id)", update["ConditionExpression"])
        _, body = embed_photo.bedrock.calls[0]
        self.assertIn("inputImage", body)
        self.assertEqual(body["embeddingConfig"], {"outputEmbeddingLength": 4})

    def test_label_failure_keeps_the_embedding(self):
        embed_photo.rekognition = FakeRekognition(fail=True)
        self.run_records(stream("MODIFY", thumb_key={"S": "thumbs/p1.jpg"}))
        values = embed_photo.table.updates[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":l"], [])
        self.assertEqual(len(values[":e"]), 16)

    def test_wrong_dimension_raises_so_the_stream_retries(self):
        embed_photo.bedrock = FakeBedrock([1, 2, 3])
        with self.assertRaises(RuntimeError):
            self.run_records(stream("MODIFY", thumb_key={"S": "thumbs/p1.jpg"}))
        self.assertEqual(embed_photo.table.updates, [])


class EvalMetricTests(unittest.TestCase):
    def test_metrics(self):
        m = eval_search.metrics(["x", "r1", "y", "r2", "z"], ["r1", "r2", "r3"], 5)
        self.assertAlmostEqual(m["recall"], 2 / 3)
        self.assertEqual(m["hit"], 1.0)
        self.assertAlmostEqual(m["mrr"], 0.5)

    def test_relevant_outside_top_k_counts_for_mrr_but_not_recall(self):
        m = eval_search.metrics(["a", "b", "c", "r1"], ["r1"], 3)
        self.assertEqual((m["recall"], m["hit"]), (0.0, 0.0))
        self.assertAlmostEqual(m["mrr"], 0.25)

    def test_nothing_returned(self):
        self.assertEqual(eval_search.metrics([], ["r1"], 5), {"recall": 0.0, "hit": 0.0, "mrr": 0.0})

    def test_average(self):
        avg = eval_search.average([{"recall": 1, "hit": 1, "mrr": 1}, {"recall": 0, "hit": 0, "mrr": 0}])
        self.assertEqual(avg, {"recall": 0.5, "hit": 0.5, "mrr": 0.5})


if __name__ == "__main__":
    unittest.main(verbosity=2)
