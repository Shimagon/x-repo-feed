# x-repo-feed

X の特定アカウントのリポストを 30 分おきに監視し、含まれる GitHub リポジトリ URL を抽出して Gmail に転送するボット。
**GitHub Actions cron + Scrapling (StealthyFetcher) + Python smtplib** のみで構成された、依存ゼロのサーバーレス監視パイプライン。

## 📌 何を作ったか (What)

- X (旧 Twitter) の curator アカウントのプロフィールページを 30 分おきに polling
- リポスト (Reposted マーカー) のうち `github.com/owner/repo` を含むものだけを抽出
- 重複を除いて Gmail に `[repo-feed] <owner/repo>` 件名で送信
- 受信側 (個人 Gmail + 下流の AI agent) が「気になったリポを後でまとめて見る」キューとして使う

X 公式 API は repost イベントを有料 tier に閉じ込めているため、**ブラウザ自動化 + cookie 認証 + Gmail-as-queue** という素朴な構成で代替。

## 🧩 どういう設計で作ったか (Design)

```
GitHub Actions cron (*/30 * * * *)
  └─ Ubuntu runner
       ├─ pip install scrapling[all] + scrapling install --force
       ├─ python scripts/poll.py
       │    ├─ StealthyFetcher で https://x.com/<curator> を開く
       │    │   (Cloudflare 自動回避 / ad-block / disable_resources で軽量化)
       │    ├─ injected auth_token + ct0 cookie で認証
       │    ├─ article[data-testid="tweet"] を CSS で抽出
       │    ├─ Reposted マーカー × github.com URL でフィルタ
       │    ├─ state.json (直近 300 ID) で重複排除
       │    └─ smtplib SMTP_SSL:465 で Gmail に送信
       └─ state.json を commit & push
```

### 設計のポイント

| 観点 | 採用 | 理由 |
|---|---|---|
| **認証** | Cookie 直挿し (auth_token + ct0) | OAuth dance なし。失効しても 30 秒でローテ可能 |
| **状態** | `state.json` を repo にコミット | DB 不要。GitHub が永続化を担当 |
| **冪等性** | 直近 300 ID を保持して重複排除 | at-most-once 配信 |
| **多重起動防止** | `concurrency group` + 8 分 timeout | runner 暴走を防ぐ |
| **アンチボット対策** | Scrapling `StealthyFetcher` | Cloudflare Turnstile 自動突破、X の anti-bot もパス |

## 🎯 なぜ作ったか (Why)

- 気になった GitHub リポジトリを X で見かけても、後でまとめて見ることがほぼ無いまま流れる
- 「**X でリポストしたものだけ後でレビューしたい**」という個人ニーズを軽量にまかなうのに、X 公式 API は repost を有料 tier に隔離していて重い
- Gmail を queue として使えば、下流の AI agent が「未読 `[repo-feed]` を全部読んで digest 作る」という消費側を自由に組める
- ついでに「**ブラウザ自動化 + サーバーレス cron + メール queue**」という、個人スケールでよく出る組み合わせの素振りにもなる

## 🛠 技術スタック

- **Python 3.11+** / `requirements.txt` は scrapling のみ
- **Scrapling** (`StealthyFetcher`) — Cloudflare 自動回避 + CSS セレクタ抽出
- **GitHub Actions** (`*/30 * * * *` cron, Ubuntu runner)
- **Gmail SMTP** (`smtplib.SMTP_SSL`, port 465, App Password)

## 必要な GitHub Secrets

`Settings → Secrets and variables → Actions` で:

| 名前 | 取得方法 |
|---|---|
| `X_USERNAME` | 監視したい X アカウントのハンドル (`@` なし) |
| `X_AUTH_TOKEN` | Chrome で x.com にログイン → DevTools → Application → Cookies → x.com → `auth_token` の Value |
| `X_CT0` | 同上の `ct0` の Value |
| `GMAIL_USER` | 送信元 Gmail アドレス |
| `GMAIL_APP_PASSWORD` | <https://myaccount.google.com/apppasswords> で生成 (要 2 段階認証) |
| `FORWARD_TO` | (任意) 転送先。未設定なら `GMAIL_USER` と同じ |

## ローカルで動作確認

```bash
pip install -r requirements.txt
scrapling install --force   # 初回のみ (browser deps)

# 全パイプライン dry run (送信せず println)
DRY_RUN=1 \
  X_USERNAME=<your-x-handle> \
  X_AUTH_TOKEN=... X_CT0=... \
  GMAIL_USER=<your-gmail> GMAIL_APP_PASSWORD=dummy \
  python scripts/poll.py
```

## メンテ

- **Cookie 失効した時**: GitHub Actions の最新 run が `LOGIN_REQUIRED` で落ちる → Chrome で再ログイン → cookie 取り直し → secrets 更新 (30 秒)
- **X が DOM 変えた時**: `article[data-testid="tweet"]` セレクタが効かなくなったら `scripts/poll.py` の `fetch_tweets()` を更新 (Scrapling の `adaptive=True` を使えばかなり耐性つく)
- **取りこぼし**: profile ページのトップ ~20 ツイートしか見ない。30 分以内に 20 件以上リポストする想定がないなら問題なし。あるなら scroll 回数を増やす

## ライセンス

MIT — [LICENSE](LICENSE) 参照。

---

**作者**: [shimada / Shimagon](https://github.com/Shimagon) — AI 駆動開発を実践するソフトウェアエンジニア
