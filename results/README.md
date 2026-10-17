# 2026-10-17 검증 결과

| 파일 | 내용 |
| --- | --- |
| [source-context.json](source-context.json) | 배포 코드·조회 함수·기존 인덱스·데이터 규모 확인 |
| [restore.json](restore.json) | 실제 정기 백업의 62개 테이블·32,117행 일치 검증 |
| [drop-restore.json](drop-restore.json) | 테이블 삭제 후 전체 논리 백업 복구 |
| [tuning.json](tuning.json) | 원본 복원본에서 기존 인덱스 유무 총 4,800회 측정 및 대표 실행계획 |
| [tuning-masked.json](tuning-masked.json) | 마스킹 복제본 4,800회 교차 검증 집계 |
| [roles.json](roles.json) | 실제 로그인과 역할별 17개 허용·거부 검사 |
| [physical.json](physical.json) | replica 중단·추격과 물리 백업·WAL 기반 PITR |

운영 데이터는 보호된 서버의 별도 인스턴스에서 처리했습니다. 성능은 원본 복원본과 식별값을 치환한 복제본에서 각각 측정했습니다. 두 데이터 모두 공개하지 않습니다. 운영 인덱스·계정·PostgreSQL 설정은 변경하지 않았습니다.

백업 파일, 접속 정보, 행 값, 개별 행 해시, SQL 매개변수, 원본 실행계획과 로그는 포함하지 않습니다. 공개 실행계획에는 조건식·상수·정렬 키를 제거했습니다. 코드 검증용 데이터의 측정값을 이 결과에 섞지 않았습니다.

Kubernetes 실습은 환자 데이터 없이 장애조치 확인용 행으로 실행했습니다. [설치 정보](cloudnativepg/installation.json), [장애조치 관측](cloudnativepg/failover.json), [재합류 확인](cloudnativepg/post-recovery.json)
