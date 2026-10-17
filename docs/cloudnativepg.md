# CloudNativePG 자동 장애조치

## 목적과 환경

로컬 Kubernetes에서 primary Pod가 사라졌을 때 replica가 자동 승격하고 쓰기 서비스가 전환되는지 확인합니다. 운영 DB를 Kubernetes로 옮기지 않으며, 장애조치 확인용 데이터만 사용합니다.

Linux amd64, rootless Podman, kind 0.30.0, Kubernetes 1.34.0, CloudNativePG 1.28.4, PostgreSQL 18.6 환경입니다. 단일 Kubernetes 노드에 DB 인스턴스 3개를 배치했습니다. 실제 물리 서버 장애나 네트워크 분단을 재현한 구성은 아닙니다.

## 실행

도구와 kubeconfig는 Git 밖의 비공개 디렉터리에 저장합니다. 도구는 공식 배포처의 SHA-256으로 확인합니다. 사용자 전역 kubeconfig와 Podman 설정은 변경하지 않습니다.

```bash
export PGOPS_KUBE_WORKSPACE="$HOME/pgops-kube-private/run-001"
python scripts/get-kube-tools.py

KIND_EXPERIMENTAL_PROVIDER=podman \
CONTAINERS_CONF_OVERRIDE="$PWD/kubernetes/podman.conf" \
KUBECONFIG="$PGOPS_KUBE_WORKSPACE/kubeconfig" \
systemd-run --user --scope -p Delegate=yes \
  "$PGOPS_KUBE_WORKSPACE/bin/kind" create cluster --name pgops-lab \
  --image docker.io/kindest/node:v1.34.0 \
  --config "$PWD/kubernetes/kind.yaml" \
  --kubeconfig "$PGOPS_KUBE_WORKSPACE/kubeconfig" --wait 120s

python scripts/cnpg.py install
python scripts/cnpg.py failover
```

스크립트는 전용 context와 `127.0.0.1` API endpoint를 확인합니다. 3개 인스턴스가 정상이고 초기 데이터 100행이 각 replica에 반영된 뒤 현재 primary Pod 하나를 강제로 삭제합니다. 이후 쓰기용 `pgops-rw` 서비스에 접속해 새 primary에서 쓰기가 성공하는지 검사합니다.

검증용 쓰기와 recovery 상태 조회는 하나의 SQL 문으로 실행합니다. 클라이언트가 성공 응답을 확인한 쓰기가 사라졌는지 확인하고, 응답을 확인하지 못했지만 실제로 들어간 행도 별도로 집계합니다.

## 결과

| 항목 | 관측값 |
| --- | --- |
| 기존 primary → 새 primary | `pgops-1` → `pgops-2` |
| 삭제 요청부터 새 primary 쓰기 성공 확인 | 4.000초 |
| 관측한 쓰기 실패 | 3회 |
| 초기 100행 | 모두 보존 |
| 성공 응답을 확인한 추가 쓰기 | 2행, 누락 0행 |
| 응답 미확인·실제 반영 행 | 0행 |
| 안정화 후 각 인스턴스 | 모두 102행 |
| 이전 primary | replica로 재합류 |

수동 승격 명령을 실행하지 않았으며 CloudNativePG가 자동으로 전환했습니다. 4초는 Pod 삭제 요청과 주기적인 접속 확인을 포함한 이 실험의 관측값입니다. 운영 RTO나 SLA가 아닙니다. 비동기 복제이므로 이번 쓰기 보존 결과만으로 모든 장애에서 데이터 손실이 없다고 보장할 수 없습니다.

근거: [설치 정보](../results/cloudnativepg/installation.json), [장애조치 관측](../results/cloudnativepg/failover.json), [재합류 확인](../results/cloudnativepg/post-recovery.json).

## 종료

결과를 보관한 뒤 이번에 만든 로컬 실습 클러스터를 삭제합니다. 내부 검증용 DB 데이터도 함께 삭제됩니다.

```bash
KIND_EXPERIMENTAL_PROVIDER=podman \
CONTAINERS_CONF_OVERRIDE="$PWD/kubernetes/podman.conf" \
  "$PGOPS_KUBE_WORKSPACE/bin/kind" delete cluster --name pgops-lab \
  --kubeconfig "$PGOPS_KUBE_WORKSPACE/kubeconfig"
```

참고: [kind rootless 실행](https://kind.sigs.k8s.io/docs/user/rootless/), [CloudNativePG 설치](https://cloudnative-pg.io/docs/1.28/installation_upgrade/), [자동 failover](https://cloudnative-pg.io/docs/1.28/failover/).
