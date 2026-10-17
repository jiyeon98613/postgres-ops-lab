# 검증 결과 파일

2026-10-17에 실행한 실습의 결과입니다. 실행 배경과 해석은 각 실습 README에, 측정값과 검사 결과는 JSON 파일에 기록했습니다.

| 실습 | 결과 파일 | 확인한 내용 |
| --- | --- | --- |
| [백업·복구](../labs/01-backup-restore/README.md) | [restore.json](restore.json) | 정기 백업의 62개 테이블·32,117행 복원 및 값 일치 |
| [테이블 삭제 복구](../labs/01-backup-restore/README.md) | [drop-restore.json](drop-restore.json) | 테이블 삭제 후 전체 논리 백업 복구 |
| [기존 인덱스 비교](../labs/02-query-tuning/README.md) | [source-context.json](source-context.json) | 배포 코드·조회 함수·기존 인덱스·데이터 규모 |
| [기존 인덱스 비교](../labs/02-query-tuning/README.md) | [tuning.json](tuning.json) | 원본 복원본에서 인덱스 유무 4,800회 측정·대표 실행계획 |
| [기존 인덱스 비교](../labs/02-query-tuning/README.md) | [tuning-masked.json](tuning-masked.json) | 마스킹 복제본의 별도 4,800회 측정 |
| [권한 분리](../labs/03-roles/README.md) | [roles.json](roles.json) | 계정별 로그인과 허용·거부 검사 17개 |
| [복제·PITR](../labs/04-replication-pitr/README.md) | [physical.json](physical.json) | replica 중단 후 반영·테이블 삭제 직전 복구 |
| [자동 장애조치](../labs/05-cloudnativepg/README.md) | [installation.json](cloudnativepg/installation.json) | 로컬 Kubernetes와 PostgreSQL 설치 상태 |
| [자동 장애조치](../labs/05-cloudnativepg/README.md) | [failover.json](cloudnativepg/failover.json) | primary 전환·쓰기 실패와 재개·데이터 보존 |
| [자동 장애조치](../labs/05-cloudnativepg/README.md) | [post-recovery.json](cloudnativepg/post-recovery.json) | 이전 primary의 replica 재합류·세 인스턴스 데이터 일치 |

운영 데이터는 보호된 서버의 별도 인스턴스에서 처리했습니다. 성능은 원본 복원본과 식별값을 치환한 복제본에서 각각 측정했습니다. Kubernetes에는 별도의 장애조치 확인용 데이터를 사용했습니다. 코드 동작 검사용 일회용 데이터의 측정값은 이 결과에 포함하지 않았습니다.

백업 파일, 접속 정보, 행 값, 개별 행 해시, SQL 매개변수, 원본 실행계획과 로그는 공개하지 않았습니다. 공개 실행계획에는 조건식·상수·정렬 키를 제외했습니다. 원본 복원본과 마스킹 복제본의 데이터도 비공개로 보관했습니다.
