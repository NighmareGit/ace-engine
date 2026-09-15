"""Shared TF-IDF plan-similarity primitive for the IIL router and Ralph G12.

Extracts the ``TfidfVectorizer(ngram_range=(1,2), analyzer="word")`` +
``cosine_similarity`` primitive that the intent router already uses, so both
router and Ralph G12 ideation-collapse detection share one implementation.

Reused by:
- ``engine/intent/router.py`` (IntentRouter vectorizer — same ngram/analyzer)
- ``engine/workflows/ralph/ideation.py`` (G12 forced-diversity detection)
"""

from __future__ import annotations

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def plan_similarity(a: str, b: str) -> float:
    """TF-IDF cosine similarity over two plan texts (word 1-2grams).

    Returns a float in [0.0, 1.0]. Identical texts → 1.0; disjoint
    vocabularies → 0.0. Empty/whitespace inputs yield 0.0 (no crash).

    Uses the same vectorizer settings as the intent router
    (``TfidfVectorizer(ngram_range=(1,2), analyzer="word")``) so scores are
    comparable across routing and G12 collapse detection.
    """
    if not a or not b or not a.strip() or not b.strip():
        return 0.0
    vectorizer = TfidfVectorizer(
        lowercase=True, ngram_range=(1, 2), analyzer="word",
    )
    try:
        matrix = vectorizer.fit_transform([a, b])
    except ValueError:
        # Vocabulary is empty after tokenization (e.g. pure punctuation).
        return 0.0
    sim = cosine_similarity(matrix[0:1], matrix[1:2])[0][0]
    return float(sim)
