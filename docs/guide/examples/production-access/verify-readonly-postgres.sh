#!/usr/bin/env bash
# Connect as the read-only role and check that it can read and cannot write.
# The INSERT, UPDATE and DELETE probes use EXPLAIN without ANALYZE, which checks
# privileges and runs nothing; the CREATE TABLE probe runs in a transaction
# that is rolled back. A misconfigured role therefore still changes nothing.
#
#   PGHOST=db.example.com PGDATABASE=app_db PGUSER=agent_readonly \
#     bash verify-readonly-postgres.sh
#
# Connection settings come from the usual libpq variables (PGHOST, PGPORT,
# PGDATABASE, PGUSER) and the password from ~/.pgpass or PGPASSWORD. Nothing
# is stored in this file. The timeout check waits for statement_timeout.
set -euo pipefail

export PGUSER="${PGUSER:-agent_readonly}"
PSQL=(psql -X -A -t -q -v ON_ERROR_STOP=1)
failures=0
warnings=0
ok() { printf 'ok    %s\n' "$1"; }
warn() {
  printf 'warn  %s\n' "$1"
  warnings=$((warnings + 1))
}
fail() {
  printf 'FAIL  %s\n' "$1"
  failures=$((failures + 1))
}
query() { "${PSQL[@]}" -c "$1"; }

# expect_value <label> <sql> <expected single value>
expect_value() {
  local got
  got="$(query "$2")"
  if [ "$got" = "$3" ]; then
    ok "$1"
  else
    fail "$1: got '$got', want '$3'"
  fi
}

# expect_error <label> <sql> <regex the error must match>
expect_error() {
  local out
  if out="$(query "$2" 2>&1)"; then
    fail "$1: not refused"
  elif printf '%s' "$out" | grep -Eq "$3"; then
    ok "$1: $(printf '%s' "$out" | grep -E "$3" | head -n 1)"
  else
    fail "$1: unexpected error: $out"
  fi
}

NON_SYSTEM="n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'"

expect_value "connect and read" "SELECT 1" "1"
expect_value "role attributes (superuser, createrole, createdb, replication, bypassrls)" \
  "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
     FROM pg_roles WHERE rolname = current_user" "f"
expect_value "no INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER on any table or view" \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE $NON_SYSTEM AND CASE WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f')
       THEN has_table_privilege(c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
         OR has_any_column_privilege(c.oid, 'INSERT, UPDATE, REFERENCES')
       ELSE false END" "0"
expect_value "no USAGE or UPDATE on sequences (nextval, setval)" \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE $NON_SYSTEM AND CASE WHEN c.relkind = 'S'
       THEN has_sequence_privilege(c.oid, 'USAGE, UPDATE') ELSE false END" "0"
expect_value "no CREATE on the database" \
  "SELECT has_database_privilege(current_database(), 'CREATE')" "f"

# The privilege checks above only see memberships the role inherits. Without
# INHERIT, a membership still lets the role SET ROLE to the other role and
# use its privileges. The predefined read roles grant no writes.
members="$(query "SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname)
  FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
  WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
    AND r.rolname NOT IN ('pg_read_all_data', 'pg_read_all_settings',
      'pg_read_all_stats', 'pg_monitor')")"
if [ -z "$members" ]; then
  ok "no membership in other roles (predefined read roles aside)"
else
  fail "member of $members: SET ROLE gives the role their privileges"
fi

schemas="$(query "SELECT string_agg(n.nspname, ', ') FROM pg_namespace n
  WHERE $NON_SYSTEM AND has_schema_privilege(n.oid, 'CREATE')")"
if [ -z "$schemas" ]; then
  ok "no CREATE on any schema"
else
  fail "CREATE on schema(s): $schemas (REVOKE CREATE ON SCHEMA ... FROM PUBLIC, or from this role)"
fi

if [ "$(query "SELECT has_database_privilege(current_database(), 'TEMPORARY')")" = "t" ]; then
  warn "TEMPORARY on the database: the role can create temporary tables (granted to PUBLIC by default)"
else
  ok "no TEMPORARY on the database"
fi

# Creating a large object needs no privilege on any table, and the object
# persists. Only a read-only transaction refuses it.
creators="$(query "SELECT string_agg(f, ', ')
  FROM unnest(ARRAY['lo_create(oid)', 'lo_creat(integer)',
    'lo_from_bytea(oid,bytea)']) AS f
  WHERE has_function_privilege(f, 'EXECUTE')")"
if [ -n "$creators" ]; then
  warn "large objects: the role can run $creators and create large objects that persist (EXECUTE is granted to PUBLIC by default)"
else
  ok "cannot create large objects"
fi
expect_value "lo_compat_privileges is off (large-object permission checks apply)" \
  "SELECT current_setting('lo_compat_privileges')" "off"

definers="$(query "SELECT string_agg(p.oid::regprocedure::text, ', ')
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE p.prosecdef AND $NON_SYSTEM AND has_function_privilege(p.oid, 'EXECUTE')")"
if [ -n "$definers" ]; then
  warn "SECURITY DEFINER functions this role can run with their owner's rights: $definers"
fi

expect_value "default_transaction_read_only is on" \
  "SELECT current_setting('default_transaction_read_only')" "on"
timeout="$(query "SELECT current_setting('statement_timeout')")"
if [ "$timeout" = "0" ]; then
  fail "statement_timeout is not set"
else
  ok "statement_timeout is $timeout"
  expect_error "a query longer than statement_timeout is cancelled" \
    "SELECT pg_sleep(extract(epoch FROM current_setting('statement_timeout')::interval) + 1)" \
    "statement timeout"
fi

# Write probes. The transaction switches the read-only default off, so the
# grants are what refuses CREATE TABLE. EXPLAIN checks privileges without the
# read-only default and without running the statement.
expect_error "CREATE TABLE in a read-write transaction" \
  "BEGIN READ WRITE; CREATE TABLE agent_readonly_probe (id int); ROLLBACK;" \
  "permission denied"
oid="$(query "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p') AND $NON_SYSTEM AND has_table_privilege(c.oid, 'SELECT')
  ORDER BY n.nspname, c.relname LIMIT 1")"
if [ -z "$oid" ]; then
  warn "no readable table found; skipped the INSERT, UPDATE and DELETE probes"
else
  table="$(query "SELECT format('%I.%I', n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = $oid")"
  column="$(query "SELECT quote_ident(attname) FROM pg_attribute
    WHERE attrelid = $oid AND attnum > 0 AND NOT attisdropped
      AND attidentity = '' AND attgenerated = '' ORDER BY attnum LIMIT 1")"
  expect_error "INSERT into $table" \
    "EXPLAIN INSERT INTO $table DEFAULT VALUES" "permission denied"
  if [ -n "$column" ]; then
    expect_error "UPDATE $table" \
      "EXPLAIN UPDATE $table SET $column = $column" "permission denied"
  fi
  expect_error "DELETE from $table" \
    "EXPLAIN DELETE FROM $table" "permission denied"
fi

if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed for role $PGUSER" >&2
  exit 1
fi
if [ "$warnings" -gt 0 ]; then
  echo "All checks passed for role $PGUSER, with $warnings warning(s) above"
else
  echo "All checks passed for role $PGUSER"
fi
