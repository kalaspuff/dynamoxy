#!/usr/bin/env bash
# Asks real DynamoDB the questions that the open issues #87, #93, #97 and #106 leave open: the
# answers no documentation, moto or dynoxide-rs capture gives, which the in-memory backend models.
#
#   AWS_PROFILE=<test-account-profile> AWS_REGION=eu-north-1 bash scripts/probe_dynamodb.sh [ROUND]
#
# ROUND 1 (the default) asks the first questions; its answers, from eu-north-1 on 2026-10-31, are
# in docs/parity/. ROUND 2 asks what those answers left open: exact size limits and how DynamoDB
# counts them, and some messages.
#
# It creates a few tables named dynamoxy-probe-<random>-*, writes to them, and deletes them at the
# end (also when interrupted). It costs cents: on-demand tables (round 1 also has one provisioned
# at 1 read and 1 write unit for a few minutes). Round 1 takes 15-30 minutes, mostly waiting for
# index creation and billing switches; round 2 about 5. Every request and answer goes to
# dynamoxy-probe-<round>-<time>.log, with 12-digit numbers (account ids) replaced by <account>;
# send that file back.
#
# Needs the AWS CLI (v1 or v2) and python3. DYNAMODB_PROBE_ENDPOINT=<url> sends everything to an
# emulator instead, to try the script out.
set -uo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$REGION" ]; then
    echo "Set AWS_REGION (and AWS_PROFILE or credentials) first." >&2
    exit 2
fi
export AWS_PAGER=""
ENDPOINT_ARGS=()
if [ -n "${DYNAMODB_PROBE_ENDPOINT:-}" ]; then
    ENDPOINT_ARGS=(--endpoint-url "$DYNAMODB_PROBE_ENDPOINT")
fi

P="dynamoxy-probe-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
ROUND="${1:-1}"
case "$ROUND" in
1 | 2) ;;
*)
    echo "Unknown round $ROUND: give 1 or 2." >&2
    exit 2
    ;;
esac
LOG="dynamoxy-probe-$ROUND-$(date -u +%Y%m%dT%H%M%SZ).log"
TMP="$(mktemp -d)"
TABLES=()

ddb() { aws dynamodb "$@" --region "$REGION" "${ENDPOINT_ARGS[@]}" --output json; }

sanitize() { sed -E 's/[0-9]{12}/<account>/g'; }

log() { echo "$*" | sanitize | tee -a "$LOG"; }

