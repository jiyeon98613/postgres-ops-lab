-- 기존 프로젝트의 a8d4f2c61b90 migration에 정의된 인덱스입니다.
-- 운영 DB에서 실행하지 않습니다. 복제 실습 DB에서만 비교합니다.
CREATE INDEX ix_change_items_vendor_business_created
ON staging.change_items (vendor_id, business_key, created_at);
