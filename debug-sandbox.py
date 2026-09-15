"""Debug: run entrypoint manually inside container to see result write."""
import os, json, tempfile
from engine.intent.sandbox import SandboxRunner, SandboxConfig

# Manually invoke run.sh to inspect
import subprocess
code = "def run():\n    return {'ok': True, 'msg': 'hello'}\n"
b64 = __import__('base64').b64encode(code.encode()).decode()
tmpdir = tempfile.mkdtemp(prefix="ace-dbg-")
print("tmpdir:", tmpdir)

run_sh = "/home/<user>/ace-engine/deploy/sandbox/run.sh"
cmd = [run_sh, "--code", code, "--timeout", "10", "--mem", "256m",
       "--workdir", tmpdir, "--result", f"{tmpdir}/result.json",
       "--image", "ace-sandbox:latest"]
proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
print("rc:", proc.returncode)
print("stdout:", repr(proc.stdout[:500]))
print("stderr:", repr(proc.stderr[:500]))
print("tmpdir contents:", os.listdir(tmpdir))
rj = os.path.join(tmpdir, "result.json")
if os.path.exists(rj):
    print("result.json:", open(rj).read())
else:
    print("NO result.json in tmpdir")
