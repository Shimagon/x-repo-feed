import { chromium } from 'playwright';
import nodemailer from 'nodemailer';
import { readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';

const X_USERNAME = process.env.X_USERNAME;
const X_AUTH_TOKEN = process.env.X_AUTH_TOKEN;
const X_CT0 = process.env.X_CT0;
const GMAIL_USER = process.env.GMAIL_USER;
const GMAIL_APP_PASSWORD = process.env.GMAIL_APP_PASSWORD;
const FORWARD_TO = process.env.FORWARD_TO || GMAIL_USER;
const STATE_FILE = process.env.STATE_FILE || 'state.json';
const DRY_RUN = process.env.DRY_RUN === '1';

for (const [k, v] of Object.entries({ X_USERNAME, X_AUTH_TOKEN, X_CT0, GMAIL_USER, GMAIL_APP_PASSWORD })) {
  if (!v) {
    console.error(`Missing required env: ${k}`);
    process.exit(1);
  }
}

async function loadState() {
  if (!existsSync(STATE_FILE)) return { processed_ids: [] };
  try { return JSON.parse(await readFile(STATE_FILE, 'utf-8')); }
  catch { return { processed_ids: [] }; }
}

async function saveState(state) {
  state.processed_ids = state.processed_ids.slice(-300);
  state.last_run_at = new Date().toISOString();
  await writeFile(STATE_FILE, JSON.stringify(state, null, 2) + '\n');
}

async function fetchTimeline() {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 1800 },
    locale: 'ja-JP',
  });

  await context.addCookies([
    { name: 'auth_token', value: X_AUTH_TOKEN, domain: '.x.com', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
    { name: 'ct0', value: X_CT0, domain: '.x.com', path: '/', httpOnly: false, secure: true, sameSite: 'Lax' },
  ]);

  const page = await context.newPage();
  console.log(`Visiting https://x.com/${X_USERNAME}`);
  await page.goto(`https://x.com/${X_USERNAME}`, { waitUntil: 'domcontentloaded', timeout: 45000 });

  try {
    await page.waitForSelector('article[data-testid="tweet"]', { timeout: 20000 });
  } catch {
    const html = await page.content();
    if (html.includes('Log in to X') || html.includes('ログイン')) {
      throw new Error('LOGIN_REQUIRED: cookies appear invalid or expired');
    }
    throw new Error('No tweets rendered within 20s');
  }

  for (let i = 0; i < 3; i++) {
    await page.evaluate(() => window.scrollBy(0, 1500));
    await page.waitForTimeout(1200);
  }

  const tweets = await page.$$eval('article[data-testid="tweet"]', (articles, viewer) => {
    return articles.map(a => {
      const statusLink = Array.from(a.querySelectorAll('a[href*="/status/"]'))
        .map(l => l.getAttribute('href'))
        .find(h => /\/status\/\d+/.test(h));
      const idMatch = statusLink?.match(/\/status\/(\d+)/);
      const id = idMatch?.[1];

      const ctxEl = a.querySelector('[data-testid="socialContext"]');
      const ctxText = (ctxEl?.textContent || '').trim();
      // On the user's own profile, socialContext indicates a repost.
      // Don't require the username inside ctxText — X sometimes drops it.
      const isRepost = /repost|retweet|リポスト|リツイート/i.test(ctxText);

      const tweetText = (a.querySelector('[data-testid="tweetText"]')?.textContent || '').trim();

      const hrefs = Array.from(a.querySelectorAll('a[href]')).map(l => l.getAttribute('href')).filter(Boolean);
      const expandedHrefs = Array.from(a.querySelectorAll('a[href]'))
        .map(l => l.getAttribute('aria-label') || l.textContent || l.getAttribute('href'))
        .filter(Boolean);
      const allHrefs = [...hrefs, ...expandedHrefs].join(' ');
      const githubMatches = allHrefs.match(/https?:\/\/github\.com\/[\w.-]+\/[\w.-]+/gi) || [];

      return { id, isRepost, ctxText, tweetText, githubMatches };
    });
  }, X_USERNAME);

  await browser.close();
  return tweets.filter(t => t.id);
}

function normalizeGithubUrl(u) {
  const m = u.match(/github\.com\/([\w.-]+)\/([\w.-]+)/i);
  if (!m) return null;
  const [, owner, repo] = m;
  return `https://github.com/${owner}/${repo.replace(/\.git$/i, '').replace(/[.,;:]+$/, '')}`;
}

async function sendOne(tweet, repos) {
  const transporter = nodemailer.createTransport({
    service: 'gmail',
    auth: { user: GMAIL_USER, pass: GMAIL_APP_PASSWORD },
  });

  const slug = repos[0].replace('https://github.com/', '');
  const subject = `[repo-feed] ${slug}${repos.length > 1 ? ` (+${repos.length - 1})` : ''}`;
  const body = [
    `Reposted by @${X_USERNAME}`,
    `Tweet: https://x.com/i/status/${tweet.id}`,
    '',
    'Repos:',
    ...repos.map(r => `  ${r}`),
    '',
    '--- tweet text ---',
    tweet.tweetText || '(no text)',
  ].join('\n');

  if (DRY_RUN) {
    console.log(`[DRY] would send: ${subject}\n${body}\n`);
    return;
  }
  await transporter.sendMail({ from: GMAIL_USER, to: FORWARD_TO, subject, text: body });
  console.log(`Sent: ${subject}`);
}

async function main() {
  const state = await loadState();
  const seen = new Set(state.processed_ids);
  const tweets = await fetchTimeline();
  console.log(`Fetched ${tweets.length} tweets from profile`);
  const repostCount = tweets.filter(t => t.isRepost).length;
  const ghCount = tweets.filter(t => t.githubMatches.length > 0).length;
  console.log(`  reposts detected: ${repostCount}, with github URL: ${ghCount}`);
  if (process.env.DEBUG === '1') {
    for (const t of tweets) {
      console.log(`  tweet ${t.id} repost=${t.isRepost} ctx="${t.ctxText}" gh=${t.githubMatches.length}`);
    }
  }

  const candidates = tweets
    .filter(t => t.isRepost && t.githubMatches.length > 0)
    .filter(t => !seen.has(t.id));

  console.log(`New GitHub-containing reposts: ${candidates.length}`);

  for (const t of candidates) {
    const repos = [...new Set(t.githubMatches.map(normalizeGithubUrl).filter(Boolean))];
    if (repos.length === 0) continue;
    try {
      await sendOne(t, repos);
      state.processed_ids.push(t.id);
    } catch (e) {
      console.error(`Failed ${t.id}: ${e.message}`);
    }
  }

  await saveState(state);
  console.log('Done.');
}

main().catch(e => {
  console.error('FATAL:', e);
  process.exit(1);
});
