"""Tests for deploy/host/preflight.py — the mandatory pre-run gate.

Covers: pin-driven pass, mismatch FAIL with remediation text, same-model-on-both
FAIL, --update-pin without --confirm refused, unreachable FAIL, gitea, workspace.
Uses a mock HTTP layer (urllib.request.urlopen patching) following existing test
conventions (mock transports, no real network).
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import urllib.error

# Ensure repo root is importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from deploy.host import preflight


def _make_models_response(model_ids):
    """Build an OpenAI-style /v1/models JSON response."""
    return json.dumps({"data": [{"id": mid} for mid in model_ids]}).encode("utf-8")


def _mock_response(status=200, body=b""):
    """Create a mock urllib response object."""
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body
    return resp


# Side-effect factory: returns correct model per configured URL
def _port_side_effect(subject_models, judge_models, gitea_ok=True):
    """Build a urlopen side_effect that serves different models per endpoint URL.

    Uses the preflight module's current SUBJECT_URL/JUDGE_URL/GITEA_URL so it
    stays correct even after env-var overrides reload the module.
    """
    def side_effect(url, timeout=None):
        url_str = str(url) if not isinstance(url, str) else url
        # Match against the actual configured URLs (not hardcoded ports)
        if "/v1/models" in url_str and preflight.SUBJECT_URL in url_str:
            return _mock_response(200, _make_models_response(subject_models))
        if "/v1/models" in url_str and preflight.JUDGE_URL in url_str:
            return _mock_response(200, _make_models_response(judge_models))
        if preflight.GITEA_URL in url_str:
            if gitea_ok:
                return _mock_response(200, b"")
            raise urllib.error.URLError("refused")
        raise AssertionError(f"unexpected URL: {url_str}")
    return side_effect


class TestPinDrivenPass(unittest.TestCase):
    """Pin-driven: correct models on both endpoints -> PASS."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_pin_driven_pass(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP-Q4_K_M"],
            judge_models=["Qwen3.6-35B-A3B-IQ4_XS"],
        )
        with patch.object(preflight, "_check_engine_imports", return_value=(True, "mocked")):
            ok_sub, det_sub = preflight._check_endpoint_models(
                preflight.SUBJECT_URL, "qwen3.5-9b", "subject")
            ok_jud, det_jud = preflight._check_endpoint_models(
                preflight.JUDGE_URL, "qwen3.6-35b", "judge")
            ok_git, det_git = preflight._check_gitea()
            ok_diff, det_diff = preflight._check_different_models(
                preflight.SUBJECT_URL, preflight.JUDGE_URL)

        self.assertTrue(ok_sub, det_sub)
        self.assertIn("Qwen3.5-9B", det_sub)
        self.assertTrue(ok_jud, det_jud)
        self.assertIn("Qwen3.6-35B", det_jud)
        self.assertTrue(ok_git, det_git)
        self.assertTrue(ok_diff, det_diff)


