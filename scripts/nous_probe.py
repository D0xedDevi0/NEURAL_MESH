"""Probe: get Nous runtime credentials via Hermes' own resolver, then call inference."""
import sys, json, urllib.request, urllib.error

# Ensure hermes package importable (version drifts across platform auto-updates)
import glob as _glob
for _sp in _glob.glob("/opt/hermes/.venv/lib/python3.*/site-packages"):
    sys.path.insert(0, _sp)
sys.path.insert(0, "/opt/hermes")

from hermes_cli.auth import resolve_nous_runtime_credentials  # noqa: E402

print("=== resolving credentials via hermes resolver ===")
try:
    creds = resolve_nous_runtime_credentials(timeout_seconds=20)
    print("source:", creds.get("source"))
    print("provider:", creds.get("provider"))
    print("base_url:", creds.get("base_url"))
    print("key_id:", creds.get("key_id"))
    print("api_key len:", len(creds.get("api_key", "")))
    print("expires_in:", creds.get("expires_in"))
except Exception as e:
    print("RESOLVE_ERR", type(e).__name__, str(e)[:300])
    sys.exit(1)

api_key = creds.get("api_key")
base_url = creds.get("base_url") or "https://inference-api.nousresearch.com/v1"

print("\n=== probe inference call ===")
body = {
    "model": "deepseek/deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
    "max_tokens": 10,
}
req = urllib.request.Request(
    base_url.rstrip("/") + "/chat/completions",
    json.dumps(body).encode(),
    {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
)
try:
    r = urllib.request.urlopen(req, timeout=60)
    out = json.loads(r.read())
    print("STATUS OK")
    print("model:", out.get("model"))
    print("reply:", out["choices"][0]["message"]["content"])
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read()[:300])
except Exception as e:
    print("ERR", type(e).__name__, e)
