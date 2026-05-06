// Local smoke test: verify cookies work and the parser sees your timeline.
// Run with: DRY_RUN=1 X_USERNAME=mijukumono_AI X_AUTH_TOKEN=... X_CT0=... GMAIL_USER=... GMAIL_APP_PASSWORD=... node scripts/smoke.mjs

import { chromium } from 'playwright';

const X_USERNAME = process.env.X_USERNAME;
const X_AUTH_TOKEN = process.env.X_AUTH_TOKEN;
const X_CT0 = process.env.X_CT0;

if (!X_USERNAME || !X_AUTH_TOKEN || !X_CT0) {
  console.error('Set X_USERNAME, X_AUTH_TOKEN, X_CT0 env vars first.');
  process.exit(1);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
});
await context.addCookies([
  { name: 'auth_token', value: X_AUTH_TOKEN, domain: '.x.com', path: '/', httpOnly: true, secure: true, sameSite: 'None' },
  { name: 'ct0', value: X_CT0, domain: '.x.com', path: '/', httpOnly: false, secure: true, sameSite: 'Lax' },
]);
const page = await context.newPage();
await page.goto(`https://x.com/${X_USERNAME}`, { waitUntil: 'domcontentloaded' });
try {
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 20000 });
} catch {
  console.error('No tweets rendered. Cookies may be invalid.');
  await page.screenshot({ path: 'smoke-fail.png', fullPage: false });
  await browser.close();
  process.exit(2);
}
const count = await page.$$eval('article[data-testid="tweet"]', a => a.length);
console.log(`OK — saw ${count} tweets on @${X_USERNAME}'s profile.`);
const reposts = await page.$$eval('article[data-testid="tweet"]', articles =>
  articles.filter(a => /repost|retweet|リポスト|リツイート/i.test(a.querySelector('[data-testid="socialContext"]')?.textContent || '')).length
);
console.log(`Of those, ${reposts} look like reposts.`);
await browser.close();