class TestPinMismatchFail(unittest.TestCase):
    """A 200 with wrong model name -> FAIL with remediation hint."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_wrong_model_on_subject_fails_with_remediation(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Llama-3.1-8B-Instruct"],
            judge_models=["Qwen3.6-35B-A3B-IQ4_XS"],
        )
        ok, detail = preflight._check_endpoint_models(
            preflight.SUBJECT_URL, "qwen3.5-9b", "subject")
        self.assertFalse(ok)
        self.assertIn("FAIL", detail)
        self.assertIn("WARNING", detail)
        self.assertIn("Llama-3.1-8B", detail)  # actual names logged
        # Remediation hint present
        self.assertIn("model-pin.json", detail)
        self.assertIn("git history is the audit trail", detail)
        self.assertIn("TRITON_SUBJECT_URL", detail)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_wrong_model_on_judge_fails(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP"],
            judge_models=["Qwen3.5-9B-MTP"],  # wrong: judge should be 3.6-35b
        )
        ok, detail = preflight._check_endpoint_models(
            preflight.JUDGE_URL, "qwen3.6-35b", "judge")
        self.assertFalse(ok)
        self.assertIn("FAIL", detail)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_empty_model_list_fails(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=[],
            judge_models=["Qwen3.6-35B-A3B"],
        )
        ok, detail = preflight._check_endpoint_models(
            preflight.SUBJECT_URL, "qwen3.5-9b", "subject")
        self.assertFalse(ok)
        self.assertIn("no model names", detail)


class TestSameModelOnBothEndpoints(unittest.TestCase):
    """Structural invariant: same model on both endpoints -> FAIL regardless of pin."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_same_model_on_both_fails(self, mock_urlopen):
        """Both endpoints serve Qwen3.5-9B — FAIL even though subject pin matches."""
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP"],
            judge_models=["Qwen3.5-9B-MTP"],  # SAME model
        )
        ok, detail = preflight._check_different_models(
            preflight.SUBJECT_URL, preflight.JUDGE_URL)
        self.assertFalse(ok)
        self.assertIn("SAME", detail)
        self.assertIn("MUST be different", detail)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_different_models_pass(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP"],
            judge_models=["Qwen3.6-35B-A3B"],
        )
        ok, detail = preflight._check_different_models(
            preflight.SUBJECT_URL, preflight.JUDGE_URL)
        self.assertTrue(ok)
        self.assertIn("different models", detail)


class TestUnreachable(unittest.TestCase):
    """Endpoint unreachable -> FAIL."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_subject_unreachable_fails(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        ok, detail = preflight._check_endpoint_models(
            preflight.SUBJECT_URL, "qwen3.5-9b", "subject")
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_gitea_unreachable_fails(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        ok, detail = preflight._check_gitea()
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class TestGitea(unittest.TestCase):
    """Gitea reachability checks."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_gitea_root_200_passes(self, mock_urlopen):
        def side_effect(url, timeout=None):
            url_str = str(url) if not isinstance(url, str) else url
            if url_str.endswith("/"):
                return _mock_response(200, b"<html>Gitea</html>")
            raise urllib.error.URLError("not found")
        mock_urlopen.side_effect = side_effect
        ok, detail = preflight._check_gitea()
        self.assertTrue(ok)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_gitea_api_version_passes(self, mock_urlopen):
        def side_effect(url, timeout=None):
            url_str = str(url) if not isinstance(url, str) else url
            if "/api/v1/version" in url_str:
                return _mock_response(200, b'{"version": "1.21.0"}')
            raise urllib.error.URLError("not found")
        mock_urlopen.side_effect = side_effect
        ok, detail = preflight._check_gitea()
        self.assertTrue(ok)

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_gitea_404_on_both_fails(self, mock_urlopen):
        def side_effect(url, timeout=None):
            return _mock_response(404, b"not found")
        mock_urlopen.side_effect = side_effect
        ok, detail = preflight._check_gitea()
        self.assertFalse(ok)


class TestWorkspace(unittest.TestCase):
    """Workspace validation checks."""

    def test_nonexistent_workspace_fails(self):
        ok, detail = preflight._check_workspace("/nonexistent/path/that/does/not/exist")
        self.assertFalse(ok)
        self.assertIn("does not exist", detail)

    def test_not_a_git_repo_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = preflight._check_workspace(tmp)
            self.assertFalse(ok)
            self.assertIn("not a git repository", detail)

    def test_clean_git_repo_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.system(f"cd {tmp} && git init -q && git commit --allow-empty -q -m init")
            ok, detail = preflight._check_workspace(tmp)
            self.assertTrue(ok)
            self.assertIn("clean", detail)

    def test_dirty_git_repo_warns_but_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.system(f"cd {tmp} && git init -q && git commit --allow-empty -q -m init")
            with open(os.path.join(tmp, "dirty_file.txt"), "w") as f:
                f.write("uncommitted content\n")
            ok, detail = preflight._check_workspace(tmp)
            self.assertTrue(ok)
            self.assertIn("WARNING", detail)
            self.assertIn("dirty", detail)


