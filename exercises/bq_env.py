"""
bq_env.py -- make `from google.cloud import bigquery` work on the Workbench image.

The Workbench JupyterLab image (as of 2026-10) ships google-cloud-bigquery 3.45,
which needs google-api-core >= 2.28, but the image build then pip-installs dsub,
which pins google-api-core <= 2.25.1. The result: the BigQuery client fails to
import on a fresh VM with
    AttributeError: module 'google.api_core' has no attribute 'check_python_version'

ensure_bigquery() tries the import and, only if it fails, restores the
google-api-core version the image was built with, then retries. It also hides
google-api-core's (harmless) grpcio post-quantum-crypto FutureWarning.

    from bq_env import ensure_bigquery
    bigquery = ensure_bigquery()
"""
import importlib
import subprocess
import sys
import warnings

API_CORE_PIN = "google-api-core[grpc]==2.38.0"


def _quiet():
    warnings.filterwarnings("ignore", message=".*Post-Quantum Cryptography.*", category=FutureWarning)


def _import():
    from google.cloud import bigquery
    return bigquery


def ensure_bigquery():
    _quiet()
    try:
        return _import()
    except (ImportError, AttributeError) as e:
        print(f"BigQuery client failed to import ({type(e).__name__}: {e}).\n"
              f"Applying the known Workbench image fix: pip install {API_CORE_PIN}", flush=True)

    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", API_CORE_PIN])

    # Drop the half-imported google.* modules so the retry loads the new version.
    for name in [m for m in sys.modules if m == "google" or m.startswith("google.")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    _quiet()
    try:
        bigquery = _import()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Fix installed but the import still fails in this process. "
                           "Restart the kernel (or re-run the script) and try again.") from e
    print("BigQuery client OK.", flush=True)
    return bigquery
