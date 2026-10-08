-- Register the vocab schema with WebAPI as a vocabulary-only source.
-- Idempotent: relies on source_key_unique and un_source_daimon constraints.
INSERT INTO webapi.source
  (source_name, source_key, source_connection, source_dialect, username, password, is_cache_enabled)
VALUES
  ('OMOP Vocabulary', 'VOCAB', :'jdbc_url', 'postgresql', :'db_user', :'db_password', false)
ON CONFLICT (source_key) DO UPDATE SET
  source_connection = EXCLUDED.source_connection,
  username = EXCLUDED.username,
  password = EXCLUDED.password,
  deleted_date = NULL;

-- daimon_type 1 = Vocabulary (SourceDaimon.DaimonType ordinal)
INSERT INTO webapi.source_daimon (source_id, daimon_type, table_qualifier, priority)
SELECT source_id, 1, 'vocab', 1 FROM webapi.source WHERE source_key = 'VOCAB'
ON CONFLICT (source_id, daimon_type) DO UPDATE SET
  table_qualifier = EXCLUDED.table_qualifier,
  priority = EXCLUDED.priority;
