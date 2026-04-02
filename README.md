# Genie Slack Bot

Databricks Genie Space に Slack から自然言語で質問できる Bot。
Databricks Apps 上で動作し、Socket Mode で Slack に接続する。

## できること

- **自然言語でデータ分析** — Slack の DM やチャンネルから Genie Space に質問。SQL を自動生成・実行し、結果をテーブルとグラフで返す
- **スレッドで会話継続** — 同じスレッド内でフォローアップ質問が可能（Genie の conversation を維持）
- **LLM 駆動のグラフ自動生成** — Foundation Model API がデータと質問の意図から最適なグラフ種別を判断し、seaborn/matplotlib で描画
- **フォローアップ質問の提案** — Genie が返す suggested questions を表示
- **フィードバック機能** — Helpful / Not Helpful ボタンで Genie API にフィードバック送信

![Demo](docs/demo.gif)

## アーキテクチャ

```
Slack ──(Socket Mode)──> Databricks App ──(Genie API)──> Genie Space ──(SQL)──> SQL Warehouse
                              │
                              ├── Foundation Model API でグラフ仕様を決定
                              └── seaborn/matplotlib でグラフ画像生成 → Slack にアップロード
```

| コンポーネント | 役割 |
|---|---|
| Slack Bot (`slack-bolt`) | Socket Mode でメッセージ受信・送信・フィードバック・モーダル処理 |
| Databricks App | サービスプリンシパルの OAuth M2M 認証で Genie API を呼び出し |
| Genie Space | 自然言語 → SQL 変換、Unity Catalog テーブルへのクエリ実行 |
| Foundation Model API | クエリ結果からグラフ仕様（種別・軸・色分け）を JSON で生成 |
| seaborn + japanize-matplotlib | グラフ仕様に基づいて PNG 画像を描画（日本語対応） |

## ファイル構成

```
genie-slack-bot/
├── databricks.yml              # DABs メイン設定（バンドル名・ターゲット定義）
├── resources/
│   └── genie_slack_bot.app.yml # App リソース定義
├── src/app/
│   ├── app.py                  # エントリーポイント
│   ├── app.yaml.example        # Databricks Apps 設定テンプレート（※ app.yaml は git 管理外）
│   ├── config.py               # 環境変数の読み込み・バリデーション
│   ├── databricks_genie_client.py  # Genie API クライアント
│   ├── slack_bot.py            # Slack Bot（イベント処理・モーダル・レスポンス整形・グラフ送信）
│   ├── chart_generator.py      # LLM チャート仕様決定 + seaborn 描画
│   ├── pyproject.toml          # 依存管理（uv 用、ブロック解除後に uv lock で使用）
│   └── requirements.txt        # 依存管理（pip 用、デプロイで使用）
├── README.md
└── .gitignore
```

## 前提条件

- Databricks ワークスペース（Apps 機能が有効）
- Databricks CLI がインストール・認証済み
- Slack ワークスペースの管理者権限（Slack App 作成に必要）
- Genie Space と接続先の Unity Catalog テーブルが準備済み

---

## セットアップ手順

### Step 1: Slack App の作成

1. https://api.slack.com/apps を開く
2. **Create New App** → **From scratch** を選択
3. App Name（例: `Genie Bot`）と対象ワークスペースを選択して作成

### Step 2: Socket Mode の有効化

1. 左メニュー **Socket Mode** をクリック
2. **Enable Socket Mode** を ON にする
3. Token Name（例: `genie-socket`）を入力し **Generate** をクリック
4. 表示される `xapp-` で始まるトークンを控える → **SLACK_APP_TOKEN** として使用

### Step 3: Bot のスコープ設定

1. 左メニュー **OAuth & Permissions** をクリック
2. **Scopes** セクションの **Bot Token Scopes** で **Add an OAuth Scope** をクリック
3. 以下の 6 つのスコープを追加:

| スコープ | 用途 |
|---|---|
| `app_mentions:read` | チャンネルでの @メンション検知 |
| `chat:write` | Bot からのメッセージ送信 |
| `files:write` | グラフ画像のアップロード |
| `im:history` | DM の履歴読み取り |
| `im:read` | DM チャンネルの読み取り |
| `im:write` | DM への書き込み |

### Step 4: Event Subscriptions の設定

1. 左メニュー **Event Subscriptions** をクリック
2. **Enable Events** を ON にする
3. **Subscribe to bot events** で以下を追加:

| イベント | 用途 |
|---|---|
| `app_mention` | チャンネルで Bot がメンションされたとき |
| `message.im` | Bot に DM が送られたとき |

### Step 5: ワークスペースへのインストールとトークン取得

1. 左メニュー **OAuth & Permissions** → **Install to Workspace** → **Allow** をクリック
2. 表示される `xoxb-` で始まるトークンを控える → **SLACK_BOT_TOKEN** として使用
3. 左メニュー **Basic Information** → **App Credentials** セクション → **Signing Secret** の **Show** をクリックして控える → **SLACK_SIGNING_SECRET** として使用

### Step 6: Genie Space ID の取得

1. Databricks ワークスペースで対象の Genie Space を開く
2. 右上の **Settings**（歯車アイコン）をクリック
3. 表示される Space ID をコピー → **DATABRICKS_GENIE_SPACE_ID** として使用

### Step 7: app.yaml の作成

```bash
cp src/app/app.yaml.example src/app/app.yaml
```

`src/app/app.yaml` を編集し、Step 2〜6 で取得した値を記入:

