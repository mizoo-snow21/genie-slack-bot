#!/usr/bin/env bash
#
# サービスプリンシパルに必要な権限を設定するスクリプト
#
# 使い方:
#   ./scripts/setup_permissions.sh --profile <databricks_profile>
#
# 前提:
#   - databricks CLI がインストール済み
#   - アプリがデプロイ済み (databricks bundle deploy 実行済み)
#   - src/app/app.yaml が設定済み (DATABRICKS_GENIE_SPACE_ID, RESEARCH_CATALOG)
#
set -euo pipefail

PROFILE="${1:---profile=DEFAULT}"
if [[ "$1" == "--profile" ]]; then
    PROFILE="--profile=$2"
    shift 2
elif [[ "$1" == --profile=* ]]; then
    shift
fi

# ─── アプリ名の検出 ───
APP_NAME=$(databricks bundle validate $PROFILE 2>/dev/null | grep -o 'genie-slack-bot-[a-z]*' | head -1 || echo "")
if [[ -z "$APP_NAME" ]]; then
    echo "❌ アプリ名が検出できません。databricks bundle validate を確認してください。"
    echo "   手動で指定する場合: APP_NAME=genie-slack-bot-dev ./scripts/setup_permissions.sh"
    exit 1
fi
echo "📦 アプリ: $APP_NAME"

# ─── SP Client ID の取得 ───
SP_CLIENT_ID=$(databricks apps get "$APP_NAME" $PROFILE --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['service_principal_client_id'])" 2>/dev/null || echo "")
if [[ -z "$SP_CLIENT_ID" ]]; then
    echo "❌ サービスプリンシパルの Client ID が取得できません。"
    echo "   アプリがデプロイ済みか確認してください: databricks apps get $APP_NAME $PROFILE"
    exit 1
fi
echo "🔑 Service Principal: $SP_CLIENT_ID"

# ─── app.yaml から設定を読み取り ───
APP_YAML="src/app/app.yaml"
if [[ ! -f "$APP_YAML" ]]; then
    echo "❌ $APP_YAML が見つかりません。app.yaml.example をコピーして設定してください。"
    exit 1
fi

GENIE_SPACE_ID=$(grep -A1 "DATABRICKS_GENIE_SPACE_ID" "$APP_YAML" | grep 'value:' | sed 's/.*value: *"\?\([^"]*\)"\?.*/\1/' | head -1)
RESEARCH_CATALOG=$(grep -A1 "RESEARCH_CATALOG" "$APP_YAML" | grep 'value:' | sed 's/.*value: *"\?\([^"]*\)"\?.*/\1/' | head -1)
RESEARCH_SCHEMA=$(grep -A1 "RESEARCH_SCHEMA" "$APP_YAML" | grep 'value:' | sed 's/.*value: *"\?\([^"]*\)"\?.*/\1/' | head -1)
RESEARCH_SCHEMA="${RESEARCH_SCHEMA:-genie_research}"

echo ""
echo "設定:"
echo "  Genie Space ID:    $GENIE_SPACE_ID"
echo "  Research Catalog:  $RESEARCH_CATALOG"
echo "  Research Schema:   $RESEARCH_SCHEMA"
echo ""

# ─── 1. Genie Space: CAN_RUN ───
echo "🔧 [1/4] Genie Space に CAN_RUN 権限を付与..."
databricks api patch "/api/2.0/permissions/genie/$GENIE_SPACE_ID" \
  --json "{\"access_control_list\": [{\"service_principal_name\": \"$SP_CLIENT_ID\", \"permission_level\": \"CAN_RUN\"}]}" \
  $PROFILE 2>&1 && echo "  ✅ Genie Space: CAN_RUN" || echo "  ⚠️  失敗 (既に設定済みの可能性)"

