# gcloud-workspace-mcp

gcloud CLI の OAuth2 認証情報を使って Google Sheets / Drive API を呼び出す MCP サーバー。

## 提供ツール

| ツール名 | 説明 |
|---------|------|
| `read_sheet` | Sheets の URL/ID を受け取り、指定シートのデータをタブ区切りで返す |
| `list_sheets` | スプレッドシートのシート名一覧とプロパティを返す |
| `read_drive_file` | Drive ファイルの URL/ID を受け取り、内容をテキストで返す（Google Docs→plain text, Sheets→CSV） |
| `search_drive` | ファイル名キーワードで Drive を検索する |

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
| `GCLOUD_PATH` | `gcloud` | gcloud CLI の絶対パス（`/home/ge/.local/bin/gcloud` など） |

## 開発・ローカル実行

```bash
# 依存インストール
pip install -e .

# サーバー起動（stdio）
gcloud-workspace-mcp

# または直接
python -m gcloud_workspace_mcp.server
```