compact() { python3 -c '
import json, sys
text = sys.stdin.read()
try:
    print(json.dumps(json.loads(text)))
except ValueError:
    print(text.strip())'; }

# probe ID DESCRIPTION ARGS...: runs one DynamoDB call and logs its exit code, output and error.
# With BODY=1 it also logs DynamoDB's last response body (the CLI shows only the codes of a
# transaction's CancellationReasons, not their messages).
probe() {
    local id=$1 what=$2
    shift 2
    local out rc debug=()
    if [ -n "${BODY:-}" ]; then debug=(--debug); fi
    out=$(ddb "$@" "${debug[@]}" 2>"$TMP/err")
    rc=$?
    {
        echo "### $id: $what"
        echo "exit: $rc"
        if [ -n "$out" ]; then echo "out: $(compact <<<"$out" | head -c 3000)"; fi
        if [ -n "${BODY:-}" ]; then
            grep -E '^An error occurred' "$TMP/err" | sed 's/^/err: /' | head -c 3000
            echo "body: $(awk 'found { body = $0; found = 0 } /Response body:$/ { found = 1 } END { print body }' "$TMP/err" | head -c 3000)"
        elif [ -s "$TMP/err" ]; then
            echo "err: $(grep -v '^[[:space:]]*$' "$TMP/err" | head -c 3000)"
        fi
        echo
    } | sanitize | tee -a "$LOG"
}

# status TABLE: prints the table's status and each global index's, e.g. "ACTIVE gsi1=CREATING/backfilling".
status() {
    ddb describe-table --table-name "$1" 2>/dev/null | python3 -c '
import json, sys
try:
    t = json.load(sys.stdin)["Table"]
except Exception:
    print("MISSING"); sys.exit()
parts = [t["TableStatus"]]
for i in t.get("GlobalSecondaryIndexes", []):
    parts.append(i["IndexName"] + "=" + i["IndexStatus"] + ("/backfilling" if i.get("Backfilling") else ""))
print(" ".join(parts))'
}

# wait_active TABLE [SECONDS]: waits until the table and all its global indexes are ACTIVE.
wait_active() {
    local table=$1 limit=${2:-1200} start=$SECONDS s
    while :; do
        s=$(status "$table")
        if [ "$s" != "MISSING" ] && ! grep -qv '^ACTIVE$' <<<"$(tr ' ' '\n' <<<"$s" | sed 's/^[^=]*=//')"; then
            return 0
        fi
        if ((SECONDS - start > limit)) || { [ "$s" = "MISSING" ] && ((SECONDS - start > 60)); }; then
            log "!!! $table not active after ${limit}s: $s"
            return 1
        fi
        sleep 5
    done
}

create() { # create NAME ARGS...: creates a table and remembers to delete it
    TABLES+=("$1")
    probe "setup" "create $1" create-table --table-name "$@" >/dev/null
}

cleanup() {
    log "## cleanup"
    local t
    for t in "${TABLES[@]}"; do
        wait_active "$t" 900 >/dev/null 2>&1
        if ddb delete-table --table-name "$t" >/dev/null 2>"$TMP/err"; then
            log "deleted $t"
        elif ! grep -q ResourceNotFound "$TMP/err"; then
            log "!!! could not delete $t, delete it yourself: aws dynamodb delete-table --table-name $t ($(cat "$TMP/err"))"
        fi
    done
    rm -rf "$TMP"
    echo "Done. Send back $LOG"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

log "# dynamoxy DynamoDB probes, round $ROUND, $(date -u +%Y-%m-%dT%H:%M:%SZ), region $REGION, $(aws --version 2>&1)"
log "tables: $P-*"
echo

KEY_ATTRS=(AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S)
KEY_SCHEMA=(AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE)

round1() {
# Item and request files: the large ones exceed what a command line argument may hold.
python3 - "$TMP" "$P" <<'EOF'
import json, sys
tmp, p = sys.argv[1], sys.argv[2]
KB = 1024

def s(v): return {"S": v}

def item(pk, sk, pad_kb=0, **extra):
    it = {"pk": s(pk), "sk": s(sk), **{k: s(v) for k, v in extra.items()}}
    if pad_kb:
        it["filler"] = s("a" * (pad_kb * KB))
    return it

def write(name, data):
    with open(f"{tmp}/{name}.json", "w") as f:
        json.dump(data, f)

# 97: attribute names of 65534, 65535 and 65536 bytes ("less than 64 KB"), in an item and through a placeholder.
for n in (65534, 65535, 65536):
    write(f"name{n}", {"pk": s("names"), "sk": s(str(n)), "n" * n: s("v")})
    write(f"names{n}", {"#n": "n" * n})
# 93: items of a table whose local index projects ALL (each item also counts its index entry?).
for kb in (150, 190, 210, 300):
    write(f"lsi{kb}", item("lsi", str(kb), kb, lsk="x"))
write("lsi300-unindexed", item("lsi", "300-unindexed", 300))
write("keys300", item("keys", "300", 300, lsk="x"))
write("pad60", {":v": s("b" * (60 * KB))})
write("transact-lsi210", [{"Put": {"TableName": f"{p}-lsi", "Item": item("lsi", "210-tx", 210, lsk="x")}}])
# 93: eleven items of 390 KB for TransactGetItems (4 MB of items read).
for i in range(11):
    write(f"big{i}", item("big", str(i), 390))
for count in (10, 11):
    write(f"get{count}", [{"Get": {"TableName": f"{p}-big", "Key": {"pk": s("big"), "sk": s(str(i))}}} for i in range(count)])
    write(f"update{count}", [
        {"Update": {
            "TableName": f"{p}-big",
            "Key": {"pk": s("upd"), "sk": s(str(i))},
            "UpdateExpression": "SET filler = :v",
            "ExpressionAttributeValues": {":v": s("c" * (390 * KB))},
        }}
        for i in range(count)
    ])
EOF

# A table with a KEYS_ONLY local index (pk, lsk) and a KEYS_ONLY global index (gk).
create "$P-idx" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions "${KEY_ATTRS[@]}" AttributeName=lsk,AttributeType=S AttributeName=gk,AttributeType=S \
    --key-schema "${KEY_SCHEMA[@]}" \
    --local-secondary-indexes '[{"IndexName": "lsi", "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "lsk", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}]' \
    --global-secondary-indexes '[{"IndexName": "gsi", "KeySchema": [{"AttributeName": "gk", "KeyType": "HASH"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}]'
# A table with a local index that projects ALL.
create "$P-lsi" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions "${KEY_ATTRS[@]}" AttributeName=lsk,AttributeType=S \
    --key-schema "${KEY_SCHEMA[@]}" \
    --local-secondary-indexes '[{"IndexName": "lsi", "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "lsk", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}]'
# A plain table for large transactions.
create "$P-big" --billing-mode PAY_PER_REQUEST --attribute-definitions "${KEY_ATTRS[@]}" --key-schema "${KEY_SCHEMA[@]}"
# A table with one global index, for billing switches.
create "$P-bill" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=gk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH \
    --global-secondary-indexes '[{"IndexName": "gsi1", "KeySchema": [{"AttributeName": "gk", "KeyType": "HASH"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}]'
# A table without indexes, to add two while the first is being created.
create "$P-gsi" --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=pk,AttributeType=S --key-schema AttributeName=pk,KeyType=HASH

log "## 87: calls while a table is CREATING"
create "$P-ttl" --billing-mode PAY_PER_REQUEST --attribute-definitions AttributeName=pk,AttributeType=S --key-schema AttributeName=pk,KeyType=HASH
log "status right after create: $(status "$P-ttl")"
probe 87-ttl-creating "UpdateTimeToLive while the table is CREATING" \
    update-time-to-live --table-name "$P-ttl" --time-to-live-specification Enabled=true,AttributeName=expires
probe 87-update-creating "UpdateTable (add a global index) while the table is CREATING" \
    update-table --table-name "$P-ttl" --attribute-definitions AttributeName=gk,AttributeType=S \
    --global-secondary-index-updates '[{"Create": {"IndexName": "gsi1", "KeySchema": [{"AttributeName": "gk", "KeyType": "HASH"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}}]'

for t in idx lsi big bill gsi ttl; do wait_active "$P-$t"; done
log "tables active"
echo

log "## 106: filters and projections of attributes an index does not project"
probe setup "item" put-item --table-name "$P-idx" \
    --item '{"pk": {"S": "a"}, "sk": {"S": "1"}, "lsk": {"S": "x"}, "gk": {"S": "g"}, "secret": {"S": "s"}}' >/dev/null
sleep 2 # global indexes are eventually consistent
S_VALUES='{":p": {"S": "a"}, ":s": {"S": "s"}}'
G_VALUES='{":g": {"S": "g"}, ":s": {"S": "s"}}'
probe 106-lsi-filter "Query KEYS_ONLY local index, filter on an attribute it does not project" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "secret = :s" --expression-attribute-values "$S_VALUES"
probe 106-lsi-filter-all "same with Select=ALL_ATTRIBUTES" \
    query --table-name "$P-idx" --index-name lsi --select ALL_ATTRIBUTES --key-condition-expression "pk = :p" --filter-expression "secret = :s" --expression-attribute-values "$S_VALUES"
probe 106-lsi-filter-count "same with Select=COUNT" \
    query --table-name "$P-idx" --index-name lsi --select COUNT --key-condition-expression "pk = :p" --filter-expression "secret = :s" --expression-attribute-values "$S_VALUES"
probe 106-lsi-filter-projection "same with a projection of that attribute (fetches from the table)" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "secret = :s" --projection-expression "pk, secret" --expression-attribute-values "$S_VALUES"
probe 106-lsi-projection "Query KEYS_ONLY local index, projection of an attribute it does not project, no filter" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --projection-expression "pk, secret" --expression-attribute-values '{":p": {"S": "a"}}'
probe 106-lsi-entity-filter "Query KEYS_ONLY local index with dynamoxy's entity filter on _type, Select=ALL_ATTRIBUTES" \
    query --table-name "$P-idx" --index-name lsi --select ALL_ATTRIBUTES --key-condition-expression "pk = :p" --filter-expression "attribute_not_exists(#t) OR #t = :e" \
    --expression-attribute-names '{"#t": "_type"}' --expression-attribute-values '{":p": {"S": "a"}, ":e": {"S": "Thing"}}'
probe 106-gsi-filter "Query KEYS_ONLY global index, filter on an attribute it does not project" \
    query --table-name "$P-idx" --index-name gsi --key-condition-expression "gk = :g" --filter-expression "secret = :s" --expression-attribute-values "$G_VALUES"
probe 106-gsi-entity-filter "Query KEYS_ONLY global index with dynamoxy's entity filter on _type" \
    query --table-name "$P-idx" --index-name gsi --key-condition-expression "gk = :g" --filter-expression "attribute_not_exists(#t) OR #t = :e" \
    --expression-attribute-names '{"#t": "_type"}' --expression-attribute-values '{":g": {"S": "g"}, ":e": {"S": "Thing"}}'
probe 106-gsi-projection "Query KEYS_ONLY global index, projection of an attribute it does not project" \
    query --table-name "$P-idx" --index-name gsi --key-condition-expression "gk = :g" --projection-expression "pk, secret" --expression-attribute-values '{":g": {"S": "g"}}'
probe 106-gsi-key-filter "control: Query global index, filter on a projected key attribute" \
    query --table-name "$P-idx" --index-name gsi --key-condition-expression "gk = :g" --filter-expression "sk = :sk" --expression-attribute-values '{":g": {"S": "g"}, ":sk": {"S": "1"}}'
probe 106-lsi-scan-filter "Scan KEYS_ONLY local index, filter on an attribute it does not project" \
    scan --table-name "$P-idx" --index-name lsi --filter-expression "secret = :s" --expression-attribute-values '{":s": {"S": "s"}}'
probe 106-lsi-scan-projection "Scan KEYS_ONLY local index, projection of an attribute it does not project" \
    scan --table-name "$P-idx" --index-name lsi --projection-expression "pk, secret"
probe 106-gsi-scan-filter "Scan KEYS_ONLY global index, filter on an attribute it does not project" \
    scan --table-name "$P-idx" --index-name gsi --filter-expression "secret = :s" --expression-attribute-values '{":s": {"S": "s"}}'
probe 106-gsi-scan-projection "Scan KEYS_ONLY global index, projection of an attribute it does not project" \
    scan --table-name "$P-idx" --index-name gsi --projection-expression "pk, secret"

log "## 97: expression limits"
KEY='{"pk": {"S": "a"}, "sk": {"S": "1"}}'
for len in 255 256 257; do
    name="#$(printf 'n%.0s' $(seq 2 "$len"))"
    probe "97-name-placeholder-$len" "a #name placeholder of $len bytes (with the #)" \
        get-item --table-name "$P-idx" --key "$KEY" --projection-expression "$name" --expression-attribute-names "{\"$name\": \"x\"}"
    value=":$(printf 'v%.0s' $(seq 2 "$len"))"
    probe "97-value-placeholder-$len" "a :value placeholder of $len bytes (with the :)" \
        query --table-name "$P-idx" --key-condition-expression "pk = $value" --expression-attribute-values "{\"$value\": {\"S\": \"a\"}}"
done
for n in 65534 65535 65536; do
    probe "97-item-name-$n" "an item with an attribute name of $n bytes" \
        put-item --table-name "$P-idx" --item "file://$TMP/name$n.json"
    probe "97-expression-name-$n" "a projection of an attribute name of $n bytes, through a placeholder" \
        get-item --table-name "$P-idx" --key "$KEY" --projection-expression "#n" --expression-attribute-names "file://$TMP/names$n.json"
done
probe setup "item" put-item --table-name "$P-idx" --item '{"pk": {"S": "atype"}, "sk": {"S": "1"}, "a": {"S": "x"}, "b": {"S": "S"}}' >/dev/null
ATYPE_KEY='{"pk": {"S": "atype"}, "sk": {"S": "1"}}'
probe 97-attribute-type-path-filter "attribute_type(a, b) in a filter, b a path holding \"S\" (a is a string)" \
    query --table-name "$P-idx" --key-condition-expression "pk = :p" --filter-expression "attribute_type(a, b)" --expression-attribute-values '{":p": {"S": "atype"}}'
probe 97-attribute-type-path-condition "attribute_type(a, b) as an update condition" \
    update-item --table-name "$P-idx" --key "$ATYPE_KEY" --update-expression "SET c = :c" --condition-expression "attribute_type(a, b)" --expression-attribute-values '{":c": {"S": "c"}}'
probe 97-attribute-type-path-condition-names "attribute_type(#a, #b) as an update condition" \
    update-item --table-name "$P-idx" --key "$ATYPE_KEY" --update-expression "SET c = :c" --condition-expression "attribute_type(#a, #b)" \
    --expression-attribute-names '{"#a": "a", "#b": "b"}' --expression-attribute-values '{":c": {"S": "c"}}'
probe 97-attribute-type-control "control: attribute_type(a, :t)" \
    query --table-name "$P-idx" --key-condition-expression "pk = :p" --filter-expression "attribute_type(a, :t)" --expression-attribute-values '{":p": {"S": "atype"}, ":t": {"S": "S"}}'
probe 97-name-placeholder-dash "a #a-b name placeholder" \
    get-item --table-name "$P-idx" --key "$KEY" --projection-expression "#a-b" --expression-attribute-names '{"#a-b": "x"}'
probe 97-name-placeholder-dash-unused "a #a-b name placeholder that the expression does not use" \
    get-item --table-name "$P-idx" --key "$KEY" --projection-expression "pk" --expression-attribute-names '{"#a-b": "x"}'
probe 97-value-placeholder-dash "a :a-b value placeholder" \
    query --table-name "$P-idx" --key-condition-expression "pk = :a-b" --expression-attribute-values '{":a-b": {"S": "a"}}'
probe 97-value-placeholder-dash-unused "a :a-b value placeholder that the expression does not use" \
    query --table-name "$P-idx" --key-condition-expression "pk = :p" --expression-attribute-values '{":p": {"S": "a"}, ":a-b": {"S": "a"}}'
BODY=1 probe 97-tx-read-missing "ExecuteTransaction, one SELECT of a missing table" \
    execute-transaction --transact-statements "[{\"Statement\": \"SELECT * FROM \\\"$P-missing\\\" WHERE pk = 'a' AND sk = '1'\"}]"
BODY=1 probe 97-tx-read-missing-second "ExecuteTransaction, SELECTs of an existing and a missing table" \
    execute-transaction --transact-statements "[{\"Statement\": \"SELECT * FROM \\\"$P-idx\\\" WHERE pk = 'a' AND sk = '1'\"}, {\"Statement\": \"SELECT * FROM \\\"$P-missing\\\" WHERE pk = 'a' AND sk = '1'\"}]"
BODY=1 probe 97-tx-write-missing "control: ExecuteTransaction, one INSERT into a missing table" \
    execute-transaction --transact-statements "[{\"Statement\": \"INSERT INTO \\\"$P-missing\\\" VALUE {'pk': 'a', 'sk': '1'}\"}]"
BODY=1 probe 97-transact-get-missing "TransactGetItems of a missing table" \
    transact-get-items --transact-items "[{\"Get\": {\"TableName\": \"$P-missing\", \"Key\": $KEY}}]"

log "## 93: local index entries in the 400 KB item limit (index projects ALL)"
for kb in 150 190 210 300; do
    probe "93-lsi-put-$kb" "PutItem of a ${kb} KB item that the ALL local index holds" \
        put-item --table-name "$P-lsi" --item "file://$TMP/lsi$kb.json" --return-consumed-capacity INDEXES
done
probe 93-lsi-put-300-unindexed "control: PutItem of a 300 KB item without the index key" \
    put-item --table-name "$P-lsi" --item "file://$TMP/lsi300-unindexed.json"
probe 93-keys-only-put-300 "control: PutItem of a 300 KB item that a KEYS_ONLY local index holds" \
    put-item --table-name "$P-idx" --item "file://$TMP/keys300.json"
probe 93-lsi-update-150-to-210 "UpdateItem growing the 150 KB indexed item by 60 KB" \
    update-item --table-name "$P-lsi" --key '{"pk": {"S": "lsi"}, "sk": {"S": "150"}}' --update-expression "SET filler2 = :v" --expression-attribute-values "file://$TMP/pad60.json"
BODY=1 probe 93-lsi-transact-put-210 "TransactWriteItems Put of the 210 KB indexed item" \
    transact-write-items --transact-items "file://$TMP/transact-lsi210.json"

log "## 93: 4 MB of items in TransactGetItems"
for i in $(seq 0 10); do
    probe setup "390 KB item $i" put-item --table-name "$P-big" --item "file://$TMP/big$i.json" >/dev/null
done
BODY=1 probe 93-transact-get-10 "control: TransactGetItems of ten 390 KB items (3.9 MB)" \
    transact-get-items --transact-items "file://$TMP/get10.json" --query 'length(Responses)'
BODY=1 probe 93-transact-get-11 "TransactGetItems of eleven 390 KB items (4.3 MB)" \
    transact-get-items --transact-items "file://$TMP/get11.json" --query 'length(Responses)'
BODY=1 probe 93-transact-update-10 "control: TransactWriteItems of ten Updates writing 390 KB each" \
    transact-write-items --transact-items "file://$TMP/update10.json"
BODY=1 probe 93-transact-update-11 "TransactWriteItems of eleven Updates writing 390 KB each" \
    transact-write-items --transact-items "file://$TMP/update11.json"

log "## 87: billing switches of a table with a global index"
G1_THROUGHPUT='[{"Update": {"IndexName": "gsi1", "ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}}}]'
probe 87-provisioned-without-index "switch to PROVISIONED with capacity for the table only" \
    update-table --table-name "$P-bill" --billing-mode PROVISIONED --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1
wait_active "$P-bill"
log "status: $(status "$P-bill")"
probe 87-provisioned-with-index "control: switch to PROVISIONED with capacity for the table and the index" \
    update-table --table-name "$P-bill" --billing-mode PROVISIONED --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1 \
    --global-secondary-index-updates "$G1_THROUGHPUT" --query 'TableDescription.TableStatus'
wait_active "$P-bill"
probe 87-describe-provisioned "the table, now provisioned" \
    describe-table --table-name "$P-bill" --query 'Table.{billing: BillingModeSummary.BillingMode, table: ProvisionedThroughput, indexes: GlobalSecondaryIndexes[].{name: IndexName, throughput: ProvisionedThroughput}}'
probe 87-create-index-without-throughput "add a global index without ProvisionedThroughput to the provisioned table" \
    update-table --table-name "$P-bill" --attribute-definitions AttributeName=gk2,AttributeType=S \
    --global-secondary-index-updates '[{"Create": {"IndexName": "gsi2", "KeySchema": [{"AttributeName": "gk2", "KeyType": "HASH"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}}]'
wait_active "$P-bill"
probe 87-on-demand "switch back to PAY_PER_REQUEST" \
    update-table --table-name "$P-bill" --billing-mode PAY_PER_REQUEST --query 'TableDescription.TableStatus'
wait_active "$P-bill"
probe 87-describe-on-demand "the table, on-demand again: what remains of the capacity" \
    describe-table --table-name "$P-bill" --query 'Table.{billing: BillingModeSummary.BillingMode, table: ProvisionedThroughput, indexes: GlobalSecondaryIndexes[].{name: IndexName, throughput: ProvisionedThroughput, on_demand: OnDemandThroughput}}'
probe 87-on-demand-with-throughput "UpdateTable of the on-demand table giving the index ProvisionedThroughput" \
    update-table --table-name "$P-bill" --global-secondary-index-updates "$G1_THROUGHPUT"

log "## 87: a second global index while the first is being created"
probe setup "item" put-item --table-name "$P-gsi" --item '{"pk": {"S": "a"}, "ga": {"S": "x"}, "gb": {"S": "y"}}' >/dev/null
create_index() { # create_index ID DESCRIPTION INDEX ATTRIBUTE
    probe "$1" "$2" update-table --table-name "$P-gsi" --attribute-definitions "AttributeName=$4,AttributeType=S" \
        --global-secondary-index-updates "[{\"Create\": {\"IndexName\": \"$3\", \"KeySchema\": [{\"AttributeName\": \"$4\", \"KeyType\": \"HASH\"}], \"Projection\": {\"ProjectionType\": \"KEYS_ONLY\"}}}]" \
        --query 'TableDescription.[TableStatus, GlobalSecondaryIndexes[].[IndexName, IndexStatus]]'
}
create_index 87-first-index "create index gsia" gsia ga
log "status: $(status "$P-gsi")"
create_index 87-second-index-at-once "create index gsib right after (table UPDATING)" gsib gb
last="" tried=""
start=$SECONDS
while ((SECONDS - start < 1500)); do
    s=$(status "$P-gsi")
    if [ "$s" != "$last" ]; then
        log "after $((SECONDS - start))s: $s"
        last=$s
    fi
    if [ -z "$tried" ] && [[ "$s" == ACTIVE* ]] && [[ "$s" == *gsia=CREATING* ]]; then
        tried=1
        create_index 87-second-index-while-backfilling "create index gsib while the table is ACTIVE and gsia is still CREATING" gsib gb
        continue
    fi
    if [[ "$s" == ACTIVE* && "$s" != *CREATING* && "$s" != *UPDATING* ]]; then
        break
    fi
    sleep 3
done
[ -n "$tried" ] || log "the table was never ACTIVE while gsia was CREATING"
log "final: $(status "$P-gsi")"
}

round2() {
# Item and request files.
python3 - "$TMP" "$P" <<'EOF'
import base64, json, sys
tmp, p = sys.argv[1], sys.argv[2]
KB = 1024

def s(v): return {"S": v}

def write(name, data):
    with open(f"{tmp}/{name}.json", "w") as f:
        json.dump(data, f)

def sized(total, pk, sk, **extra):
    """An item of exactly `total` bytes by DynamoDB's count (names and string values in UTF-8)."""
    it = {"pk": s(pk), "sk": s(sk), **{k: s(v) for k, v in extra.items()}}
    used = sum(len(k) + len(v["S"]) for k, v in it.items()) + len("filler")
    it["filler"] = s("a" * (total - used))
    return it

# 93: the 400 KB limit of an item and its local index entries, at the byte. "lsk" makes the item an
# index entry; sort keys of one length keep the sizes comparable.
for total in (204690, 204700, 204740, 204750, 204751, 204760, 204790, 204800, 204801, 204850):
    write(f"all{total}", sized(total, "b", f"all{total}", lsk="x"))
for total in (409480, 409488, 409489, 409500, 409580, 409588, 409589, 409600):
    write(f"keys{total}", sized(total, "b", f"key{total}", lsk="x"))
for total in (409600, 409601):
    write(f"plain{total}", sized(total, "b", f"pla{total}"))

# 93: what TransactWriteItems counts as its payload ("Payload Size: N"), one change at a time.
def updates(table, count, value_size, pk="upd", expression="SET filler = :v", extra=None):
    actions = []
    for i in range(count):
        update = {
            "TableName": table,
            "Key": {"pk": s(pk), "sk": s(f"{i:02}")},
            "UpdateExpression": expression,
            "ExpressionAttributeValues": {":v": s("c" * value_size)},
        }
        update.update(extra or {})
        actions.append({"Update": update})
    return actions

big, longer = f"{p}-big", f"{p}-big-with-a-longer-name"
write("tx-base", updates(big, 11, 390 * KB))
data = updates(big, 11, 390 * KB, expression="SET filler = :v, note = :w")
for action in data:
    action["Update"]["ExpressionAttributeValues"][":w"] = s("x")
write("tx-expression", data)
write("tx-long-table", updates(longer, 11, 390 * KB))
write("tx-long-key", updates(big, 11, 390 * KB, pk="updupd"))
write("tx-condition", updates(big, 11, 390 * KB, extra={"ConditionExpression": "attribute_not_exists(absent)"}))
data = updates(big, 11, 390 * KB, expression="SET #f = :v")
for action in data:
    action["Update"]["ExpressionAttributeNames"] = {"#f": "filler"}
write("tx-names", data)
write("tx-puts", [{"Put": {"TableName": big, "Item": {"pk": s("upd"), "sk": s(f"{i:02}"), "filler": s("c" * (390 * KB))}}} for i in range(11)])
# Over 4,000,000 bytes, under 4 MiB (4,194,304): which "4 MB" is it?
write("tx-decimal", updates(big, 11, 363_700, pk="dec"))

# 93: what TransactGetItems counts towards 4 MB: binary items of 3.3 MB in all, whose response
# (base64) is 4.4 MB; and string items of 4,000,900 bytes in all (under 4 MiB).
for i in range(11):
    write(f"bin{i}", {"pk": s("bin"), "sk": s(f"{i:02}"), "blobby": {"B": base64.b64encode(b"\x00" * (300 * KB)).decode()}})
    write(f"dec{i}", {"pk": s("decget"), "sk": s(f"{i:02}"), "filler": s("d" * 363_700)})
for name, pk in (("get-bin", "bin"), ("get-dec", "decget")):
    write(name, [{"Get": {"TableName": big, "Key": {"pk": s(pk), "sk": s(f"{i:02}")}}} for i in range(11)])

# 97: attribute names of 64 KB in a map, in an update and behind write placeholders.
write("map-name", {"pk": s("names"), "sk": s("map"), "m": {"M": {"n" * 65536: s("v")}}})
for n in (65535, 65536):
    write(f"update-names{n}", {"#n": "n" * n})
write("long-key-names", {"#" + "n" * 255: "x"})
EOF

KEYS_LSI='[{"IndexName": "lsi", "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "lsk", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}]'
create "$P-idx" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions "${KEY_ATTRS[@]}" AttributeName=lsk,AttributeType=S AttributeName=gk,AttributeType=S \
    --key-schema "${KEY_SCHEMA[@]}" --local-secondary-indexes "$KEYS_LSI" \
    --global-secondary-indexes '[{"IndexName": "gsi", "KeySchema": [{"AttributeName": "gk", "KeyType": "HASH"}], "Projection": {"ProjectionType": "KEYS_ONLY"}}]'
create "$P-lsi" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions "${KEY_ATTRS[@]}" AttributeName=lsk,AttributeType=S \
    --key-schema "${KEY_SCHEMA[@]}" \
    --local-secondary-indexes '[{"IndexName": "lsi", "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "lsk", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}]'
create "$P-keys" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions "${KEY_ATTRS[@]}" AttributeName=lsk,AttributeType=S \
    --key-schema "${KEY_SCHEMA[@]}" --local-secondary-indexes "$KEYS_LSI"
create "$P-big" --billing-mode PAY_PER_REQUEST --attribute-definitions "${KEY_ATTRS[@]}" --key-schema "${KEY_SCHEMA[@]}"
create "$P-big-with-a-longer-name" --billing-mode PAY_PER_REQUEST --attribute-definitions "${KEY_ATTRS[@]}" --key-schema "${KEY_SCHEMA[@]}"
for t in idx lsi keys big big-with-a-longer-name; do wait_active "$P-$t"; done
log "tables active"
echo

log "## 106: more reads of attributes an index does not project"
probe setup "item" put-item --table-name "$P-idx" \
    --item '{"pk": {"S": "a"}, "sk": {"S": "1"}, "lsk": {"S": "x"}, "gk": {"S": "g"}, "secret": {"S": "s"}, "note": {"S": "o"}, "m": {"M": {"x": {"S": "s"}}}}' >/dev/null
sleep 2 # global indexes are eventually consistent
probe 106-lsi-scan-all-filter "Scan KEYS_ONLY local index, Select=ALL_ATTRIBUTES, filter on an attribute it does not project" \
    scan --table-name "$P-idx" --index-name lsi --select ALL_ATTRIBUTES --filter-expression "secret = :s" --expression-attribute-values '{":s": {"S": "s"}}'
probe 106-lsi-scan-projection-filter "Scan KEYS_ONLY local index, projection and filter of an attribute it does not project" \
    scan --table-name "$P-idx" --index-name lsi --projection-expression "pk, secret" --filter-expression "secret = :s" --expression-attribute-values '{":s": {"S": "s"}}'
probe 106-lsi-filter-nested "Query local index, filter on a nested path of an attribute it does not project" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "m.x = :s" --expression-attribute-values '{":p": {"S": "a"}, ":s": {"S": "s"}}'
probe 106-lsi-filter-size "Query local index, size() of an attribute it does not project" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "size(secret) > :n" --expression-attribute-values '{":p": {"S": "a"}, ":n": {"N": "0"}}'
probe 106-lsi-filter-two "Query local index, filter on two attributes it does not project (secret, then note)" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "secret = :s AND note = :o" --expression-attribute-values '{":p": {"S": "a"}, ":s": {"S": "s"}, ":o": {"S": "o"}}'
probe 106-lsi-filter-two-reversed "same, note first" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "note = :o AND secret = :s" --expression-attribute-values '{":p": {"S": "a"}, ":s": {"S": "s"}, ":o": {"S": "o"}}'
probe 106-lsi-filter-mixed "Query local index, filter on a projected and an unprojected attribute" \
    query --table-name "$P-idx" --index-name lsi --key-condition-expression "pk = :p" --filter-expression "gk = :g AND secret = :s" --expression-attribute-values '{":p": {"S": "a"}, ":s": {"S": "s"}, ":g": {"S": "g"}}'
probe 106-gsi-projection-several "Query global index, projection of several attributes it does not project" \
    query --table-name "$P-idx" --index-name gsi --key-condition-expression "gk = :g" --projection-expression "note, m.x, pk, secret" --expression-attribute-values '{":g": {"S": "g"}}'
probe 106-gsi-scan-projection-filter "Scan global index, filter and projection of attributes it does not project" \
    scan --table-name "$P-idx" --index-name gsi --projection-expression "secret" --filter-expression "note = :o" --expression-attribute-values '{":o": {"S": "o"}}'

log "## 93: the 400 KB limit with local index entries, at the byte"
for total in 204690 204700 204740 204750 204751 204760 204790 204800 204801 204850; do
    probe "93-all-$total" "PutItem of a $total-byte item that an ALL local index holds" \
        put-item --table-name "$P-lsi" --item "file://$TMP/all$total.json"
done
for total in 409480 409488 409489 409500 409580 409588 409589 409600; do
    probe "93-keys-$total" "PutItem of a $total-byte item that a KEYS_ONLY local index holds (index entry: 12 bytes of keys)" \
        put-item --table-name "$P-keys" --item "file://$TMP/keys$total.json"
done
for total in 409600 409601; do
    probe "93-plain-$total" "control: PutItem of a $total-byte item, table without indexes" \
        put-item --table-name "$P-big" --item "file://$TMP/plain$total.json"
done

log "## 93: what TransactWriteItems counts as its payload"
for variant in base expression long-table long-key condition names puts decimal; do
    BODY=1 probe "93-tx-$variant" "TransactWriteItems, variant $variant" \
        transact-write-items --transact-items "file://$TMP/tx-$variant.json"
done

log "## 93: what TransactGetItems counts towards 4 MB"
for i in $(seq 0 10); do
    probe setup "binary item $i" put-item --table-name "$P-big" --item "file://$TMP/bin$i.json" >/dev/null
    probe setup "string item $i" put-item --table-name "$P-big" --item "file://$TMP/dec$i.json" >/dev/null
done
BODY=1 probe 93-get-binary "TransactGetItems of eleven 300 KB binary items (3.3 MB of items, 4.4 MB of base64)" \
    transact-get-items --transact-items "file://$TMP/get-bin.json" --query 'length(Responses)'
BODY=1 probe 93-get-decimal "TransactGetItems of eleven 363,700-byte string items (4,000,900 bytes, under 4 MiB)" \
    transact-get-items --transact-items "file://$TMP/get-dec.json" --query 'length(Responses)'

log "## 97: long names elsewhere"
KEY='{"pk": {"S": "a"}, "sk": {"S": "1"}}'
probe 97-map-key-65536 "an item with a map key of 65536 bytes" \
    put-item --table-name "$P-idx" --item "file://$TMP/map-name.json"
for n in 65535 65536; do
    probe "97-update-name-$n" "UpdateItem SET of an attribute name of $n bytes, through a placeholder" \
        update-item --table-name "$P-idx" --key "$KEY" --update-expression "SET #n = :v" --expression-attribute-names "file://$TMP/update-names$n.json" --expression-attribute-values '{":v": {"S": "v"}}'
done
probe 97-write-long-placeholder "PutItem with a 256-byte #name placeholder in its condition" \
    put-item --table-name "$P-idx" --item '{"pk": {"S": "w"}, "sk": {"S": "1"}}' \
    --condition-expression "attribute_not_exists(#$(printf 'n%.0s' $(seq 1 255)))" --expression-attribute-names "file://$TMP/long-key-names.json"
probe 97-write-dash-placeholder "PutItem with a #a-b placeholder in its condition" \
    put-item --table-name "$P-idx" --item '{"pk": {"S": "w"}, "sk": {"S": "2"}}' \
    --condition-expression "attribute_not_exists(#a-b)" --expression-attribute-names '{"#a-b": "x"}'
probe 97-write-attribute-type-path "PutItem with attribute_type(a, b) in its condition" \
    put-item --table-name "$P-idx" --item '{"pk": {"S": "w"}, "sk": {"S": "3"}}' --condition-expression "attribute_type(a, b)"
}

"round$ROUND"
