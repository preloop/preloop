-- Read-only role for the postgres-readonly MCP server (DBHub).
--
-- Run as a superuser, or as a role that has CREATEROLE and is app_owner or a
-- member of it, connected to the target database. app_owner is the role that
-- owns the database and its tables:
--
--   psql -1 -v ON_ERROR_STOP=1 -v app_owner=app_owner -d app_db \
--     -f postgres-readonly-role.sql
--
-- -1 runs the file as one transaction, so an error leaves nothing behind.
-- psql sets DBNAME to the database you connected to. Then set the password
-- with \password agent_readonly (psql sends a hash, not the plain text) and
-- run verify-readonly-postgres.sh as agent_readonly.

CREATE ROLE agent_readonly
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 5;

-- The only privileges the role gets: connect, see the schema, read tables.
-- Repeat the last three statements for each schema the agent may read.
GRANT CONNECT ON DATABASE :"DBNAME" TO agent_readonly;
GRANT USAGE ON SCHEMA public TO agent_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_readonly;
-- Tables app_owner creates later are readable too.
ALTER DEFAULT PRIVILEGES FOR ROLE :"app_owner" IN SCHEMA public
  GRANT SELECT ON TABLES TO agent_readonly;

-- Session defaults. A session can change these with SET or BEGIN READ WRITE,
-- so they limit mistakes rather than enforce anything; the grants above are
-- what refuses writes to tables, sequences and schemas.
-- DBHub's query_timeout also sets statement_timeout for its queries.
ALTER ROLE agent_readonly SET default_transaction_read_only = on;
ALTER ROLE agent_readonly SET statement_timeout = '15s';
ALTER ROLE agent_readonly SET lock_timeout = '2s';
ALTER ROLE agent_readonly SET idle_in_transaction_session_timeout = '30s';

-- Three PUBLIC defaults reach this role as well. They apply to every role in
-- the database, so this file leaves them to you. Run the REVOKEs as a
-- superuser and grant back to the roles that need them:
--   * TEMPORARY on the database lets the role create temporary tables, which
--     last until the session ends.
--       REVOKE TEMPORARY ON DATABASE app_db FROM PUBLIC;
--   * EXECUTE on the large-object functions lets the role create large
--     objects, which persist. No grant to this role controls it.
--       REVOKE EXECUTE ON FUNCTION lo_create(oid), lo_creat(integer),
--         lo_from_bytea(oid, bytea) FROM PUBLIC;
--   * Before PostgreSQL 15, CREATE on schema public lets the role create
--     tables there.
--       REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- verify-readonly-postgres.sh reports all three.
