"""
bq_env.py -- make `from google.cloud import bigquery` work on the Workbench image.

The Workbench JupyterLab image (as of 2026-10) ships google-cloud-bigquery 3.45,
which expects google-api-core >= 2.28, but the image build then pip-installs dsub,
which pins google-api-core <= 2.25.1. The result: the BigQuery client fails to
import on a fresh VM with
    AttributeError: module 'google.api_core' has no attribute 'check_python_version'

The only things the BigQuery client needs from the newer google-api-core are
check_python_version() and check_dependency_versions(), which just print
warnings about outdated Python or dependencies. ensure_bigquery() adds do-nothing
stand-ins for those two functions when they're missing, then imports the client.
Nothing is installed, so there's no kernel restart. It also hides
google-api-core's (harmless) grpcio post-quantum-crypto FutureWarning.

    from bq_env import ensure_bigquery
    bigquery = ensure_bigquery()

If the import still fails, the older fix is to restore the image's intended
version and restart the kernel:
    pip install "google-api-core[grpc]==2.38.0"
"""
import warnings


def _quiet():
    warnings.filterwarnings("ignore", message=".*Post-Quantum Cryptography.*", category=FutureWarning)


def ensure_bigquery():
    _quiet()
    import google.api_core as api_core

    for name in ("check_python_version", "check_dependency_versions"):
        if not hasattr(api_core, name):
            setattr(api_core, name, lambda *args, **kwargs: None)

    from google.cloud import bigquery
    return bigquery
