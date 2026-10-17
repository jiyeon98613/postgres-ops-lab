-- document_discovery._previous_items()에서 사용하는 전체 행 조회입니다.
-- 식별자에 실제 Drive ID 또는 운영 계정 ID를 적지 않습니다.
SELECT i.*, s.*
FROM staging.change_items AS i
JOIN staging.change_sets AS s
  ON s.change_set_id = i.change_set_id
 AND s.vendor_id = i.vendor_id
WHERE i.vendor_id = %(vendor_id)s
  AND s.change_type = 'PATIENT_DOCUMENT_DISCOVERY'
  AND s.change_set_id <> %(current_set_id)s
  AND i.business_key IN (%(business_key_1)s, %(business_key_2)s)
  AND i.deleted_at IS NULL
  AND s.deleted_at IS NULL
ORDER BY s.created_at DESC, i.created_at DESC;
