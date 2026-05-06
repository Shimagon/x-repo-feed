# x-repo-feed

@mijukumono_AI が X でリポストした GitHub リポジトリを 30 分おきに検出して、自分の Gmail に `[repo-feed]` 件名で転送するボット。

GitHub Actions cron + Playwright (X.com にログイン状態で訪問 → DOM scrape) + Nodemailer (Gmail SMTP)。

## ライフサイクル

```
30 min cron
  → Ubuntu runner
    → Playwright + injected X cookies で https://x.com/<user> 開く
      → article[data-testid="tweet"] 全部スキャン
        → socialContext で「Reposted」マーカーつき & github.com URL 含むやつだけ抽出
          → state.json で重複チェック
            → 新規だけ Gmail SMTP で taiseipaisen@gmail.com に送信 (件名 [repo-feed] <owner/repo>)
              → state.json 更新 → repo に commit & push
```

下流: 別途 Anthropic remote agent (`trig_01CujvdxGWi8EZcDAwNZFepm`) が毎朝 8 時にこの `[repo-feed]` メールをまとめて digest ドラフト作る。

## 必要な GitHub Secrets

`Settings → Secrets and variables → Actions` で:

| 名前 | 取得方法 |
|---|---|
| `X_USERNAME` | `mijukumono_AI` |
| `X_AUTH_TOKEN` | Mac の Chrome で x.com にログイン → DevTools (⌥⌘I) → Application → Cookies → x.com → `auth_token` の Value |
| `X_CT0` | 同上の `ct0` の Value |
| `GMAIL_USER` | `taiseipaisen@gmail.com` |
| `GMAIL_APP_PASSWORD` | https://myaccount.google.com/apppasswords で生成 (要 2 段階認証) |
| `FORWARD_TO` | (任意) 転送先。未設定なら `GMAIL_USER` と同じ |

## ローカルで動作確認

```bash
npm install
npx playwright install chromium

# cookie が生きてるか
X_USERNAME=mijukumono_AI X_AUTH_TOKEN=... X_CT0=... node scripts/smoke.mjs

# 全パイプライン dry run (送信せず println)
DRY_RUN=1 X_USERNAME=mijukumono_AI X_AUTH_TOKEN=... X_CT0=... \
  GMAIL_USER=taiseipaisen@gmail.com GMAIL_APP_PASSWORD=dummy \
  node scripts/poll.mjs
```

## メンテ

- **cookie 失効した時**: GitHub Actions の最新 run が `LOGIN_REQUIRED` で落ちる → Chrome で再ログイン → cookie 取り直し → secrets 更新 (30 秒)
- **X が DOM 変えた時**: `article[data-testid="tweet"]` セレクタが効かなくなったら `scripts/poll.mjs` の `fetchTimeline()` を更新
- **取りこぼし**: profile ページのトップ ~20 ツイートしか見ない。30 分以内に 20 件以上リポストする想定がないなら問題なし。あるなら scroll 回数を増やす。
