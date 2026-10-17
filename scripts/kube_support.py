"""Tools and credentials for the dedicated local Kubernetes exercise."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from common import private_root, save_json

ROOT = Path(__file__).resolve().parents[1]
STATE = private_root(os.environ.get("PGOPS_KUBE_WORKSPACE", str(Path.home() / "pgops-kube-private")))


def command(argv, *, data=None, check=True):
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed with exit code {result.returncode}")
    return result


def save(path, value):
    save_json(path, value)


def now():
    return datetime.now(timezone.utc).isoformat()
