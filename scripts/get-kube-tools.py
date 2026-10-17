#!/usr/bin/env python3
"""공식 배포처에서 고정 버전 도구를 내려받고 SHA-256을 확인합니다."""
import hashlib
import json
from pathlib import Path
import urllib.request

from kube_support import STATE
tools_dir = STATE / 'bin'
tools_dir.mkdir(parents=True, exist_ok=True)

def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'postgres-ops-lab'})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()

release = json.loads(get('https://api.github.com/repos/kubernetes-sigs/kind/releases/tags/v0.30.0'))
assets = {item['name']: item for item in release['assets']}
asset = assets['kind-linux-amd64']
digest = asset.get('digest', '')
if digest.startswith('sha256:'):
    expected = digest.split(':')[1]
else:
    checksum = next(v for k, v in assets.items() if k.startswith('kind-linux-amd64.') and 'sha256' in k)
    expected = get(checksum['browser_download_url']).decode().split()[0]
for name, url, sha in [
    ('kind', asset['browser_download_url'], expected),
    ('kubectl', 'https://dl.k8s.io/release/v1.34.0/bin/linux/amd64/kubectl',
     get('https://dl.k8s.io/release/v1.34.0/bin/linux/amd64/kubectl.sha256').decode().strip()),
]:
    path = tools_dir / name
    content = path.read_bytes() if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha else get(url)
    if hashlib.sha256(content).hexdigest() != sha:
        raise SystemExit(f'{name}: checksum mismatch')
    path = tools_dir / name
    path.write_bytes(content)
    path.chmod(0o755)
    print(f'{name}: SHA-256 verified', flush=True)
