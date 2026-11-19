#!/bin/bash
# تمرين استعادة نسخة احتياطية في بيئة معزولة ببيانات اصطناعية:
# قاعدة مصدر ← تعبئة ٥٠٠ عميل اصطناعي + ١٠٠ تدريبي ← نسخة pg_dump ← استعادة في قاعدة جديدة فارغة ← عدّ ومطابقة.
# الاستخدام: PGURL=postgresql://postgres:pg@localhost bash scripts/backup_restore_drill.sh
set -euo pipefail
PGURL=${PGURL:-postgresql://postgres:pg@localhost}
SRC=drill_src; DST=drill_restore; OUT=$(mktemp -d)/drill.dump
psql "$PGURL/postgres" -qc "DROP DATABASE IF EXISTS $SRC" -c "DROP DATABASE IF EXISTS $DST" -c "CREATE DATABASE $SRC" -c "CREATE DATABASE $DST"
export DATABASE_URL="postgresql+psycopg://${PGURL#postgresql://}/$SRC" DATA_DIR=$(mktemp -d) SECRET_KEY=drill-secret ANTHROPIC_API_KEY=
python -m mandoob.seed_test >/dev/null
python -m mandoob.training_demo >/dev/null
SUM="SELECT 'companies', count(*), md5(string_agg(id::text||name, ',' ORDER BY id)) FROM companies UNION ALL
     SELECT 'contacts', count(*), md5(string_agg(id::text||value, ',' ORDER BY id)) FROM contacts UNION ALL
     SELECT 'activities', count(*), '' FROM activities UNION ALL SELECT 'users', count(*), '' FROM users"
psql "$PGURL/$SRC" -Atc "$SUM" > "$OUT.src"
t0=$(date +%s.%N)
pg_dump -Fc "$PGURL/$SRC" -f "$OUT"
t1=$(date +%s.%N)
pg_restore --no-owner -d "$PGURL/$DST" "$OUT"
t2=$(date +%s.%N)
psql "$PGURL/$DST" -Atc "$SUM" > "$OUT.dst"
echo "DRILL backup_seconds=$(echo "$t1-$t0" | bc) restore_seconds=$(echo "$t2-$t1" | bc) dump_bytes=$(stat -c %s "$OUT")"
echo "DRILL last_restored_point=$(psql "$PGURL/$DST" -Atc "SELECT max(at) FROM activities")"
paste -d'|' "$OUT.src" "$OUT.dst" | sed 's/^/DRILL source|restored: /'
if diff -q "$OUT.src" "$OUT.dst" >/dev/null; then echo "DRILL RESULT: PASS (counts and checksums match)"; else echo "DRILL RESULT: FAIL"; exit 1; fi
