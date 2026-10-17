# PostgreSQL 운영 실습

문서 수집·청구 시스템의 정기 백업과 실제 조회 SQL을 바탕으로 백업 복구, 기존 인덱스의 효과, 계정별 권한을 검증했습니다. 이어서 스트리밍 복제, 특정 시점 복구(PITR), CloudNativePG 자동 장애조치를 실습했습니다.

## 실습 기록

각 실습의 목적, 실행한 명령, 결과, 막혔던 점, 배운 점을 기록했습니다. 검증일은 2026-10-17입니다.

| 순서 | 실습 | 주요 결과 |
| --- | --- | --- |
| 1 | [정기 백업 복구·테이블 삭제 복구](labs/01-backup-restore/README.md) | 62개 테이블·32,117행을 복원하고 백업과 값 일치 확인 |
| 2 | [실제 조회 SQL의 기존 인덱스 비교](labs/02-query-tuning/README.md) | 파일 ID 1개 조회에서 평균 실행 시간의 중앙값 0.6173ms → 0.1044ms |
| 3 | [읽기 전용·애플리케이션·소유자 권한 분리](labs/03-roles/README.md) | 실제 계정 로그인과 허용·거부 검사 17개 통과 |
| 4 | [스트리밍 복제·PITR](labs/04-replication-pitr/README.md) | replica 중단 중 추가한 1,000행 반영, 삭제 직전 시점으로 복구 |
| 5 | [CloudNativePG 자동 장애조치](labs/05-cloudnativepg/README.md) | primary Pod 삭제 후 4초에 새 primary 쓰기 확인, 이전 primary 재합류 |

논리 복구와 성능 비교에는 실제 정기 백업을 사용했습니다. 성능은 원본 값을 유지한 비공개 복원본에서 측정하고, 식별값을 치환한 복제본에서도 따로 비교했습니다. 62개 테이블에는 Alembic 버전 관리 테이블 1개가 포함됩니다. [실측 결과 파일](results/README.md)

운영 DB의 인덱스·계정·설정은 변경하지 않았습니다. 복구와 부하 실험은 별도 인스턴스에서 진행했고, Kubernetes 실습에는 장애조치 확인용 데이터만 사용했습니다.

## 실행 환경과 순서

- 복구·성능·권한·복제: PostgreSQL 18.6, Python 3.11 이상, `psycopg`
- Kubernetes: Linux amd64, rootless Podman, kind 0.30.0, Kubernetes 1.34.0, CloudNativePG 1.28.4

명령은 저장소 루트를 기준으로 작성했습니다. 아래 두 경로는 사용할 백업 파일과 새 실습 디렉터리로 바꿉니다. 작업 디렉터리는 Git 저장소 밖의 짧은 절대 경로를 사용합니다.

```bash
python -m pip install -r requirements.txt

export BACKUP_FILE="/absolute/path/to/completed.dump"
export LAB_WORKSPACE="$HOME/pgops-private/run-001"
```

1~4번은 같은 `LAB_WORKSPACE`에서 순서대로 실행합니다. 3번 권한 실습은 2번의 `masked` 모드가 만든 DB를 사용합니다. 4번은 실습 DB에서 테이블을 삭제하므로 마지막에 실행합니다. 처음부터 다시 실습할 때는 새 작업 디렉터리를 지정합니다. 5번은 별도의 Kubernetes 환경에서 독립적으로 실행합니다.

1~4번의 실습 PostgreSQL은 TCP 포트를 열지 않고, 권한 0700 작업 디렉터리의 Unix socket과 별도 비밀번호를 사용합니다. 각 스크립트가 끝나면 실습 인스턴스를 종료합니다.

## 공개 자료의 범위

백업 원본, 복원 DB, 마스킹 키, SQL 매개변수, 원본 로그는 비공개 작업 디렉터리에 보관했습니다. 이 저장소에는 코드, 실행 절차, 식별값을 제외한 집계 결과만 올렸습니다. 원본 데이터가 비공개이므로 외부에서 같은 측정값을 재현하는 데에는 제한이 있습니다.

실습에서 검증한 PITR과 권한 설계는 운영 적용과 별개입니다. 확인 당시 운영 DB의 `archive_mode`는 `off`였습니다.

## 코드 동작 확인

공개 코드의 동작은 일회용 데이터로도 확인할 수 있습니다. 다음 명령은 운영 DB에 연결하지 않으며, 복구 후 값 비교, 시퀀스, 외래키, RLS 역할 참조, 인덱스 비교, 권한 거부와 출력 정제를 검사합니다. 이 데이터의 측정값은 실측 결과에 포함하지 않았습니다.

```bash
podman build -t localhost/postgres-ops-check:18.6 -f Containerfile .
podman run --rm --network none --memory 512m --cpus 1 \
  --tmpfs /private:rw,mode=1777 \
  --entrypoint python3 localhost/postgres-ops-check:18.6 \
  /opt/lab/scripts/check_restore.py \
  --workspace /private/check --pg-bin /usr/lib/postgresql/18/bin
```

물리 복구 코드까지 검사하려면 컨테이너 메모리를 `1g`로 설정하고 마지막 인자에 `--physical`을 추가합니다.
