"""IIL mechanical pre-router — TF-IDF cosine over multi-example route tables.

Implements the spike S2 v3 design (FROZEN): for each route we keep several
example utterances (7/route, from the spike). Routing = argmax over per-route
MAX similarity (the multi-example max-sim that unlocked PASS — single-description
cosine failed at 81%). An intent is flagged UNCERTAIN when the top score is
below ``threshold`` OR the top1-top2 gap is below ``gap``; uncertain intents
are escalated to the native lane (the ~6% edge → ~99.7% end-to-end).

Defaults (from the spike operating point): threshold=0.50, gap=0.04.
Latency target: <1ms p50 on the TF-IDF path (spike measured 0.4ms).

Pure sklearn (TfidfVectorizer + cosine_similarity) — already a project dep.
No network, no LLM in the routing path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from engine.intent.types import Route

# Default operating point — FROZEN from spike S2 v3.
DEFAULT_THRESHOLD = 0.50
DEFAULT_GAP = 0.04

# Route-table search path: an explicit path wins, then the package `routes/`,
# then a project-level `engine/intent/routes/` dir.
ROUTES_DIR = Path(__file__).resolve().parent / "routes"


@dataclass
class RouterConfig:
    """Tunable router parameters (spec §signal table — edit with evidence)."""
    threshold: float = DEFAULT_THRESHOLD
    gap: float = DEFAULT_GAP
    route_table: str = "ace_actions"


class IntentRouter:
    """TF-IDF pre-router over a JSON route table.

    The vectorizer is fitted once per route table (cached). ``route()`` runs
    in sub-millisecond time on the common case (spike: 0.4ms p50).
    """

    def __init__(self, config: RouterConfig | None = None,
                 routes_dir: str | os.PathLike | None = None) -> None:
        self.config = config or RouterConfig()
        self._routes_dir = Path(routes_dir) if routes_dir else ROUTES_DIR
        self._table: dict | None = None
        self._vectorizer: TfidfVectorizer | None = None
        # Per-action corpus lists (action -> list of example strings).
        self._action_texts: dict[str, list[str]] = {}
        # Flat corpus + action label alignment for vectorized scoring.
        self._corpus: list[str] = []
        self._corpus_actions: list[str] = []
        self._matrix = None  # type: ignore[var-annotated]
        self._load_and_fit()

    # -- table loading -------------------------------------------------------

    def _load_table(self) -> dict:
        """Load the route table JSON by name."""
        path = self._routes_dir / f"{self.config.route_table}.json"
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _load_and_fit(self) -> None:
        """Load the route table and fit the TF-IDF matrix."""
        table = self._load_table()
        self._table = table
        routes = table["routes"]
        # Override threshold/gap from the table if present (table is authoritative).
        table_threshold = table.get("threshold")
        table_gap = table.get("gap")
        if table_threshold is not None:
            self.config.threshold = float(table_threshold)
        if table_gap is not None:
            self.config.gap = float(table_gap)

        # Build a flat corpus: one entry per (action, example).
        corpus: list[str] = []
        actions: list[str] = []
        action_texts: dict[str, list[str]] = {}
        for action, meta in routes.items():
            examples = meta.get("examples") or []
            if not examples:
                # Fall back to the description if no examples are given.
                examples = [meta.get("description", action)]
            action_texts[action] = examples
            for ex in examples:
                corpus.append(ex)
                actions.append(action)
        self._action_texts = action_texts
        self._corpus = corpus
        self._corpus_actions = actions

        # Fit TF-IDF once. ngram 1-2 matched the spike baseline.
        self._vectorizer = TfidfVectorizer(
            lowercase=True, ngram_range=(1, 2), analyzer="word",
        )
        self._matrix = self._vectorizer.fit_transform(corpus)

    # -- public API ----------------------------------------------------------

    @property
    def actions(self) -> list[str]:
        """List of known action names (registry contract)."""
        return sorted(self._action_texts.keys()) if self._action_texts else []

    def route(self, text: str) -> Route:
        """Route an utterance to an action (or flag uncertain).

        Returns a Route with confidence + uncertain flag. O(1) after fit:
        one transform + one cosine call (sub-ms).
        """
        t0 = time.perf_counter()
        if not text or not text.strip() or self._vectorizer is None:
            return Route(action="", confidence=0.0, uncertain=True,
                         latency_ms=self._elapsed(t0), route_table=self.config.route_table)

        q = self._vectorizer.transform([text])
        sims = cosine_similarity(q, self._matrix)[0]  # shape (n_examples,)

        # Per-action MAX similarity (multi-example max-sim, spike v3).
        per_action: dict[str, float] = {}
        for action, score in zip(self._corpus_actions, sims):
            if score > per_action.get(action, -1.0):
                per_action[action] = float(score)

        if not per_action:
            return Route(action="", confidence=0.0, uncertain=True,
                         latency_ms=self._elapsed(t0), route_table=self.config.route_table)

        # Sort actions by score descending.
        ranked = sorted(per_action.items(), key=lambda kv: kv[1], reverse=True)
        top_action, top_score = ranked[0]
        runner_up_action = ranked[1][0] if len(ranked) > 1 else None
        runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

        gap_ok = (top_score - runner_up_score) >= self.config.gap
        score_ok = top_score >= self.config.threshold
        uncertain = not (score_ok and gap_ok)

        return Route(
            action=top_action,
            confidence=round(top_score, 4),
            runner_up_action=runner_up_action,
            runner_up_confidence=round(runner_up_score, 4),
            uncertain=uncertain,
            latency_ms=self._elapsed(t0),
            route_table=self.config.route_table,
        )

    # -- helpers -------------------------------------------------------------

    def _elapsed(self, t0: float) -> float:
        return round((time.perf_counter() - t0) * 1000.0, 3)


def load_default_router(table: str = "ace_actions") -> IntentRouter:
    """Factory: load the default ACE action router."""
    return IntentRouter(config=RouterConfig(route_table=table))
