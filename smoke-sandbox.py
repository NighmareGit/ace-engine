"""Sandbox smoke test for Triton deployment. Run on Triton."""
import os, tempfile
from engine.intent.sandbox import SandboxRunner, SandboxConfig

print("=== Sandbox Smoke Test ===")
r = SandboxRunner(SandboxConfig(timeout_sec=10))
print("available:", r.available, "| backend:", r.backend.value)

# Trivial payload
res1 = r.run_code("def run():\n    return {'ok': True, 'msg': 'hello from sandbox'}\n")
print("\n--- trivial ---")
print("ok:", res1.ok, "rc:", res1.returncode, "timed_out:", res1.timed_out)
print("value:", res1.value)
print("error:", res1.error)
print("stdout:", repr(res1.stdout[:300]))
print("stderr:", repr(res1.stderr[:300]))

# No-network enforcement
code2 = (
    "import socket\n"
    "def run():\n"
    "    s = socket.socket()\n"
    "    try:\n"
    "        s.connect(('192.0.2.3', 1))\n"
    "        return 'CONNECTED'\n"
    "    except OSError:\n"
    "        return 'NO_NET'\n"
    "    finally:\n"
    "        s.close()\n"
)
res2 = r.run_code(code2)
print("\n--- no-network ---")
print("ok:", res2.ok, "rc:", res2.returncode, "timed_out:", res2.timed_out)
print("value:", res2.value)
print("error:", res2.error)
print("stdout:", repr(res2.stdout[:300]))
print("stderr:", repr(res2.stderr[:300]))

# Summary
print("\n=== VERDICT ===")
trivial_ok = res1.ok and res1.value and res1.value.get("ok") == True
nonet_ok = res2.value == "NO_NET"
print("trivial payload:", "PASS" if trivial_ok else "FAIL")
print("no-network enforcement:", "PASS" if nonet_ok else "FAIL")
print("overall:", "PASS" if (trivial_ok and nonet_ok) else "FAIL")
