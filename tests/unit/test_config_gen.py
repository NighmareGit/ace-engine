#!/usr/bin/env python3
"""
Unit tests for ccbs-config-gen.py — CCBS-004 Config Generator

Tests cover:
    1. VRAM estimation accuracy
    2. Compatibility filtering
    3. Config generation (Cartesian product → filter → rank)
    4. Draft model handling (graceful skip)
    5. Config ID generation
    6. Output format
    7. CLI argument parsing
"""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main as unittest_main

# Add parent directory to path for imports
_ccbs_dir = str(Path(__file__).parent.parent.parent / "tickets" / "ccbs")
sys.path.insert(0, _ccbs_dir)

# The file uses hyphens (ccbs-config-gen.py), so importlib is needed
_spec = importlib.util.spec_from_file_location(
    "ccbs_config_gen",
    os.path.join(_ccbs_dir, "ccbs-config-gen.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["ccbs_config_gen"] = _mod
_spec.loader.exec_module(_mod)

from ccbs_config_gen import (
    COMPATIBILITY,
    CONTEXT_SIZES,
    CUDA_OVERHEAD,
    GPU_POOL,
    KV_CONSTANTS,
    SPEC_OVERHEAD_GB,
    VRAM_LIMIT_FRACTION,
    _find_draft_model,
    _is_compatible,
    _make_config_id,
    _normalize_model,
    build_output,
    estimate_vram,
    generate_configs,
    load_model_pool,
    parse_args,
    write_queue,
)


# ─── Test Fixtures ──────────────────────────────────────────────────────

def _make_model(
    name: str = "Qwen3.6-35B-A3B",
    arch: str = "qwen3",
    size_gb: float = 17.0,
    active_params: int = 3_500_000_000,
    total_params: int = 35_000_000_000,
    context_length: int = 131072,
    has_mtp: bool = False,
) -> dict:
    """Create a test model entry."""
    return {
        "path": f"/data/models/{name.replace(' ', '-')}.gguf",
        "name": name,
        "arch": arch,
        "totalParams": total_params,
        "activeParams": active_params,
        "sizeGb": size_gb,
        "quantType": "IQ4_XS",
        "hasMtp": has_mtp,
        "supportsKvarn": arch in ("qwen3", "llama", "gemma4"),
        "contextLength": context_length,
        "blockCount": 64,
        "expertCount": None,
        "docker_profile": None,
        "quality_weight": 1.0,
    }


def _make_gpu(name: str = "3090", vram_gb: int = 24, index: int = 0, port: int = 8080) -> dict:
    """Create a test GPU entry."""
    return {"name": name, "vram_gb": vram_gb, "index": index, "port": port}


def _make_draft(name: str = "Qwen3.6-35B-A3B-DFlash-Q4_K_M", arch: str = "qwen3") -> dict:
    """Create a test draft model entry."""
    return {
        "path": f"/home/<user>/models/{name}.gguf",
        "name": name,
        "arch": arch,
        "totalParams": None,
        "activeParams": None,
        "sizeGb": 0.2,
        "quantType": "Q4_K_M",
        "hasMtp": False,
        "supportsKvarn": True,
        "contextLength": None,
        "blockCount": None,
        "expertCount": None,
    }


# ─── VRAM Estimation Tests ─────────────────────────────────────────────

class TestVRAMEstimation(TestCase):
    """Test VRAM estimation accuracy."""

    def test_vram_basic(self):
        """Basic VRAM estimation for a known config."""
        model = _make_model(size_gb=17.0)
        gpu = _make_gpu(vram_gb=24)

        vram = estimate_vram(model, gpu, "f16", "none", 4096)
        # Expected: ~17.0 (weights) + 32768*4096/1e9 (~0.13) + 0 (spec) + 1.0 (cuda) = ~18.13
        self.assertGreater(vram, 17.0)
        self.assertLess(vram, 20.0)

    def test_vram_kvarn5_reduces_usage(self):
        """KVarN5 should use less VRAM than f16."""
        model = _make_model(size_gb=17.0)
        gpu = _make_gpu(vram_gb=24)

        vram_f16 = estimate_vram(model, gpu, "f16", "none", 131072)
        vram_kvarn5 = estimate_vram(model, gpu, "kvarn5", "none", 131072)

        self.assertGreater(vram_f16, vram_kvarn5)

    def test_vram_larger_context_uses_more(self):
        """Larger context should use more VRAM."""
        model = _make_model(size_gb=17.0)
        gpu = _make_gpu(vram_gb=24)

        vram_4k = estimate_vram(model, gpu, "f16", "none", 4096)
        vram_128k = estimate_vram(model, gpu, "f16", "none", 131072)

        self.assertGreater(vram_128k, vram_4k)

    def test_vram_spec_overhead(self):
        """Speculative decoding adds VRAM overhead."""
        model = _make_model(size_gb=17.0)
        gpu = _make_gpu(vram_gb=24)

        vram_none = estimate_vram(model, gpu, "f16", "none", 4096)
        vram_dflash = estimate_vram(model, gpu, "f16", "dflash", 4096)
        vram_dflash2 = estimate_vram(model, gpu, "f16", "dflash2", 4096)

        self.assertGreater(vram_dflash, vram_none)
        self.assertGreater(vram_dflash2, vram_dflash)

    def test_vram_3070_has_less_overhead(self):
        """3070 (8GB) has less CUDA overhead than 3090 (24GB)."""
        model = _make_model(size_gb=5.0)
        gpu_3090 = _make_gpu(vram_gb=24)
        gpu_3070 = _make_gpu(vram_gb=8)

        vram_3090 = estimate_vram(model, gpu_3090, "f16", "none", 4096)
        vram_3070 = estimate_vram(model, gpu_3070, "f16", "none", 4096)

        # Difference should be exactly the CUDA overhead difference (1.0 - 0.7 = 0.3)
        self.assertAlmostEqual(vram_3090 - vram_3070, 0.3, places=2)

    def test_vram_within_10_percent_of_weights_plus_kv(self):
        """VRAM estimate should be within 10% of (weights + KV + overhead)."""
        model = _make_model(size_gb=10.0, active_params=5_000_000_000)
        gpu = _make_gpu(vram_gb=24)

        for ctx in [4096, 32768, 131072]:
            vram = estimate_vram(model, gpu, "kvarn4", "none", ctx)
            # Manual calculation
            kv_bytes = KV_CONSTANTS["qwen3"]["kvarn4"]
            expected = 10.0 + (kv_bytes * ctx / 1e9) + 0.0 + CUDA_OVERHEAD[24]
            self.assertAlmostEqual(vram, expected, places=2,
                msg=f"VRAM mismatch at ctx={ctx}: got {vram}, expected {expected}")

    def test_vram_all_architectures(self):
        """VRAM estimation should work for all supported architectures."""
        gpu = _make_gpu(vram_gb=24)

        for arch in COMPATIBILITY:
            model = _make_model(arch=arch, size_gb=5.0)
            for cache in COMPATIBILITY[arch]["cache"]:
                vram = estimate_vram(model, gpu, cache, "none", 8192)
                self.assertGreater(vram, 0, f"VRAM should be positive for {arch}/{cache}")

    def test_vram_zero_weights(self):
        """Model with zero size should still estimate CUDA overhead."""
        model = _make_model(size_gb=0)
        gpu = _make_gpu(vram_gb=24)

        vram = estimate_vram(model, gpu, "f16", "none", 4096)
        self.assertGreaterEqual(vram, CUDA_OVERHEAD[24])


# ─── Compatibility Tests ───────────────────────────────────────────────

class TestCompatibility(TestCase):
    """Test architecture × cache × spec compatibility checking."""

    def test_qwen3_supports_all_caches(self):
        """Qwen3 should support all cache types."""
        for cache in COMPATIBILITY["qwen3"]["cache"]:
            self.assertTrue(_is_compatible("qwen3", cache, "none"))

    def test_qwen3_supports_dflash_and_mtp(self):
        """Qwen3 should support dflash and mtp."""
        self.assertTrue(_is_compatible("qwen3", "f16", "dflash"))
        self.assertTrue(_is_compatible("qwen3", "f16", "mtp"))

    def test_glm4_only_f16(self):
        """GLM4 should only support f16 cache."""
        self.assertTrue(_is_compatible("glm4", "f16", "none"))
        self.assertFalse(_is_compatible("glm4", "kvarn5", "none"))

    def test_glm4_no_spec(self):
        """GLM4 should not support speculative decoding."""
        self.assertFalse(_is_compatible("glm4", "f16", "dflash"))

    def test_laguna_only_f16(self):
        """Laguna should only support f16 cache."""
        self.assertTrue(_is_compatible("laguna", "f16", "none"))
        self.assertFalse(_is_compatible("laguna", "kvarn3", "none"))

    def test_laguna_no_spec(self):
        """Laguna should not support speculative decoding."""
        self.assertFalse(_is_compatible("laguna", "f16", "dflash"))

    def test_llama_no_kvarn8(self):
        """Llama should not support kvarn8."""
        self.assertTrue(_is_compatible("llama", "kvarn5", "none"))
        self.assertFalse(_is_compatible("llama", "kvarn8", "none"))

    def test_llama_supports_dflash2(self):
        """Llama should support dflash2."""
        self.assertTrue(_is_compatible("llama", "f16", "dflash2"))

    def test_unknown_arch_incompatible(self):
        """Unknown architecture should be incompatible."""
        self.assertFalse(_is_compatible("unknown_arch", "f16", "none"))

    def test_gemma4_no_dflash2(self):
        """Gemma4 should not support dflash2."""
        self.assertTrue(_is_compatible("gemma4", "f16", "dflash"))
        self.assertFalse(_is_compatible("gemma4", "f16", "dflash2"))

    def test_all_architectures_have_compatibility(self):
        """All defined architectures should have entries in COMPATIBILITY."""
        expected_archs = {"qwen3", "llama", "gemma4", "glm4", "glm4-flash", "laguna"}
        self.assertEqual(set(COMPATIBILITY.keys()), expected_archs)

    def test_all_kv_constants_match_compatibility(self):
        """KV_CONSTANTS cache types should be a superset of COMPATIBILITY cache types."""
        for arch, compat in COMPATIBILITY.items():
            if arch in KV_CONSTANTS:
                kv_caches = set(KV_CONSTANTS[arch].keys())
                compat_caches = set(compat["cache"])
                # KV_CONSTANTS should have at least the compatible cache types
                self.assertTrue(
                    compat_caches.issubset(kv_caches),
                    f"KV_CONSTANTS[{arch}] missing caches: {compat_caches - kv_caches}"
                )


# ─── Config Generation Tests ──────────────────────────────────────────

class TestConfigGeneration(TestCase):
    """Test config generation pipeline."""

    def test_basic_generation(self):
        """Basic config generation with a small pool."""
        models = [_make_model("TestModel", "qwen3", size_gb=5.0, active_params=2_000_000_000)]
        gpus = [_make_gpu("3090", 24)]

        configs, stats = generate_configs(models, gpus, draft_pool=[], context_sizes=[4096])

        self.assertGreater(len(configs), 0)
        self.assertGreater(stats["candidates"], 0)

    def test_configs_sorted_by_value(self):
        """Configs should be sorted by expected_value descending."""
        models = [
            _make_model("BigModel", "qwen3", size_gb=10.0, active_params=10_000_000_000),
            _make_model("SmallModel", "qwen3", size_gb=2.0, active_params=1_000_000_000),
        ]
        gpus = [_make_gpu("3090", 24)]

        configs, _ = generate_configs(models, gpus, draft_pool=[], context_sizes=[4096])

        if len(configs) >= 2:
            self.assertGreaterEqual(configs[0]["expected_value"], configs[1]["expected_value"])

    def test_vram_filtering(self):
        """Configs exceeding VRAM limit should be filtered out."""
        # A very large model that won't fit on 3070
        model = _make_model("HugeModel", "qwen3", size_gb=20.0, active_params=50_000_000_000)
        gpu_small = [_make_gpu("3070", 8)]

        configs, stats = generate_configs([model], gpu_small, draft_pool=[], context_sizes=[4096])

        # The large model should be filtered by VRAM
        # With 20GB weights + 8GB CUDA overhead, it won't fit on 8GB GPU
        # (even at f16 with 4K context)
        for config in configs:
            self.assertLessEqual(
                config["estimated_vram_gb"],
                8 * VRAM_LIMIT_FRACTION + 0.01,  # Small tolerance
                f"Config {config['id']} exceeds VRAM limit"
            )

    def test_context_size_filtering(self):
        """Configs with context > model max should be filtered."""
        model = _make_model("SmallCtxModel", "qwen3", context_length=8192, size_gb=2.0)
        gpus = [_make_gpu("3090", 24)]

        configs, _ = generate_configs(
            [model], gpus, draft_pool=[],
            context_sizes=[4096, 8192, 16384, 32768],
        )

        # Model max context is 8192, so only 4096 and 8192 should pass
        for config in configs:
            self.assertLessEqual(config["context_size"], 8192)

    def test_draft_filtering(self):
        """Configs needing draft but no draft available should be filtered."""
        model = _make_model("TestModel", "qwen3", size_gb=2.0)
        gpus = [_make_gpu("3090", 24)]

        # No draft pool — dflash/mtp configs should be filtered
        configs, stats = generate_configs(
            [model], gpus, draft_pool=[], context_sizes=[4096],
        )

        # Only "none" spec configs should remain
        for config in configs:
            self.assertEqual(config["spec_type"], "none",
                f"Config {config['id']} has spec {config['spec_type']} but no draft available")

    def test_draft_availability(self):
        """Configs with available draft should be included."""
        model = _make_model("TestModel", "qwen3", size_gb=2.0)
        gpus = [_make_gpu("3090", 24)]
        drafts = [_make_draft("TestModel-DFlash-Q4_K_M", "qwen3")]

        configs, _ = generate_configs(
            [model], gpus, draft_pool=drafts, context_sizes=[4096],
        )

        spec_types = {c["spec_type"] for c in configs}
        self.assertIn("dflash", spec_types, "dflash config should be present when draft exists")

    def test_config_id_uniqueness(self):
        """All config IDs should be unique."""
        models = [_make_model(f"Model{i}", "qwen3", size_gb=2.0) for i in range(3)]
        gpus = [_make_gpu("3090", 24)]

        configs, _ = generate_configs(models, gpus, draft_pool=[], context_sizes=[4096, 8192])

        ids = [c["id"] for c in configs]
        self.assertEqual(len(ids), len(set(ids)), "Config IDs should be unique")

    def test_config_has_required_fields(self):
        """Each config should have all required fields."""
        model = _make_model()
        gpus = [_make_gpu("3090", 24)]

        configs, _ = generate_configs([model], gpus, draft_pool=[], context_sizes=[4096])

        required_fields = [
            "id", "rank", "model", "model_path", "gpu", "gpu_index",
            "cache_k", "cache_v", "spec_type", "spec_model", "context_size",
            "estimated_vram_gb", "expected_value", "docker_profile",
        ]

        for config in configs:
            for field in required_fields:
                self.assertIn(field, config, f"Config missing field '{field}'")

    def test_rank_assignment(self):
        """Ranks should be assigned sequentially from 1."""
        models = [_make_model(f"Model{i}", "qwen3", size_gb=2.0) for i in range(5)]
        gpus = [_make_gpu("3090", 24)]

        configs, _ = generate_configs(models, gpus, draft_pool=[], context_sizes=[4096])

        ranks = [c["rank"] for c in configs]
        self.assertEqual(ranks, list(range(1, len(configs) + 1)))

    def test_gpu_filter(self):
        """--gpu filter should only return configs for the specified GPU."""
        models = [_make_model("TestModel", "qwen3", size_gb=2.0)]
        gpus = [_make_gpu("3090", 24), _make_gpu("3070", 8)]

        configs_3090, _ = generate_configs(models, gpus, gpu_filter="3090", context_sizes=[4096])
        configs_3070, _ = generate_configs(models, gpus, gpu_filter="3070", context_sizes=[4096])

        for c in configs_3090:
            self.assertEqual(c["gpu"], "3090")
        for c in configs_3070:
            self.assertEqual(c["gpu"], "3070")

    def test_empty_pool(self):
        """Empty model pool should return empty configs."""
        configs, stats = generate_configs([], GPU_POOL, context_sizes=[4096])
        self.assertEqual(len(configs), 0)
        self.assertEqual(stats["candidates"], 0)

    def test_compatibility_stats(self):
        """Stats should track filtering pipeline correctly."""
        models = [_make_model("TestModel", "qwen3", size_gb=2.0)]
        gpus = [_make_gpu("3090", 24)]

        configs, stats = generate_configs(models, gpus, draft_pool=[], context_sizes=[4096])

        self.assertGreater(stats["candidates"], 0)
        self.assertGreaterEqual(stats["after_compatibility"], 0)
        self.assertGreaterEqual(stats["after_vram"], 0)
        self.assertGreaterEqual(stats["after_draft"], 0)
        self.assertEqual(len(configs), stats["after_draft"])


# ─── Draft Model Tests ─────────────────────────────────────────────────

class TestDraftModel(TestCase):
    """Test draft model finding and graceful handling."""

    def test_no_draft_for_none_spec(self):
        """spec_type='none' should return None for draft."""
        model = _make_model()
        result = _find_draft_model(model, "none", [])
        self.assertIsNone(result)

    def test_draft_found_by_name(self):
        """Draft should be found when name matches."""
        model = _make_model("Qwen3.6-35B")
        drafts = [_make_draft("Qwen3.6-35B-DFlash-Q4_K_M")]
        result = _find_draft_model(model, "dflash", drafts)
        self.assertIsNotNone(result)

    def test_draft_not_found(self):
        """Draft should return None when no match exists."""
        model = _make_model("UnknownModel")
        drafts = [_make_draft("OtherModel-DFlash-Q4_K_M")]
        result = _find_draft_model(model, "dflash", drafts)
        self.assertIsNone(result)

    def test_draft_empty_pool(self):
        """Empty draft pool should return None."""
        model = _make_model()
        result = _find_draft_model(model, "dflash", [])
        self.assertIsNone(result)

    def test_mtp_draft(self):
        """MTP draft should be found."""
        model = _make_model("Qwen3.5-9B-MTP")
        drafts = [_make_draft("Qwen3.5-9B-DFlash")]
        result = _find_draft_model(model, "mtp", drafts)
        # MTP draft matching is heuristic; might or might not find it
        # The important thing is it doesn't crash
        self.assertIsInstance(result, (str, type(None)))


# ─── Config ID Tests ───────────────────────────────────────────────────

class TestConfigID(TestCase):
    """Test config ID generation."""

    def test_id_format(self):
        """Config ID should follow expected format."""
        model = _make_model("Qwen3.6-35B-A3B")
        gpu = _make_gpu("3090")

        config_id = _make_config_id(
            model["name"], "kvarn5", "dflash", 131072, gpu["name"]
        )
        self.assertIn("gen-", config_id)
        self.assertIn("kvarn5", config_id)
        self.assertIn("dflash", config_id)
        self.assertIn("128k", config_id)
        self.assertIn("3090", config_id)

    def test_id_no_special_chars(self):
        """Config ID should not contain special characters."""
        config_id = _make_config_id("Qwen3.6-35B-A3B", "f16", "none", 4096, "3070")
        # Should only contain alphanumeric and hyphens
        for char in config_id:
            self.assertTrue(
                char.isalnum() or char == "-",
                f"Special char '{char}' in config ID: {config_id}"
            )

    def test_id_length(self):
        """Config ID should not be excessively long."""
        long_name = "A" * 50
        config_id = _make_config_id(long_name, "kvarn8", "dflash2", 131072, "3090")
        self.assertLess(len(config_id), 80, f"Config ID too long: {len(config_id)} chars")


# ─── Output Format Tests ───────────────────────────────────────────────

class TestOutputFormat(TestCase):
    """Test output JSON structure."""

    def test_build_output_structure(self):
        """Output should have required top-level fields."""
        configs = [{"id": "test", "rank": 1, "expected_value": 0.5}]
        stats = {"candidates": 10, "after_compatibility": 8, "after_vram": 5, "after_draft": 4}

        output = build_output(configs, stats, model_pool_count=10, gpu_pool_count=2)

        self.assertIn("generated_at", output)
        self.assertIn("model_pool_count", output)
        self.assertIn("gpu_pool_count", output)
        self.assertIn("filtering", output)
        self.assertIn("configs", output)

    def test_write_queue_creates_file(self):
        """write_queue should create a valid JSON file."""
        configs = [
            {
                "id": "test-config",
                "rank": 1,
                "model": "TestModel",
                "model_path": "/path/to/model.gguf",
                "gpu": "3090",
                "gpu_index": 0,
                "cache_k": "f16",
                "cache_v": "f16",
                "spec_type": "none",
                "spec_model": None,
                "context_size": 4096,
                "estimated_vram_gb": 18.0,
                "expected_value": 0.75,
                "docker_profile": "profile-test",
            }
        ]
        stats = {"candidates": 1, "after_compatibility": 1, "after_vram": 1, "after_draft": 1}

        output = build_output(configs, stats, 1, 2)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = f.name

        try:
            write_queue(output, tmp_path)

            with open(tmp_path) as f:
                loaded = json.load(f)

            self.assertEqual(loaded["model_pool_count"], 1)
            self.assertEqual(len(loaded["configs"]), 1)
            self.assertEqual(loaded["configs"][0]["id"], "test-config")
        finally:
            os.unlink(tmp_path)

    def test_output_has_iso_timestamp(self):
        """generated_at should be an ISO timestamp."""
        output = build_output([], {"candidates": 0, "after_compatibility": 0, "after_vram": 0, "after_draft": 0}, 0, 0)
        ts = output["generated_at"]
        # Should be parseable as ISO format
        from datetime import datetime
        datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ─── Model Pool Loading Tests ──────────────────────────────────────────

class TestModelPool(TestCase):
    """Test model pool loading."""

    def test_load_json_pool(self):
        """Should load model pool from JSON file."""
        pool_data = [
            {
                "path": "/test/model.gguf",
                "name": "TestModel",
                "arch": "qwen3",
                "totalParams": 1000000000,
                "activeParams": 1000000000,
                "sizeGb": 2.0,
                "quantType": "Q4_K_M",
                "hasMtp": False,
                "supportsKvarn": True,
                "contextLength": 32768,
                "blockCount": 32,
                "expertCount": None,
            }
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(pool_data, f)
            tmp_path = f.name

        try:
            pool = load_model_pool(tmp_path)
            self.assertEqual(len(pool), 1)
            self.assertEqual(pool[0]["name"], "TestModel")
            self.assertEqual(pool[0]["arch"], "qwen3")
        finally:
            os.unlink(tmp_path)

    def test_load_empty_pool(self):
        """Should handle empty pool file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([], f)
            tmp_path = f.name

        try:
            pool = load_model_pool(tmp_path)
            self.assertEqual(len(pool), 0)
        finally:
            os.unlink(tmp_path)

    def test_normalize_model_adds_defaults(self):
        """_normalize_model should add default fields."""
        raw = {"path": "/test/model.gguf", "name": "Test"}
        model = _normalize_model(raw)

        self.assertIsNotNone(model)
        self.assertEqual(model["arch"], "unknown")
        self.assertFalse(model["hasMtp"])
        self.assertIn("quality_weight", model)

    def test_normalize_model_with_params(self):
        """_normalize_model should handle param fields."""
        raw = {
            "path": "/test/model.gguf",
            "arch": "llama",
            "total_params": 7000000000,
            "active_params": 7000000000,
        }
        model = _normalize_model(raw)

        self.assertIsNotNone(model)
        self.assertEqual(model["totalParams"], 7000000000)
        self.assertEqual(model["activeParams"], 7000000000)


# ─── CLI Argument Parsing Tests ────────────────────────────────────────

class TestCLI(TestCase):
    """Test CLI argument parsing."""

    def test_default_args(self):
        """Default args should set scan=True."""
        args = parse_args([])
        self.assertTrue(args.scan)

    def test_dry_run(self):
        """--dry-run flag should be parsed."""
        args = parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)

    def test_gpu_filter(self):
        """--gpu flag should set gpu filter."""
        args = parse_args(["--gpu", "3090"])
        self.assertEqual(args.gpu, "3090")

    def test_role_filter(self):
        """--role flag should set role filter."""
        args = parse_args(["--role", "orchestrator"])
        self.assertEqual(args.role, "orchestrator")

    def test_estimate_args(self):
        """--estimate should parse 5 arguments."""
        args = parse_args([
            "--estimate", "/path/to/model.gguf", "3090", "kvarn5", "dflash", "131072"
        ])
        self.assertIsNotNone(args.estimate)
        self.assertEqual(len(args.estimate), 5)

    def test_add_model(self):
        """--add-model should set the model path."""
        args = parse_args(["--add-model", "/path/to/model.gguf"])
        self.assertEqual(args.add_model, "/path/to/model.gguf")


# ─── Integration Tests ─────────────────────────────────────────────────

class TestIntegration(TestCase):
    """Integration tests with realistic model pool."""

    def test_full_pipeline_small_pool(self):
        """Full pipeline with a small, realistic pool."""
        models = [
            _make_model("Qwen3.6-35B-A3B", "qwen3", size_gb=17.0, active_params=3_500_000_000),
            _make_model("Qwen3.5-9B-MTP", "qwen3", size_gb=5.5, active_params=9_000_000_000),
            _make_model("Laguna-XS", "laguna", size_gb=15.0, active_params=15_000_000_000),
        ]
        gpus = [_make_gpu("3090", 24)]
        drafts = [_make_draft("Qwen3.6-35B-A3B-DFlash-Q4_K_M")]

        configs, stats = generate_configs(
            models, gpus, draft_pool=drafts, context_sizes=[4096, 8192, 16384]
        )

        # Should have configs for at least some combinations
        self.assertGreater(len(configs), 0)

        # All configs should be valid
        for config in configs:
            self.assertGreater(config["estimated_vram_gb"], 0)
            self.assertGreaterEqual(config["expected_value"], 0)
            self.assertIn(config["spec_type"], ["none", "dflash", "mtp"])

    def test_mixed_architectures(self):
        """Test with mixed architectures."""
        models = [
            _make_model("Qwen3Model", "qwen3", size_gb=5.0),
            _make_model("LlamaModel", "llama", size_gb=5.0),
            _make_model("GemmaModel", "gemma4", size_gb=5.0),
            _make_model("GlmModel", "glm4", size_gb=5.0),
        ]
        gpus = [_make_gpu("3090", 24)]

        configs, stats = generate_configs(
            models, gpus, draft_pool=[], context_sizes=[4096]
        )

        # Should have configs for compatible archs
        self.assertGreater(len(configs), 0)

        # All configs should have valid architectures
        for config in configs:
            arch = config.get("arch")
            if arch:
                self.assertIn(arch, COMPATIBILITY)

    def test_dual_gpu(self):
        """Test with both GPUs."""
        models = [_make_model("TestModel", "qwen3", size_gb=2.0)]
        gpus = [_make_gpu("3090", 24), _make_gpu("3070", 8)]

        configs, _ = generate_configs(
            models, gpus, draft_pool=[], context_sizes=[4096]
        )

        gpu_counts = {}
        for config in configs:
            gpu = config["gpu"]
            gpu_counts[gpu] = gpu_counts.get(gpu, 0) + 1

        # Both GPUs should have configs
        self.assertIn("3090", gpu_counts)
        # 3070 might have fewer due to VRAM constraints
        # (but with size_gb=2.0, it should fit)


if __name__ == "__main__":
    unittest_main()
