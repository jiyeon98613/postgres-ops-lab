# 정기 백업의 복구 검증

## 목적

백업 파일이 생성된다는 사실과 복구할 수 있다는 사실을 구분합니다. 완료된 운영 백업을 새 PostgreSQL에 실제로 복원하고, 백업에 들어 있는 행과 복원된 행이 일치하는지 확인합니다.

## 절차

1. 운영 백업 파일을 Git 밖의 권한 0700 디렉터리에 복사합니다. 복사 전후 파일 크기·수정 시각이 달라지면 진행하지 않습니다.
2. 별도 데이터 디렉터리와 Unix socket을 가진 PostgreSQL을 실행합니다. 운영 PostgreSQL 설정과 데이터 디렉터리는 수정하지 않습니다.
3. `pg_restore --no-owner --no-acl --single-transaction --exit-on-error`로 빈 DB에 복원합니다.
4. `pg_restore --data-only`의 COPY 데이터를 테이블별로 읽어 각 행의 SHA-256을 계산합니다. 행 해시를 정렬해 다시 해시하므로 행 순서와 무관하게 값·중복 개수를 비교할 수 있습니다.
5. 복원 DB에서 같은 열을 COPY로 읽어 비교합니다. TOC의 TABLE DATA 개수와 검증한 테이블 개수도 대조하여 누락을 확인합니다.
6. 백업의 `setval`과 복원 시퀀스의 `last_value`, `is_called`를 비교하고, 유효하지 않은 인덱스와 미검증 제약조건을 확인합니다.
7. 추가 DB에서 `staging.change_items`를 삭제하고 존재하지 않음을 확인합니다. 같은 백업을 또 다른 빈 DB에 전체 복원해 모든 테이블과 시퀀스를 다시 대조합니다.

COPY 비교는 UTC·ISO 날짜·동일한 부동소수점/bytea 출력 설정을 사용합니다. 원본 백업 세션의 출력 설정이 다르면 값이 같아도 문자열 비교가 실패할 수 있습니다. 이 경우 성공으로 처리하지 않고 설정을 확인합니다.

## 실행

```bash
python scripts/restore.py \
  --dump "$BACKUP_FILE" \
  --workspace "$LAB_WORKSPACE" \
  --pg-bin /usr/lib/postgresql/18/bin
python scripts/drop_restore.py \
  --workspace "$LAB_WORKSPACE" \
  --pg-bin /usr/lib/postgresql/18/bin
```

RLS 정책에서 특정 역할을 참조하면 해당 이름을 `--placeholder-role ROLE_NAME`으로 지정할 수 있습니다. 이 역할은 비밀번호와 로그인 권한이 없는 참조용 역할입니다. 운영 계정의 인증·권한을 복원했다는 뜻이 아닙니다.

## 결과 해석

`restore-result.json`은 복원 명령 성공뿐 아니라 테이블별 값 일치, 전체 검증 개수, 시퀀스 일치를 포함합니다. `drop-restore-result.json`은 삭제 확인과 복구 후 일치를 기록합니다. 상세 행 해시는 비공개 작업 디렉터리에만 남깁니다.

미검증 제약조건 수가 0보다 크면 해당 제약조건까지 데이터 검증을 마쳤다고 판단하지 않습니다. `NOT VALID` 제약조건의 추가 검증 여부는 원래 스키마의 의도를 확인해 결정합니다.

`restore_command_seconds`는 `pg_restore` 명령의 경과 시간입니다. 장애 감지, 서버 준비, 애플리케이션 재시작, 접속 전환까지 포함한 서비스 RTO가 아닙니다.

이 실습에서 돌아갈 수 있는 시점은 백업에 포함된 스냅샷입니다. 백업 이후 변경분을 복구하려면 별도의 WAL 아카이브와 물리 백업을 이용한 PITR 검증이 필요합니다. 백업 파일 수정 시각을 스냅샷 시각으로 단정하지 않습니다.

## 검증 범위의 한계

- `--no-owner --no-acl`을 사용하므로 데이터와 DB 객체 복원을 확인하며 운영 소유권·권한의 복구는 별도입니다.
- `pg_dump`에는 클러스터 전역 역할, PostgreSQL 설정, 외부 PDF·문서 파일이 모두 포함되지 않습니다.
- 현재 검증기는 COPY 테이블과 시퀀스를 지원합니다. Large Object 또는 materialized view 데이터가 있으면 추가 검증이 필요하므로 중단합니다.
- 원본 백업과 복원 데이터의 일치는 확인할 수 있지만 백업 생성 이후의 실시간 운영 DB와 같음을 의미하지 않습니다.
- RLS 정책이 참조하는 역할을 만들어도 실제 애플리케이션 계정의 로그인과 업무 동작을 검증한 것은 아닙니다.

참고: [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html), [SQL Dump](https://www.postgresql.org/docs/18/backup-dump.html)
