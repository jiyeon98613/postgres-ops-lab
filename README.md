# PostgreSQL 운영 검증

문서 수집·청구 애플리케이션의 정기 백업과 실제 조회 SQL을 대상으로 복구 가능성과 기존 인덱스의 효과를 확인합니다.

| 항목 | 검증 방법 | 현재 상태 |
| --- | --- | --- |
| 논리 백업 복구 | 별도 PostgreSQL에 복원하고 백업 안의 모든 COPY 데이터·시퀀스와 대조 | 코드 검증 완료, 운영 백업 실행 대기 |
| 테이블 삭제 복구 | 격리된 DB에서 수집 이력 테이블 삭제 후 새 DB로 전체 백업 복원 | 코드 검증 완료, 운영 백업 실행 대기 |
| 기존 인덱스 비교 | 실제 수집 이력 SQL에 기존 복합 인덱스가 있을 때와 없을 때 비교 | 코드 검증 완료, 운영 데이터 측정 대기 |
| 권한 분리 | 읽기·앱·스키마 소유자 계정으로 실제 로그인 후 허용·거부 확인 | 코드 검증 완료, 최종 실습 실행 대기 |

운영 데이터로 측정한 결과가 아직 없으므로 성능 개선 수치나 운영 백업 복구 성공을 제시하지 않습니다. 코드 검증에 사용하는 작은 데이터셋의 수치는 프로젝트 성과에 포함하지 않습니다.

## 구성

- [백업·복구 검증](docs/backup-restore.md)
- [기존 SQL·인덱스 비교](docs/query-tuning.md)
- [계정·권한 검증](docs/roles.md)
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
python scripts/roles.py --workspace "$LAB_WORKSPACE" --pg-bin /usr/lib/postgresql/18/bin
```

`BACKUP_FILE`은 완료된 custom-format 백업 경로입니다. `LAB_WORKSPACE`는 Git 저장소 밖의 새 디렉터리여야 하며, 짧은 절대 경로를 사용합니다. 예를 들어 `$HOME/pgops-private/run-001`을 지정할 수 있습니다. 기존 실행 결과를 덮어쓰지 않으므로 재실행할 때는 새 디렉터리를 사용합니다.

복구 인스턴스는 TCP를 열지 않고, 권한 0700 디렉터리 안의 Unix socket과 별도 비밀번호를 사용합니다. 운영 접속 설정이나 환경의 `PGHOST`를 사용하지 않습니다. 각 검증이 끝나면 해당 실습 인스턴스를 종료합니다.

운영 복제본에는 개인정보가 남아 있습니다. 원본 백업·복원 DB·마스킹 키·작업 로그·SQL 매개변수는 접근이 제한된 작업 디렉터리에 보관합니다. 성능 비교용으로 식별값을 치환하더라도 데이터를 공개하지 않습니다. 저장소에 올리는 파일은 코드, 절차, 검토한 집계 결과뿐입니다.

## 코드 검증

다음 명령은 운영 DB에 연결하지 않습니다. 일회용 데이터로 복구 비교, 시퀀스, 외래키, 역할을 참조하는 RLS 정책, 인덱스 비교, 권한 거부, 출력 정제를 확인합니다.

```bash
podman build -t localhost/postgres-ops-check:18.6 -f Containerfile .
podman run --rm --network none --memory 512m --cpus 1 \
  --tmpfs /private:rw,mode=1777 \
  --entrypoint python3 localhost/postgres-ops-check:18.6 \
  /opt/lab/scripts/check_restore.py \
  --workspace /private/check --pg-bin /usr/lib/postgresql/18/bin
```

스트리밍 복제, WAL 기반 PITR, Kubernetes 자동 장애조치는 논리 복구와 별도 검증 대상입니다. 위 코드의 통과를 해당 기능의 구현·검증 완료로 해석하지 않습니다.
