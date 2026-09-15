"""Tests for engine.intent.similarity — plan_similarity TF-IDF cosine."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from engine.intent.similarity import plan_similarity


class TestPlanSimilarity:
    def test_identical_texts(self):
        """Identical plans → 1.0."""
        assert plan_similarity("implement a cache layer", "implement a cache layer") == pytest.approx(1.0)

    def test_completely_different(self):
        """Disjoint vocabularies → ~0.0."""
        assert plan_similarity("implement a cache layer", "refactor database schema") == pytest.approx(0.0, abs=0.01)

    def test_partial_overlap(self):
        """Partial overlap → between 0 and 1."""
        sim = plan_similarity("implement a cache layer for redis", "implement a cache layer for memcached")
        assert 0.0 < sim < 1.0

    def test_empty_inputs(self):
        """Empty strings → 0.0, no crash."""
        assert plan_similarity("", "") == 0.0
        assert plan_similarity("some text", "") == 0.0
        assert plan_similarity("", "some text") == 0.0

    def test_whitespace_only(self):
        """Whitespace-only inputs → 0.0."""
        assert plan_similarity("   ", "\t\n") == 0.0

    def test_punctuation_only(self):
        """Pure punctuation (empty vocabulary) → 0.0, no crash."""
        assert plan_similarity("!!!", "???") == 0.0

    def test_symmetry(self):
        """Similarity is symmetric: sim(a,b) == sim(b,a)."""
        a = "add retry logic to the http client"
        b = "add retry logic to the database pool"
        assert plan_similarity(a, b) == pytest.approx(plan_similarity(b, a))

    def test_g12_threshold_zone(self):
        """Plans that are semantically similar land above the 0.75 G12 threshold."""
        a = "implement a Least Recently Used cache with a configurable eviction policy"
        b = "implement a Least Recently Used cache with a configurable eviction threshold"
        assert plan_similarity(a, b) >= 0.75

    def test_g12_dissimilar_below_threshold(self):
        """Dissimilar plans land below the 0.75 G12 threshold."""
        a = "implement a Least Recently Used cache with a configurable eviction policy"
        b = "add comprehensive unit tests for the authentication middleware"
        assert plan_similarity(a, b) < 0.75
