-- 01_databases.sql
--
-- TraceMap's analytics database. Jaeger owns a SEPARATE `jaeger` database on
-- the same server, created and managed by its own storage backend.
--
-- M16: we never write to Jaeger's tables and never build views on them. Its
-- ClickHouse backend is alpha and free to change its schema underneath us; the
-- point of keeping two databases is that a Jaeger upgrade cannot break the
-- analytics tier.
CREATE DATABASE IF NOT EXISTS tracemap;

-- Provisioned empty for Jaeger's own backend to populate when
-- STORAGE_PROFILE=clickhouse. Creating the database is not the same as building
-- on it: Jaeger owns every table inside, and nothing in TraceMap reads them.
CREATE DATABASE IF NOT EXISTS jaeger;
