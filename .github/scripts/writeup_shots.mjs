// THE WRITE-UP CARDS (BATCH-GAME-WRITEUP, 2026-10-04). A featured NFL game's
// X write-up carries each called player's card as an image: the LIVE player
// page, photographed, cropped to the card -- no mock render. Runs from
// .github/workflows/writeup-shots.yml before each featured game.
//
//   SITE  the site (default https://dashnetwork.vercel.app)
//   OUT   where the PNGs go (default /tmp/writeup-shots)
//   PLAYER + GAME  one shot by hand (workflow_dispatch test), skips the window
//
// SIGNED OUT, ALWAYS: a fresh browser context per player, no storage, no
// cookies -- so nothing personal ("You starred him N nights") can reach a post.
// A card that still carries a personal line is refused, not published.
// File name: <game_id>_<player_id>.png (the site's lib/writeups/post.js shotUrl).
// The page: /app#sport=nfl&tab=boards&card=<id>&cm=TD -- the player card on the TD market.
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const SITE = (process.env.SITE || 'https://dashnetwork.vercel.app').replace(/\/+$/, '')
const OUT = process.env.OUT || '/tmp/writeup-shots'
const WINDOW = [95 * 60e3, 150 * 60e3]   // shoot 95-150 min before kickoff; the post goes at 75-60
// a line about THIS viewer (the empty 'My note' box every card shows is a label, not personal)
const PERSONAL = /you starred|you highlighted|signed in as|nights? on your/i
mkdirSync(OUT, { recursive: true })

let jobs = []
if (process.env.PLAYER) {
  jobs = [{ game_id: process.env.GAME || 'test', player_id: process.env.PLAYER }]
} else {
  const r = await fetch(`${SITE}/api/writeups/featured?sport=nfl`, { cache: 'no-store' })
  const j = r.ok ? await r.json() : { games: [] }
  const now = Date.now()
  for (const g of j.games || []) {
    const lead = new Date(g.kickoff).getTime() - now
    if (g.state === 'pre' || g.state == null) {
      if (lead >= WINDOW[0] && lead <= WINDOW[1]) for (const p of g.players || []) jobs.push({ game_id: g.game_id, player_id: p.player_id })
    }
  }
}
if (!jobs.length) { console.log('no featured game in the shot window -- nothing to do'); process.exit(0) }

const browser = await chromium.launch()
let made = 0, refused = 0
for (const job of jobs) {
  const ctx = await browser.newContext({ viewport: { width: 430, height: 932 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  const page = await ctx.newPage()
  try {
    // his card opened ON THE TD MARKET (cm=TD) -- the write-up is about touchdowns, and the
    // players page opens on his best market instead (Jeanty: rushing yards)
    await page.goto(`${SITE}/app#sport=nfl&tab=boards&card=${encodeURIComponent(job.player_id)}&cm=TD`, { waitUntil: 'domcontentloaded', timeout: 45000 })
    const card = page.locator('[role=dialog]').first()
    await card.waitFor({ state: 'visible', timeout: 30000 })
    await page.waitForTimeout(3500)   // faces, numbers and tiles settle
    const text = await card.innerText()
    const note = await card.locator('textarea').evaluateAll((ts) => ts.map((t) => t.value).join('').trim()).catch(() => '')
    if (PERSONAL.test(text) || note) { refused++; console.log(`REFUSED ${job.player_id}: a personal line is on the card`); continue }
    // the site's fixed tab bar is chrome over the card, not part of it
    await page.addStyleTag({ content: 'nav.mobileTabBar{display:none !important}' })
    // the card's head: name, scores, TD rates, price -- down to its tab row (Overview /
    // Matchup / Splits); below that is the viewer's own pick card, never part of a post
    const box = await card.boundingBox()
    const tabs = await card.getByRole('button', { name: /^Overview$/ }).first().boundingBox().catch(() => null)
    const height = Math.round(Math.min(tabs ? tabs.y - box.y - 8 : 900, 1000))
    await page.screenshot({ path: `${OUT}/${job.game_id}_${job.player_id}.png`, clip: { x: box.x, y: box.y, width: box.width, height } })
    made++
    console.log(`shot ${job.game_id}_${job.player_id}.png (${Math.round(box.width)}x${height})`)
  } catch (e) {
    console.log(`FAILED ${job.player_id}: ${e.message}`)
  } finally { await ctx.close() }
}
await browser.close()
console.log(`${made} card(s), ${refused} refused, of ${jobs.length}`)
process.exit(made || !jobs.length ? 0 : 1)
