// T-044: the real H5 page in a real browser engine at a phone viewport — open, send a message, see the streamed reply (NFR-015).
//
//   mkdir -p /tmp/pw-scratch && cd /tmp/pw-scratch && npm init -y && npm i playwright && npx playwright install chromium   # a scratch directory, not part of the product
//   docker compose exec -d -e E2E_H5_HOLD=180 api sh -c 'rm -f /tmp/e2e_h5.done; python /app/scripts/check_open_api_e2e.py > /tmp/e2e.log'
//   # wait for "H5_FIXTURE {...}" in /tmp/e2e.log (inside the api container), then:
//   NODE_PATH=/tmp/pw-scratch/node_modules node scripts/check_h5_mobile.mjs '<the H5_FIXTURE json>'
//   docker compose exec api touch /tmp/e2e_h5.done          # lets the e2e script finish and clean up
//
// Two engines/viewports: Chromium with an iPhone-sized viewport and with an Android-sized one (touch, device scale factor, mobile user agent).
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(resolve(process.env.NODE_PATH ?? '.', 'x.js'))
const { chromium, devices } = require('playwright')
const fixture = JSON.parse(process.argv[2])
const api = process.env.WIDGET_API ?? 'http://localhost:8000'
const log = (label, detail = '') => console.log(`[OK] ${label}${detail ? ` — ${detail}` : ''}`)

const browser = await chromium.launch()
try {
  for (const name of ['iPhone 13', 'Pixel 5']) {
    const context = await browser.newContext({ ...devices[name] })
    const page = await context.newPage()
    const problems = []
    page.on('pageerror', (error) => problems.push(error.message))
    page.on('console', (message) => { if (message.type() === 'error') problems.push(message.text()) })
    await page.goto(`${api}${fixture.path}`)

    // The widget lives in a shadow root; Playwright pierces open shadow roots with plain CSS selectors.
    await page.waitForSelector('.bw-msg.bot')
    assert.equal((await page.locator('.bw-msg.bot').first().textContent()).trim(), fixture.welcome)
    log(`${name}: the page opens by itself and shows the welcome text`)

    const box = await page.locator('.bw-panel').boundingBox()
    const viewport = page.viewportSize()
    assert.ok(box.width >= viewport.width - 1 && box.height >= viewport.height - 1, `full screen (${JSON.stringify(box)} in ${JSON.stringify(viewport)})`)
    assert.equal(await page.locator('.bw-fab').isVisible(), false)
    log(`${name}: full screen, no launcher`, `${Math.round(box.width)}×${Math.round(box.height)}`)

    const scroll = await page.evaluate(() => ({ x: document.documentElement.scrollWidth, w: window.innerWidth }))
    assert.ok(scroll.x <= scroll.w, `no horizontal scroll (${scroll.x} > ${scroll.w})`)
    const fontSize = await page.locator('.bw-input').evaluate((node) => parseFloat(getComputedStyle(node).fontSize))
    assert.ok(fontSize >= 16, 'the input is at least 16px, so iOS does not zoom in on focus')

    await page.locator('.bw-input').fill('你好，请问你们几点开门？')
    await page.locator('.bw-send').tap()
    await page.waitForFunction((answer) => [...document.querySelectorAll('[data-beework-widget]')].some((host) => [...host.shadowRoot.querySelectorAll('.bw-msg.bot')].some((n) => n.textContent === answer)), fixture.answer, { timeout: 60000 })
    log(`${name}: the streamed reply appears`, fixture.answer)
    assert.equal(await page.locator('.bw-msg.user').count(), 1)
    assert.deepEqual(problems, [], `no errors in the page: ${problems.join(' | ')}`)

    const remembered = await page.evaluate(() => Object.keys(window.localStorage).filter((k) => k.startsWith('bw_conversation_h5_')).length)
    assert.equal(remembered, 1)
    await page.reload()
    await page.waitForSelector('.bw-msg.bot')
    log(`${name}: the page opens again after a reload`)
    await context.close()
  }
  console.log('H5 mobile check passed')
} finally {
  await browser.close()
}
