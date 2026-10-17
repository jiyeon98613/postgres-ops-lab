# 05. CloudNativePG 자동 장애조치

## 목적

Kubernetes에서 primary Pod를 삭제했을 때 replica가 자동 승격하고, 쓰기용 서비스가 새 primary로 연결되는지 확인했습니다. 전환 전후의 데이터와 기존 primary의 재합류까지 점검했습니다.

rootless Podman과 kind로 로컬 Kubernetes 노드 1개를 만들고 PostgreSQL 인스턴스 3개를 배치했습니다. kind 0.30.0, Kubernetes 1.34.0, CloudNativePG 1.28.4, PostgreSQL 18.6을 사용했습니다. 데이터는 장애조치 확인용으로 생성했으며 운영 데이터를 사용하지 않았습니다.

## 실행한 명령

도구와 kubeconfig는 Git 저장소 밖에 보관했습니다. 도구 파일은 공식 배포처의 SHA-256으로 검증했고, 전용 kubeconfig와 Podman 설정을 지정했습니다.

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

[장애조치 스크립트](../../scripts/cnpg.py)는 전용 context와 `127.0.0.1` API 주소를 확인한 뒤 실행하도록 했습니다. 인스턴스 3개가 준비되고 초기 데이터 100행이 각 replica에 반영된 상태에서 현재 primary Pod를 강제로 삭제했습니다.

이후 쓰기용 `pgops-rw` 서비스로 주기적으로 접속해, 새 primary에서 쓰기가 성공하는지 확인했습니다. 수동 승격 명령은 실행하지 않았습니다. 클라이언트가 성공 응답을 받은 쓰기와 응답을 확인하지 못한 쓰기를 나눠 전환 후 데이터와 대조했습니다.

결과를 보관한 뒤 실습 클러스터를 삭제했습니다.

```bash
KIND_EXPERIMENTAL_PROVIDER=podman \
CONTAINERS_CONF_OVERRIDE="$PWD/kubernetes/podman.conf" \
  "$PGOPS_KUBE_WORKSPACE/bin/kind" delete cluster --name pgops-lab \
  --kubeconfig "$PGOPS_KUBE_WORKSPACE/kubeconfig"
```

## 결과

| 확인 항목 | 결과 |
| --- | --- |
| 기존 primary → 새 primary | `pgops-1` → `pgops-2` |
| 삭제 요청부터 새 primary 쓰기 성공 확인 | 4.000초 |
| 전환 중 관측한 쓰기 실패 | 3회 |
| 초기 100행 | 모두 보존 |
| 성공 응답을 받은 추가 쓰기 | 2행, 누락 0행 |
| 응답을 확인하지 못했으나 실제 반영된 행 | 0행 |
| 쓰기 서비스 연결 대상 | `pgops-2` |
| 안정화 후 데이터 | 세 인스턴스 모두 102행 |
| 이전 primary `pgops-1` | replica로 재합류 |

결과 파일: [설치 정보](../../results/cloudnativepg/installation.json), [장애조치 관측](../../results/cloudnativepg/failover.json), [재합류 확인](../../results/cloudnativepg/post-recovery.json)

## 막혔던 점

primary 전환 중 쓰기 요청이 3회 실패했습니다. 장애조치 과정의 실패로 기록하고, 같은 쓰기 서비스에서 새 primary로의 쓰기가 성공할 때까지 관측했습니다. primary 이름이 바뀌는 시점만으로 복구 완료를 판단하지 않고 서비스 연결 대상과 실제 쓰기 성공을 함께 확인했습니다.

쓰기 응답을 받지 못한 경우에는 실제 저장 여부가 불확실할 수 있어, 요청별 키를 전환 후 테이블과 다시 대조했습니다. 이번 실행에서는 성공 응답을 받은 쓰기의 누락과 응답 없이 반영된 행이 모두 0개였습니다.

## 배운 점

자동 장애조치를 확인할 때는 승격 여부와 함께 애플리케이션이 쓰는 접속 경로에서도 쓰기가 되는지 확인해야 했습니다. 이전 primary가 replica로 다시 합류하고 세 인스턴스의 데이터가 같아지는 것까지 검사했습니다.

4초는 이번 Pod 삭제와 주기적인 접속 검사에서 얻은 관측값입니다. 단일 노드에서 실행했으므로 물리 서버 장애나 네트워크 분단은 검증하지 못했습니다. 또한 비동기 복제에서 이번 쓰기가 보존됐다는 결과만으로 모든 장애에서 데이터 손실이 없다고 보장할 수는 없습니다.

참고: [kind rootless 실행](https://kind.sigs.k8s.io/docs/user/rootless/), [CloudNativePG 설치](https://cloudnative-pg.io/docs/1.28/installation_upgrade/), [자동 failover](https://cloudnative-pg.io/docs/1.28/failover/)
