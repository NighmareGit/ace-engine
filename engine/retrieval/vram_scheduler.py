"""PrepStage — dynamic VRAM load/embed/unload lifecycle manager.

Manages model lifecycle against a llama.cpp HTTP server so the subject
model on the 3070 is never VRAM-contended.  Runs OFF the critical latency
path (prep stage only — never called from the generate hot path).

Target: standalone llama.cpp server (server-vulkan Docker image on Intel
iGPU, CPU fallback to server image).  Talks HTTP via stdlib urllib — no
new dependencies.

Public interface:
    prep_stage = PrepStage(server_url="http://localhost:8080")
    prep_stage.embed_batch(model, texts)        # load → embed → unload
    prep_stage.swap_for_judge(judge, orch)      # unload orch → load judge
    handle = prep_stage.load(model)
    embeddings = prep_stage.embed(handle, texts)
    prep_stage.unload(handle)

The HttpTransport protocol is the internal seam — tests inject a mock.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Protocol


# ── Model handle ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelHandle:
    """Opaque handle to a loaded model on the llama.cpp server."""
    model_name: str
    base_url: str


# ── HTTP transport seam (mockable) ──────────────────────────────────────────

class HttpTransport(Protocol):
    """Internal seam for HTTP calls.  Production uses _StdlibTransport;
    tests inject a mock."""

    def post(self, url: str, body: bytes, timeout: float = 30.0) -> dict:
        """POST JSON to *url*, return parsed JSON response."""
        ...


class _StdlibTransport:
    """Production transport using stdlib urllib."""

    def post(self, url: str, body: bytes, timeout: float = 30.0) -> dict:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Try to read error body for diagnostics.
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"llama.cpp server returned HTTP {exc.code} for {url}: "
                f"{error_body}"
            ) from exc


# ── PrepStage ────────────────────────────────────────────────────────────────

class PrepStage:
    """VRAM lifecycle manager for the prep stage.

    Usage::

        stage = PrepStage("http://localhost:8080")
        embeddings = stage.embed_batch("bge-small-en-v1.5-Q4_K_M.gguf",
                                       ["hello world", "foo bar"])
    """

    # llama.cpp server admin endpoint for unloading a model to free VRAM.
    # Documented in ggml-org/llama.cpp tools/server/README.md —
    # "POST /models/unload: Unload a model. Payload: {"model": "<name>"}".
    # This is the llama.cpp-sanctioned mechanism for releasing VRAM without
    # restarting the server (router mode / model management API).
    _UNLOAD_ENDPOINT = "/models/unload"

    def __init__(
        self,
        server_url: str,
        transport: Optional[HttpTransport] = None,
        embedding_endpoint: str = "/v1/embeddings",
    ) -> None:
        self._base_url = server_url.rstrip("/")
        self._transport = transport or _StdlibTransport()
        self._embedding_endpoint = embedding_endpoint

    def load(self, model: str) -> ModelHandle:
        """Load *model* on the server.  Returns a handle for subsequent
        embed/unload calls.

        The llama.cpp server supports model switching via the model
        management admin API (POST /models/load) or via container restart.
        This method abstracts that choice — the transport hides the
        implementation.  For the default stdlib transport this issues a
        POST /models/load so the model is resident before embedding."""
        url = f"{self._base_url}/models/load"
        body = json.dumps({"model": model}).encode("utf-8")
        self._transport.post(url, body)
        return ModelHandle(model_name=model, base_url=self._base_url)

    def unload(self, handle: ModelHandle) -> None:
        """Unload the model referenced by *handle*, freeing VRAM.

        Uses the llama.cpp server's model management admin API:
        ``POST /models/unload`` with payload ``{"model": "<name>"}``.
        This is the llama.cpp-sanctioned mechanism for releasing model
        VRAM without restarting the server.  See [ggml-org/llama.cpp
        README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
        (section "POST /models/unload: Unload a model").

        The transport is injectable so tests can mock this call."""
        url = f"{self._base_url}{self._UNLOAD_ENDPOINT}"
        body = json.dumps({"model": handle.model_name}).encode("utf-8")
        self._transport.post(url, body)

    def embed(self, handle: ModelHandle, texts: list[str]) -> list[list[float]]:
        """Embed *texts* using the model referenced by *handle*.

        Calls the llama.cpp /v1/embeddings endpoint.  Returns a list of
        float vectors (one per input text)."""
        if not texts:
            return []
        url = f"{handle.base_url}{self._embedding_endpoint}"
        body = json.dumps({
            "model": handle.model_name,
            "input": texts,
        }).encode("utf-8")
        response = self._transport.post(url, body)
        # llama.cpp returns {"data": [{"embedding": [...]}, ...]} or
        # {"embeddings": [[...], ...]}.  Handle both shapes.
        if "data" in response:
            return [item["embedding"] for item in response["data"]]
        if "embeddings" in response:
            return [list(vec) for vec in response["embeddings"]]
        if "embedding" in response:
            return [list(response["embedding"])]
        raise RuntimeError(
            f"Unexpected response shape from {url}: "
            f"keys={list(response.keys())}"
        )

    def embed_batch(self, model: str, texts: list[str]) -> list[list[float]]:
        """High-level lifecycle: load → embed → unload.

        Fire-and-forget VRAM cleanup.  The model is loaded, all texts are
        embedded in a single batch, then the model is unloaded.  This is
        the primary entry point for the prep stage."""
        handle = self.load(model)
        try:
            return self.embed(handle, texts)
        finally:
            self.unload(handle)

    def swap_for_judge(
        self, judge_model: str, orchestrator_model: str
    ) -> ModelHandle:
        """On-demand swap: unload orchestrator → load judge.

        Returns the judge handle.  The orchestrator is unloaded first to
        free VRAM before loading the judge model."""
        orch_handle = ModelHandle(
            model_name=orchestrator_model, base_url=self._base_url)
        self.unload(orch_handle)
        return self.load(judge_model)
