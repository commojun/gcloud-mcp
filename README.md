# gcloud-workspace-mcp

gcloud CLI の OAuth2 認証情報を使って Google Sheets / Drive API を呼び出す MCP サーバー。

## 提供ツール

### 読み取り

| ツール名 | 説明 |
|---------|------|
| `read_sheet` | Sheets の URL/ID を受け取り、セルアドレス付きの JSON 形式でデータを返す。`show_formulas=true` で数式文字列を取得可能 |
| `list_sheets` | スプレッドシートのシート名一覧とプロパティ（行数・列数・非表示フラグ）を返す |
| `read_drive_file` | Drive ファイルの URL/ID を受け取り、内容をテキストで返す（Google Docs→plain text, Sheets→CSV, Slides→plain text） |
| `search_drive` | ファイル名キーワードで Drive を検索する。フォルダ絞り込み・Shared Drive 対応 |

### 書き込み

| ツール名 | 説明 |
|---------|------|
| `update_sheet` | 指定範囲に値を書き込む（上書き）。`value_input_option` で数式解釈の有無を制御 |
| `append_rows` | シートの末尾（最終データ行の直後）に行を追加する |
| `clear_range` | 指定範囲の値を消去する（書式は保持） |

## 前提条件

- `gcloud` CLI がインストールされていること
- Drive スコープでログイン済みであること

```bash
gcloud auth login --enable-gdrive-access
```

## インストール・MCP 登録

```bash
# リポジトリをクローン
git clone https://github.com/commojun/gcloud-workspace-mcp ~/git/gcloud-mcp

# Claude Code に MCP サーバーとして登録
claude mcp add gcloud-workspace --scope user \
  -- uvx --from ~/git/gcloud-mcp gcloud-workspace-mcp
```

## 環境変数

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `GCLOUD_PATH` | `gcloud` | gcloud CLI の絶対パス（`/home/user/.local/bin/gcloud` など） |

## 開発・ローカル実行

```bash
# 依存インストール
pip install -e .

# サーバー起動（stdio）
gcloud-workspace-mcp

# または直接
python -m gcloud_workspace_mcp.server
```
