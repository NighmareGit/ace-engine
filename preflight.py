"""Pre-flight health check for benchmark operations."""

import json
import sys
import time
from ssh_utils import SSHClient, TRITON_HOST, TRITON_USER, TRITON_PW, PORT_3090, PORT_3070


def preflight_check(ssh_client=None):
    """Run all pre-flight checks. Returns dict with check name -> {status, detail}."""
    results = {}

    # Create client if not provided
    create_client = ssh_client is None
    if create_client:
        ssh_client = SSHClient()

    # 0. SSH Connectivity
    try:
        if create_client:
            connected = ssh_client.connect()
        else:
            connected = ssh_client.connected
        if connected:
            results["ssh_connectivity"] = {"status": "pass", "detail": f"Connected to {TRITON_HOST}"}
        else:
            results["ssh_connectivity"] = {"status": "fail", "detail": f"Cannot connect to {TRITON_HOST}"}
            # If SSH fails, all other checks will fail too
            for check_name in ["gpu_availability", "beellama_3090_health", "beellama_3070_health",
                               "disk_space", "system_load", "stress_test"]:
                results[check_name] = {"status": "fail", "detail": "Skipped: SSH connection failed"}
            if create_client:
                ssh_client.close()
            return results
    except Exception as e:
        results["ssh_connectivity"] = {"status": "fail", "detail": str(e)}
        if create_client:
            ssh_client.close()
        return results

    # 1. GPU availability (nvidia-smi on both GPUs)
    try:
        gpus = ssh_client.get_nvidia_smi()
        if len(gpus) >= 2:
            gpu_names = [f"GPU{g['index']}: {g['name']} ({g['memory_used_mb']}/{g['memory_total_mb']}MB, {g['temperature_c']}°C)"
                        for g in gpus]
            results["gpu_availability"] = {
                "status": "pass",
                "detail": f"{len(gpus)} GPUs detected. " + " | ".join(gpu_names)
            }
        elif len(gpus) == 1:
            results["gpu_availability"] = {
                "status": "warn",
                "detail": f"Only 1 GPU detected: {gpus[0]['name']}"
            }
        else:
            results["gpu_availability"] = {"status": "fail", "detail": "No GPUs detected"}
    except Exception as e:
        results["gpu_availability"] = {"status": "fail", "detail": str(e)}

    # 2. beellama health on port 8080 (3090)
    try:
        health_3090 = ssh_client.check_beellama_health(PORT_3090)
        if health_3090:
            models = ssh_client.get_model_list(PORT_3090)
            results["beellama_3090_health"] = {
                "status": "pass",
                "detail": f"Healthy. Models: {', '.join(models) if models else 'none loaded'}"
            }
        else:
            results["beellama_3090_health"] = {"status": "fail", "detail": "Unhealthy or not responding"}
    except Exception as e:
        results["beellama_3090_health"] = {"status": "fail", "detail": str(e)}

    # 3. beellama health on port 8082 (3070)
    try:
        health_3070 = ssh_client.check_beellama_health(PORT_3070)
        if health_3070:
            models = ssh_client.get_model_list(PORT_3070)
            results["beellama_3070_health"] = {
                "status": "pass",
                "detail": f"Healthy. Models: {', '.join(models) if models else 'none loaded'}"
            }
        else:
            results["beellama_3070_health"] = {"status": "fail", "detail": "Unhealthy or not responding"}
    except Exception as e:
        results["beellama_3070_health"] = {"status": "fail", "detail": str(e)}

    # 4. Disk space > 10GB on /home/<user>/
    try:
        stdout, stderr, exit_code = ssh_client.run("df -BG /home/<user>/ | tail -1 | awk '{print $4}'")
        if exit_code == 0:
            space_gb = int(stdout.strip().replace("G", "").replace("G", ""))
            if space_gb >= 10:
                results["disk_space"] = {"status": "pass", "detail": f"{space_gb}GB available"}
            else:
                results["disk_space"] = {"status": "fail", "detail": f"Only {space_gb}GB available (need 10GB)"}
        else:
            results["disk_space"] = {"status": "fail", "detail": f"Failed to check disk space: {stderr}"}
    except Exception as e:
        results["disk_space"] = {"status": "fail", "detail": str(e)}

    # 5. System load (load average < 4.0)
    try:
        stdout, stderr, exit_code = ssh_client.run("cat /proc/loadavg | awk '{print $1}'")
        if exit_code == 0:
            load_avg = float(stdout.strip())
            if load_avg < 4.0:
                results["system_load"] = {"status": "pass", "detail": f"Load average: {load_avg}"}
            else:
                results["system_load"] = {"status": "warn", "detail": f"High load average: {load_avg}"}
        else:
            results["system_load"] = {"status": "fail", "detail": f"Failed to check load: {stderr}"}
    except Exception as e:
        results["system_load"] = {"status": "fail", "detail": str(e)}

    # 6. Stress test: 3 rapid API calls succeed
    try:
        stress_messages = [{"role": "user", "content": "Say 'OK' and nothing else."}]
        success_count = 0
        for i in range(3):
            try:
                response = ssh_client.curl_beellama(
                    port=PORT_3090,
                    messages=stress_messages,
                    max_tokens=10,
                    temperature=0.1
                )
                # Check for content or reasoning_content (thinking models may only return reasoning)
                if response.get("content") or response.get("reasoning_content"):
                    success_count += 1
            except Exception:
                pass

        if success_count == 3:
            results["stress_test"] = {"status": "pass", "detail": "3/3 rapid calls succeeded"}
        elif success_count > 0:
            results["stress_test"] = {"status": "warn", "detail": f"{success_count}/3 rapid calls succeeded"}
        else:
            results["stress_test"] = {"status": "fail", "detail": "0/3 rapid calls succeeded"}
    except Exception as e:
        results["stress_test"] = {"status": "fail", "detail": str(e)}

    if create_client:
        ssh_client.close()

    return results


if __name__ == "__main__":
    print("Running pre-flight checks...")
    print("=" * 60)

    results = preflight_check()

    all_pass = True
    for name, result in results.items():
        status = result['status']
        if status == 'pass':
            icon = '✅'
        elif status == 'warn':
            icon = '⚠️'
        else:
            icon = '❌'
            all_pass = False
        print(f"  {icon} {name}: {result.get('detail', '')}")

    print("=" * 60)
    if all_pass:
        print("All checks passed! Ready for benchmarking.")
    else:
        print("Some checks failed. Review issues above.")

    sys.exit(0 if all_pass else 1)
