# PostgreSQL 운영 검증

문서 수집·청구 애플리케이션의 정기 백업과 실제 조회 SQL을 대상으로 복구 가능성과 기존 인덱스의 효과를 확인합니다.

| 항목 | 검증 결과 |
| --- | --- |
| 정기 백업 복구 | 62개 테이블·32,117행을 복원하고 원본 백업과 전체 값 일치 확인 |
| 테이블 삭제 복구 | 수집 이력 2,692행이 있는 테이블 삭제 후 새 DB 복구 및 전체 데이터 일치 확인 |
| 기존 인덱스 비교 | 실제 이력 2,692행·수집 묶음 27행 기반, 원본 복원본 4,800회·마스킹 복제본 4,800회 측정 |
| 권한 분리 | 실제 로그인 및 허용·거부 17개 검사 통과 |
| 스트리밍 복제 | replica 중단 중 추가한 검증용 1,000행 추격, 업무 테이블 62개 일치 |
| 특정 시점 복구 | 물리 백업·WAL로 삭제 직전 복구, 목표 이전 변경 포함·이후 변경 제외 |
| Kubernetes 자동 장애조치 | primary Pod 삭제 후 새 primary 쓰기 확인, 세 인스턴스 데이터 일치 |

2026-10-17에 완료된 정기 백업으로 실행했습니다. 62개 테이블에는 Alembic 버전 관리 테이블 1개가 포함됩니다. 성능은 원본 값을 유지한 비공개 복원본에서 측정하고, 마스킹 복제본에서도 교차 검증했습니다. 운영 DB의 인덱스·계정·설정은 변경하지 않았습니다. [검증 결과](results/README.md)

## 구성

- [백업·복구 검증](docs/backup-restore.md)
- [기존 SQL·인덱스 비교](docs/query-tuning.md)
- [계정·권한 검증](docs/roles.md)
- [스트리밍 복제·PITR](docs/replication-pitr.md)
- [CloudNativePG 장애조치](docs/cloudnativepg.md)
- [공개 결과의 범위](results/README.md)

실행에는 PostgreSQL 18 서버 도구와 Python 3.11 이상, `psycopg`가 필요합니다. 서버에 이미 설치된 도구를 사용하거나 [Containerfile](Containerfile)로 격리된 검증 이미지를 만들 수 있습니다.

```bash
python -m pip install -r requirements.txt
python scripts/restore.py \
  --dump "$BACKUP_FILE" \
  --workspace "$LAB_WORKSPACE" \
  --pg-bin /usr/lib/postgresql/18/bin
python scripts/drop_restore.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin
python scripts/tuning.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin
python scripts/tuning.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin --data-mode unmodified
python scripts/roles.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin
python scripts/physical.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin
```

`BACKUP_FILE`은 완료된 custom-format 백업 경로입니다. `LAB_WORKSPACE`는 Git 저장소 밖의 새 디렉터리여야 하며, 짧은 절대 경로를 사용합니다. 예를 들어 `$HOME/pgops-private/run-001`을 지정할 수 있습니다. 기존 실행 결과를 덮어쓰지 않으므로 재실행할 때는 새 디렉터리를 사용합니다.

복구 인스턴스는 TCP를 열지 않고, 권한 0700 디렉터리 안의 Unix socket과 별도 비밀번호를 사용합니다. 운영 접속 설정이나 환경의 `PGHOST`를 사용하지 않습니다. 각 검증이 끝나면 해당 실습 인스턴스를 종료합니다.

운영 복제본에는 개인정보가 남아 있습니다. 원본 백업·복원 DB·마스킹 키·작업 로그·SQL 매개변수는 접근이 제한된 작업 디렉터리에 보관합니다. 성능 비교용으로 식별값을 치환하더라도 데이터를 공개하지 않습니다. 저장소에 올리는 파일은 코드, 절차, 검토한 집계 결과뿐입니다.

## 코드 검증

실측에 사용한 원본 데이터는 비공개이므로 외부에서 동일한 수치를 완전히 재현할 수는 없습니다. 공개 코드는 절차를 재실행할 수 있게 제공하고, 다음 검증용 데이터는 코드의 동작을 확인하는 용도로 사용합니다. 다음 명령은 운영 DB에 연결하지 않습니다. 일회용 데이터로 복구 비교, 시퀀스, 외래키, 역할을 참조하는 RLS 정책, 인덱스 비교, 권한 거부, 출력 정제를 확인합니다.

```bash
podman build -t localhost/postgres-ops-check:18.6 -f Containerfile .
podman run --rm --network none --memory 512m --cpus 1 \
  --tmpfs /private:rw,mode=1777 \
  --entrypoint python3 localhost/postgres-ops-check:18.6 \
  /opt/lab/scripts/check_restore.py \
  --workspace /private/check --pg-bin /usr/lib/postgresql/18/bin
```

물리 복구 코드 검증까지 포함하려면 컨테이너 메모리를 `1g`로 설정하고 마지막 인자에 `--physical`을 추가합니다. 운영 DB에는 WAL 아카이브가 설정되어 있지 않습니다. 실습의 PITR 성공은 운영 PITR 구성이 완료되었다는 뜻이 아닙니다.
