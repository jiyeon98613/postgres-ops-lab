# 실제 백업 복제본의 스트리밍 복제와 PITR

## 환경과 범위

정기 논리 백업을 복원한 비공개 PostgreSQL 18.6 인스턴스를 실습 primary로 사용합니다. replica와 복구 인스턴스는 별도 데이터 디렉터리·Unix socket을 가집니다. 같은 Linux 서버의 서로 다른 프로세스이며, TCP 포트를 열지 않습니다.

운영 PostgreSQL의 `archive_mode`는 `off`입니다. WAL 아카이브 설정, 복제 계정, 슬롯, 장애 주입은 실습 인스턴스에만 적용합니다. 이 실험은 운영 복제 구성이나 서로 다른 서버의 장애 내성을 증명하지 않습니다.

## 스트리밍 복제

1. 로그인·복제 권한을 가진 전용 계정을 만듭니다. 슈퍼유저 권한은 주지 않습니다.
2. `pg_basebackup -X stream -c fast -R -C -S pgops_replica`로 replica를 구성합니다.
3. replica가 recovery 상태이고 초기 검증 행을 받은 것을 확인합니다.
4. replica를 종료하고 primary의 별도 검증 테이블에 1,000행을 추가합니다.
5. replica를 다시 시작해 마지막 변경의 WAL 위치까지 재생했는지 확인합니다.
6. 검증 행 개수뿐 아니라 백업에 있는 62개 테이블 전체를 다시 대조합니다.
7. 실습 replica를 종료하고 복제 슬롯을 제거해 불필요한 WAL 보관을 남기지 않습니다.

관측 결과: 중단 중 WAL 차이 154,256바이트, 재시작 요청부터 목표 WAL 재생 확인까지 0.1432초, 검증 행 1,001개 및 62개 테이블 일치. 변경량이 작고 같은 서버에서 실행한 결과이며 운영 복구 시간으로 일반화하지 않습니다.

## 특정 시점 복구

1. 실습 primary에서 WAL 아카이브를 켜고 별도 물리 백업을 생성합니다.
2. 물리 백업이 끝난 뒤 검증 행을 추가하고 목표 시각을 기록합니다.
3. `staging.change_items`를 삭제하고 목표 시각 이후의 검증 행도 추가합니다.
4. 필요한 WAL이 아카이브된 것을 확인합니다.
5. 물리 백업에 `recovery.signal`, `restore_command`, `recovery_target_time`을 설정합니다.
6. 목표 지점에서 재생이 일시 정지했는지 확인한 뒤 복구를 완료하고 쓰기 가능한 상태로 전환합니다.
7. 삭제된 테이블이 복구되었고, 목표 이전 행은 포함되며 이후 행은 제외되는지 확인합니다. 전체 테이블도 원본 논리 백업과 다시 대조합니다.

목표 시각은 `2026-10-17T04:23:17.053583+00:00`이었습니다. 물리 백업 이후·목표 이전에 쓴 행은 복구되었고 목표 이후 행은 제외되었습니다. 삭제된 수집 이력 테이블과 62개 테이블의 값이 일치했으며, 복구 완료 후 추가 쓰기도 성공했습니다. WAL 아카이브 성공 7개, 실패 0개였습니다.

기동 요청부터 목표 지점 일시 정지 확인까지 0.8506초였습니다. 장애 감지, 백업 확보, 복구 환경 준비, 애플리케이션 전환은 포함하지 않은 측정입니다.

## 실행

앞의 논리 복구·성능·권한 검증을 끝낸 작업 디렉터리에서 마지막으로 실행합니다. 삭제 실험 때문에 해당 작업 디렉터리의 **실습 primary DB**는 더 이상 최초 복원 상태가 아닙니다. 원본 백업은 보존되고 복구된 인스턴스는 별도 디렉터리에 남습니다.

```bash
python scripts/physical.py \
  --workspace "$LAB_WORKSPACE" \
  --pg-bin /usr/lib/postgresql/18/bin
```

## 검증 중 수정한 점

일회용 데이터로 검증할 때 목표 지점에서 일시 정지한 복구 DB를 조회하다가 `55P03` 잠금 오류가 발생했습니다. WAL 재생 중 DDL 잠금과 조회가 충돌할 수 있으므로, 최종 코드는 목표 도달을 확인하고 명시적으로 복구를 완료한 뒤 테이블을 검증합니다. 이 순서로 코드 검증과 실제 백업 검증을 통과했습니다. [Hot Standby의 조회 충돌](https://www.postgresql.org/docs/18/hot-standby.html), [recovery_target_action](https://www.postgresql.org/docs/18/runtime-config-wal.html)

근거: [물리 복구 결과](../results/physical.json). 참고: [연속 아카이브와 PITR](https://www.postgresql.org/docs/18/continuous-archiving.html), [스트리밍 복제](https://www.postgresql.org/docs/18/warm-standby.html).