class TestEnvOverrides(unittest.TestCase):
    """Env-var overrides change the checked URLs."""

    @patch.dict(os.environ, {
        "TRITON_SUBJECT_URL": "http://192.0.2.1:9092",
        "TRITON_JUDGE_URL": "http://192.0.2.1:9090",
        "TRITON_GITEA_URL": "http://192.0.2.1:9000",
    })
    def test_env_overrides_applied(self):
        import importlib
        importlib.reload(preflight)
        self.assertEqual(preflight.SUBJECT_URL, "http://192.0.2.1:9092")
        self.assertEqual(preflight.JUDGE_URL, "http://192.0.2.1:9090")
        self.assertEqual(preflight.GITEA_URL, "http://192.0.2.1:9000")


class TestCaseInsensitive(unittest.TestCase):
    """Pin matching is case-insensitive."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_case_insensitive_match(self, mock_urlopen):
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["QWEN3.5-9B-mtp"],
            judge_models=["QWEN3.6-35B-a3b"],
        )
        ok, detail = preflight._check_endpoint_models(
            preflight.SUBJECT_URL, "qwen3.5-9b", "subject")
        self.assertTrue(ok)


class TestModelPinLoading(unittest.TestCase):
    """ModelPin.load reads and validates the pin file."""

    def test_load_valid_pin(self):
        pin = preflight.ModelPin.load()
        self.assertEqual(pin.subject, "qwen3.5-9b")
        self.assertEqual(pin.judge, "qwen3.6-35b")

    def test_load_missing_pin_raises(self):
        with self.assertRaises(FileNotFoundError):
            preflight.ModelPin.load("/nonexistent/pin.json")

    def test_load_malformed_pin_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"subject": "x"}, f)  # missing 'judge'
            f.flush()
            with self.assertRaises(ValueError):
                preflight.ModelPin.load(f.name)
        os.unlink(f.name)

    def test_pin_lowercases(self):
        pin = preflight.ModelPin(subject="Qwen3.5-9B", judge="Qwen3.6-35B")
        self.assertEqual(pin.subject, "qwen3.5-9b")
        self.assertEqual(pin.judge, "qwen3.6-35b")


class TestUpdatePin(unittest.TestCase):
    """--update-pin requires --confirm; without it, refuses to write."""

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_update_pin_without_confirm_refused(self, mock_urlopen):
        """Without --confirm, _update_pin prints diff and refuses."""
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP"],
            judge_models=["Qwen3.6-35B-A3B"],
        )
        ok, msg = preflight._update_pin(preflight.SUBJECT_URL, preflight.JUDGE_URL, confirm=False)
        self.assertFalse(ok)
        # "REFUSED" is printed to stdout; the return message confirms refusal
        self.assertIn("refused", msg.lower())

    @patch("deploy.host.preflight.urllib.request.urlopen")
    def test_update_pin_with_confirm_writes(self, mock_urlopen):
        """With --confirm, _update_pin writes the pin file."""
        mock_urlopen.side_effect = _port_side_effect(
            subject_models=["Qwen3.5-9B-MTP"],
            judge_models=["Qwen3.6-35B-A3B"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_pin = os.path.join(tmp, "model-pin.json")
            # Seed with old pin
            with open(tmp_pin, "w") as f:
                json.dump({"subject": "old-model", "judge": "old-judge",
                           "pinned_at": "2020-01-01", "note": "old"}, f)
            with patch.object(preflight, "PIN_FILE", tmp_pin):
                ok, msg = preflight._update_pin(preflight.SUBJECT_URL, preflight.JUDGE_URL, confirm=True)
            self.assertTrue(ok)
            # Verify file was written
            with open(tmp_pin, "r") as f:
                data = json.load(f)
            self.assertEqual(data["subject"], "qwen3.5-9b-mtp")
            self.assertEqual(data["judge"], "qwen3.6-35b-a3b")
            self.assertIn("pinned_at", data)


if __name__ == "__main__":
    unittest.main()
