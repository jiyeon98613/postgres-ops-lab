#!/usr/bin/env python3
"""로컬 kind 전용 CloudNativePG 설치와 primary Pod 삭제 모의 장애."""
import hashlib
import json
import sys
import time
import urllib.request
from kube_support import ROOT, STATE, command, save, now

KUBE = str(STATE / 'bin/kubectl')
CONFIG = str(STATE / 'kubeconfig')
DEST = STATE / 'results'
MANIFEST = 'https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.28/releases/cnpg-1.28.4.yaml'


def kube(*args, data=None, check=True):
    return command([KUBE, '--kubeconfig', CONFIG, '--context', 'kind-pgops-lab',
                    '--request-timeout=20s', *args], data=data, check=check)


def cluster():
    return json.loads(kube('-n', 'pgops-lab', 'get', 'cluster', 'pgops', '-o', 'json').stdout)


def psql(query, check=True):
    return kube('-n', 'pgops-lab', 'exec', '-i', 'pgops-client', '--',
                'psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                data=query.encode(), check=check)


def assert_local():
    config = json.loads(kube('config', 'view', '-o', 'json').stdout)
    server = next(x for x in config['clusters'] if x['name'] == 'kind-pgops-lab')['cluster']['server']
    if not server.startswith('https://127.0.0.1:'):
        raise RuntimeError('Only the dedicated loopback kind cluster is allowed')


def install():
    # 연결 대상이 새로 만든 로컬 kind인지 확인합니다. 기존 사용자 컨텍스트는 사용하지 않습니다.
    config = json.loads(kube('config', 'view', '-o', 'json').stdout)
    server = next(x for x in config['clusters'] if x['name'] == 'kind-pgops-lab')['cluster']['server']
    if not server.startswith('https://127.0.0.1:'):
        raise RuntimeError('전용 로컬 kind endpoint가 아닙니다.')
    with urllib.request.urlopen(MANIFEST, timeout=60) as response:
        manifest = response.read()
    path = STATE / 'cnpg-1.28.4.yaml'
    path.write_bytes(manifest)
    kube('apply', '--server-side', '-f', str(path))
    print('CloudNativePG operator 적용; 기동 대기', flush=True)
    kube('-n', 'cnpg-system', 'rollout', 'status', 'deployment/cnpg-controller-manager', '--timeout=120s')
    kube('apply', '-f', str(ROOT / 'kubernetes/cluster.yaml'))
    print('PostgreSQL 인스턴스 3개 생성 중', flush=True)
    # 실제 상태는 후속 명령과 README에서 확인할 수 있습니다.
    for _ in range(180):
        status = cluster().get('status', {})
        if status.get('readyInstances') == 3 and status.get('phase') == 'Cluster in healthy state':
            break
        time.sleep(2)
    else:
        raise RuntimeError('3개 인스턴스 준비 시간 초과')
    kube('apply', '-f', str(ROOT / 'kubernetes/client.yaml'))
    kube('-n', 'pgops-lab', 'wait', '--for=condition=Ready', 'pod/pgops-client', '--timeout=120s')
    save(DEST / 'installation.json', {
        'run_at_utc': now(), 'kind': '0.30.0', 'kubernetes': '1.34.0', 'cnpg': '1.28.4',
        'postgres_image': 'ghcr.io/cloudnative-pg/postgresql:18.6', 'instances': 3,
        'operator_manifest_url': MANIFEST, 'operator_manifest_sha256': hashlib.sha256(manifest).hexdigest(),
        'single_kubernetes_node': True, 'single_physical_host': True})
    print('CloudNativePG 3개 인스턴스와 실습 클라이언트 준비 완료', flush=True)


def failover():
    before = cluster()
    old_primary = before['status']['currentPrimary']
    if before['status'].get('readyInstances') != 3:
        raise RuntimeError('3개 인스턴스가 정상인 상태에서만 실험을 시작합니다.')
    psql('CREATE TABLE public.failover_events (event_key text PRIMARY KEY, payload text NOT NULL);')
    psql("INSERT INTO public.failover_events SELECT 'seed-'||n, md5(n::text) FROM generate_series(1, 100) n;")
    # replica 반영을 먼저 확인하여 baseline의 존재를 보증합니다.
    for pod in json.loads(kube('-n', 'pgops-lab', 'get', 'pods', '-l', 'cnpg.io/cluster=pgops', '-o', 'json').stdout)['items']:
        if pod['status']['phase'] != 'Running':
            continue
        name = pod['metadata']['name']
        for _ in range(60):
            result = kube('-n', 'pgops-lab', 'exec', name, '--', 'psql', '-U', 'postgres', '-d', 'app', '-Atc',
                          'SELECT count(*) FROM public.failover_events;', check=False)
            if result.returncode == 0 and result.stdout.decode().strip() == '100':
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('baseline 복제 확인 실패: ' + name)
    begin_at = now()
    start = time.perf_counter()
    kube('-n', 'pgops-lab', 'delete', 'pod', old_primary, '--grace-period=0', '--force', '--wait=false')
    events, acknowledged, new_primary = [], [], None
    for number in range(180):
        key = f'probe-{number}'
        result = psql(f"WITH inserted AS (INSERT INTO public.failover_events VALUES ('{key}', 'probe') ON CONFLICT DO NOTHING RETURNING event_key) SELECT pg_is_in_recovery() FROM inserted;", check=False)
        ok = result.returncode == 0 and result.stdout.decode().strip() == 'f'
        status = cluster()['status']
        primary = status.get('currentPrimary')
        events.append({'elapsed_seconds': round(time.perf_counter()-start, 3),
                       'write_succeeded': ok, 'primary': primary, 'phase': status.get('phase')})
        if ok:
            acknowledged.append(key)
        if ok and primary and primary != old_primary:
            new_primary = primary
            break
        time.sleep(0.5)
    if not new_primary:
        raise RuntimeError('새 primary의 쓰기 성공을 확인하지 못했습니다.')
    recovered_seconds = time.perf_counter()-start
    final_keys = json.loads(psql("SELECT json_agg(event_key ORDER BY event_key) FROM public.failover_events;").stdout)
    missing = sorted(set(acknowledged) - set(final_keys))
    seed_count = len([x for x in final_keys if x.startswith('seed-')])
    assert seed_count == 100 and not missing
    endpoints = json.loads(kube('-n', 'pgops-lab', 'get', 'endpointslices', '-l', 'kubernetes.io/service-name=pgops-rw', '-o', 'json').stdout)
    targets = [e.get('targetRef', {}).get('name') for item in endpoints['items'] for e in item['endpoints'] if e.get('conditions', {}).get('ready')]
    assert new_primary in targets
    save(DEST / 'failover.json', {
        'run_at_utc': begin_at, 'failure_injection': 'force deletion of primary Pod',
        'old_primary': old_primary, 'new_primary': new_primary,
        'injection_until_new_primary_write_seconds': round(recovered_seconds, 3),
        'probes': events, 'seed_rows_preserved': seed_count,
        'acknowledged_probe_writes': len(acknowledged), 'missing_acknowledged_keys': missing, 'rows_after_failover': len(final_keys),
        'unacknowledged_but_present_probe_rows': len(set(final_keys) - set(acknowledged) - {f'seed-{n}' for n in range(1,101)}),
        'rw_service_ready_targets': targets,
        'limitations': ['single kind node on one host', 'Pod deletion; not host loss or network partition',
                       'short sampled client probe; not a production RTO/SLA',
                       'asynchronous replication; this run does not guarantee zero data loss']})
    print(f'자동 전환 검증 완료: {old_primary} -> {new_primary}; 새 primary 쓰기 확인', flush=True)
    verify_rejoined()


def verify_rejoined():
    for _ in range(120):
        status = cluster()['status']
        if status.get('readyInstances') == 3 and status.get('phase') == 'Cluster in healthy state':
            break
        time.sleep(1)
    else:
        raise RuntimeError('3개 인스턴스 정상 복귀 시간 초과')
    states = []
    pods = json.loads(kube('-n', 'pgops-lab', 'get', 'pods', '-l', 'cnpg.io/cluster=pgops', '-o', 'json').stdout)['items']
    for pod in pods:
        if pod['status']['phase'] != 'Running':
            continue
        name = pod['metadata']['name']
        result = kube('-n', 'pgops-lab', 'exec', name, '--', 'psql', '-U', 'postgres', '-d', 'app', '-Atc',
                      "SELECT json_build_object('in_recovery',pg_is_in_recovery(),'rows',(SELECT count(*) FROM public.failover_events));")
        states.append({'pod': name, **json.loads(result.stdout)})
    evidence = json.loads((DEST / 'failover.json').read_text())
    expected_rows = evidence['rows_after_failover']
    assert len(states) == 3 and all(x['rows'] == expected_rows for x in states)
    assert sum(not x['in_recovery'] for x in states) == 1
    assert next(x for x in states if x['pod'] == evidence['old_primary'])['in_recovery']
    save(DEST / 'post-recovery.json', {'run_at_utc': now(), 'phase': status['phase'],
        'current_primary': status['currentPrimary'], 'ready_instances': status['readyInstances'], 'instances': states})
    print('이전 primary의 replica 재합류와 3개 인스턴스 데이터 일치 확인', flush=True)


if __name__ == '__main__':
    assert_local()
    task = sys.argv[1] if len(sys.argv) > 1 else 'install'
    if task == 'install':
        install()
    elif task == 'failover':
        failover()
    elif task == 'verify':
        verify_rejoined()
    else:
        raise SystemExit('usage: cnpg.py install|failover|verify')
