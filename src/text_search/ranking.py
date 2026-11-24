"""Pure-Python retrieval helpers (no AWS imports, so they are easy to unit test)."""
import array
import math
import re

STOPWORDS = {
    "a", "an", "and", "the", "of", "in", "on", "at", "to", "with", "for", "from", "by", "is",
    "are", "photo", "photos", "picture", "pictures", "image", "images", "someone", "people",
}


def unpack(raw):
    """DynamoDB Binary (or bytes) holding little-endian float32 values -> array('f')."""
    data = bytes(getattr(raw, "value", raw))
    vec = array.array("f")
    vec.frombytes(data)
    return vec


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _stem(word):
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def tokens(text):
    out = []
    for w in re.findall(r"[a-z]+", text.lower()):
        if len(w) >= 3 and w not in STOPWORDS:
            out.append(_stem(w))
    return out


def keyword_score(query, labels):
    """How many distinct query words appear in the photo's label words (the baseline)."""
    label_words = {_stem(w) for label in labels for w in re.findall(r"[a-z]+", label.lower())}
    return sum(1 for t in set(tokens(query)) if t in label_words)


def rank_semantic(query_vec, items):
    """items: [{'photo_id', 'embedding'}] -> [(photo_id, cosine)] best first."""
    scored = [(i["photo_id"], cosine(query_vec, unpack(i["embedding"]))) for i in items if i.get("embedding")]
    return sorted(scored, key=lambda s: (-s[1], s[0]))


def rank_keyword(query, items):
    """Only photos with at least one matching label are returned."""
    scored = [(i["photo_id"], keyword_score(query, i.get("labels") or [])) for i in items]
    return sorted([s for s in scored if s[1] > 0], key=lambda s: (-s[1], s[0]))


def fuse(rankings, k=60, weights=None):
    """Weighted reciprocal rank fusion of several [(photo_id, score)] lists."""
    weights = weights or [1.0] * len(rankings)
    fused = {}
    for ranking, weight in zip(rankings, weights):
        for pos, (pid, _) in enumerate(ranking, start=1):
            fused[pid] = fused.get(pid, 0.0) + weight / (k + pos)
    return sorted(fused.items(), key=lambda s: (-s[1], s[0]))