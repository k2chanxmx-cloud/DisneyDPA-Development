# Disney DPA Prediction API Ver4.1.1 Complete

Renderへそのまま配置できる、APIとWeb画面を含む完全版です。

## 含まれるもの
- Flask API
- `templates/index.html`
- `static/css/style.css`
- `static/js/app.js`
- favicon
- Supabase接続
- Yosocal取得
- 予測ログ・自己学習
- Render設定

## Render環境変数
- `APP_ENV`：開発は `development`、本番は `production`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`（予測ログ保存に推奨）
- `REQUEST_TIMEOUT`（任意、既定15秒）

## Render設定
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

## URL
- `/` Web画面
- `/api/status` 状態確認
- `/api/forecast?date=2026-08-15&entry_time=10:00` 予測JSON
- `/api/analytics` 分析JSON
- `/api/database` 実績JSON

## 配置時の注意
ZIP内のファイル・フォルダを、GitHubリポジトリ直下へ置いてください。`templates` と `static` を削除しないでください。