# ─── 2. SQL Warehouse: CAN_USE ───
echo "🔧 [2/4] SQL Warehouse に CAN_USE 権限を付与..."
# 最初の RUNNING/STARTING warehouse を使う
WAREHOUSE_ID=$(databricks warehouses list $PROFILE --output json 2>/dev/null | python3 -c "
import sys, json
whs = json.load(sys.stdin).get('warehouses', [])
for w in whs:
    if w.get('state') in ('RUNNING', 'STARTING'):
        print(w['id'])
        break
" 2>/dev/null || echo "")

if [[ -n "$WAREHOUSE_ID" ]]; then
    databricks api patch "/api/2.0/permissions/warehouses/$WAREHOUSE_ID" \
      --json "{\"access_control_list\": [{\"service_principal_name\": \"$SP_CLIENT_ID\", \"permission_level\": \"CAN_USE\"}]}" \
      $PROFILE 2>&1 && echo "  ✅ Warehouse $WAREHOUSE_ID: CAN_USE" || echo "  ⚠️  失敗"
else
    echo "  ⚠️  RUNNING な Warehouse が見つかりません。手動で権限を付与してください。"
fi

# ─── 3. Unity Catalog: スキーマ権限 ───
echo "🔧 [3/4] Unity Catalog のスキーマ権限を付与..."
if [[ -n "$RESEARCH_CATALOG" && "$RESEARCH_CATALOG" != "your_catalog" ]]; then
    # CREATE SCHEMA if not exists
    databricks api post "/api/2.0/sql/statements" \
      --json "{\"statement\": \"CREATE SCHEMA IF NOT EXISTS $RESEARCH_CATALOG.$RESEARCH_SCHEMA\", \"warehouse_id\": \"$WAREHOUSE_ID\", \"wait_timeout\": \"30s\"}" \
      $PROFILE 2>&1 > /dev/null && echo "  ✅ スキーマ $RESEARCH_CATALOG.$RESEARCH_SCHEMA 作成/確認" || echo "  ⚠️  スキーマ作成失敗"

    # GRANT USE CATALOG, USE SCHEMA, CREATE TABLE, CREATE VOLUME
    for grant in \
      "GRANT USE CATALOG ON CATALOG $RESEARCH_CATALOG TO \`$SP_CLIENT_ID\`" \
      "GRANT USE SCHEMA ON SCHEMA $RESEARCH_CATALOG.$RESEARCH_SCHEMA TO \`$SP_CLIENT_ID\`" \
      "GRANT CREATE TABLE ON SCHEMA $RESEARCH_CATALOG.$RESEARCH_SCHEMA TO \`$SP_CLIENT_ID\`" \
      "GRANT CREATE VOLUME ON SCHEMA $RESEARCH_CATALOG.$RESEARCH_SCHEMA TO \`$SP_CLIENT_ID\`"
    do
        databricks api post "/api/2.0/sql/statements" \
          --json "{\"statement\": \"$grant\", \"warehouse_id\": \"$WAREHOUSE_ID\", \"wait_timeout\": \"30s\"}" \
          $PROFILE 2>&1 > /dev/null && echo "  ✅ $grant" || echo "  ⚠️  $grant — 失敗"
    done
else
    echo "  ⏭️  RESEARCH_CATALOG 未設定、スキップ"
fi

# ─── 4. Genie Space のテーブルに SELECT 付与 ───
echo "🔧 [4/4] Genie Space のテーブルへの SELECT 権限..."
echo "  ℹ️  Genie Space が参照するテーブルへの SELECT 権限は手動で付与してください:"
echo "     GRANT SELECT ON TABLE <catalog>.<schema>.<table> TO \`$SP_CLIENT_ID\`;"
echo ""

echo "=================================================="
echo "✅ セットアップ完了"
echo ""
echo "次のステップ:"
echo "  1. Genie Space のテーブルに SELECT 権限を付与"
echo "  2. databricks bundle deploy && databricks apps deploy $APP_NAME --source-code-path ..."
echo "  3. databricks apps logs $APP_NAME $PROFILE で起動を確認"
echo "=================================================="