```yaml
command: ["python", "app.py"]

env:
  - name: SLACK_BOT_TOKEN
    value: "xoxb-..."        # Step 5 で取得
  - name: SLACK_SIGNING_SECRET
    value: "..."              # Step 5 で取得
  - name: SLACK_APP_TOKEN
    value: "xapp-..."        # Step 2 で取得
  - name: DATABRICKS_GENIE_SPACE_ID
    value: "..."              # Step 6 で取得
  - name: PORT
    value: "3000"
  - name: LOG_LEVEL
    value: "INFO"
```

### Step 8: Databricks CLI の認証

```bash
# ワークスペースにログイン（ブラウザで SSO 認証）
databricks auth login --host https://<your-workspace>.cloud.databricks.com --profile DEFAULT

# 認証状態の確認（Valid が YES であること）
databricks auth profiles
```

> `databricks.yml` の `workspace.profile` がログインした profile 名と一致していることを確認してください。デフォルトは `DEFAULT` です。

### Step 9: Databricks Asset Bundle でデプロイ

```bash
# 1. バリデーション
databricks bundle validate

# 2. デプロイ
databricks bundle deploy

# 3. app.yaml をワークスペースにアップロード（.gitignore で除外されているため手動）
databricks workspace import "<source_code_path>/app.yaml" --file src/app/app.yaml --format AUTO --overwrite

# 4. アプリ起動
databricks bundle run genie_slack_bot
```

> **ターゲット指定**: prod 環境にデプロイする場合は `-t prod` を付与

### Step 10: サービスプリンシパルに権限を付与

アプリ作成時にサービスプリンシパルが自動生成されます。その Client ID を確認:

```bash
databricks apps get genie-slack-bot-dev
# → service_principal_client_id の値を控える
```

以下 3 つの権限を付与:

```bash
SP_CLIENT_ID=<service_principal_client_id>
GENIE_SPACE_ID=<genie_space_id>
WAREHOUSE_ID=<sql_warehouse_id>

# (a) Genie Space: CAN_RUN
databricks api patch /api/2.0/permissions/genie/$GENIE_SPACE_ID \
  --json "{\"access_control_list\": [{\"service_principal_name\": \"$SP_CLIENT_ID\", \"permission_level\": \"CAN_RUN\"}]}"

# (b) SQL Warehouse: CAN_USE
databricks api patch /api/2.0/permissions/warehouses/$WAREHOUSE_ID \
  --json "{\"access_control_list\": [{\"service_principal_name\": \"$SP_CLIENT_ID\", \"permission_level\": \"CAN_USE\"}]}"

# (c) Unity Catalog テーブル: SELECT（SQL または UI から付与）
# GRANT SELECT ON TABLE <catalog>.<schema>.<table> TO `<sp_client_id>`;
```

### Step 11: 動作確認

```bash
databricks apps logs genie-slack-bot-dev
```

以下が出ていれば起動成功:

```
⚡️ Bolt app is running!
Starting to receive messages from a new connection
```

---

## 使い方

| 方法 | 例 |
|---|---|
| **DM** | Bot に直接メッセージ: `売上トップ10の顧客は？` |
| **チャンネル** | `@Genie Bot 地域別の売上推移を見せて` |
| **スレッドで深掘り** | 回答のスレッドに `前四半期と比較して` と続ける |
| **フィードバック** | 回答後に表示される **Helpful** / **Not Helpful** ボタンをクリック |

### グラフの自動生成

LLM（Foundation Model API）がユーザーの質問とクエリ結果を分析し、最適なグラフ種別を自動選択します。

| グラフ種別 | 選択される場面 |
|---|---|
| 縦棒グラフ | ランキング・比較（12件以下） |
| 横棒グラフ | ランキング・比較（13件以上 or 長いラベル） |
| 折れ線グラフ | 時系列の推移 |
| 複数折れ線 | カテゴリ別の時系列推移 |
| 面グラフ | 累計・ボリュームの推移 |
| ドーナツチャート | シェア・構成比・割合 |
| 積み上げ棒グラフ | カテゴリ別の内訳比較 |
| グループ化棒グラフ | 同スケールの複数指標比較 |
| 散布図 | 2つの数値の相関 |
| ヒストグラム | 数値の分布 |
| 2軸グラフ | スケールが異なる2指標の比較 |

### フィードバック

- **Helpful** ボタン → Genie Space に POSITIVE フィードバックが送信される
- **Not Helpful** ボタン → Genie Space に NEGATIVE フィードバックが送信される

---

## 再デプロイ

ソースコードを変更した場合:

```bash
databricks bundle deploy
databricks bundle run genie_slack_bot
```

---

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `invalid_auth` エラー | Slack トークンが無効。Slack App の OAuth & Permissions で再インストールし、新しいトークンで `app.yaml` を更新 |
| Genie API timeout | SQL Warehouse が停止中の可能性。Warehouse を起動してから再試行 |
| グラフが表示されない | Slack App に `files:write` スコープが未追加。追加後に Reinstall to Workspace が必要 |
| グラフにタイトル/ラベルがない | japanize-matplotlib が未インストール。`requirements.txt` に記載されているか確認 |
| `App Not Available` とブラウザに表示 | 正常動作。このアプリはバックエンドサービスのため UI はない |

---

## 制限事項

- 会話マッピング（スレッド ↔ Genie conversation）はインメモリ管理のため、アプリ再起動で消失する
- Genie API のフィードバックは rating（POSITIVE/NEGATIVE）のみ保存可能。テキストコメントは未サポート
- グラフ生成は Foundation Model API（`databricks-gpt-5-4-nano`）を使用するため、エンドポイントが利用可能であること

## 参考

- [Databricks Apps Docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/)
- [Databricks Apps Dependencies](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/dependencies)
- [Genie API Reference](https://docs.databricks.com/api/workspace/genie)
- [Slack Bolt for Python](https://slack.dev/bolt-python/)
