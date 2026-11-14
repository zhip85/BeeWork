// T-044: widget.js in H5 mode — the page is the conversation (full screen or bottom drawer), it signs in through /v1/h5/session, and it applies the
// experience the channel froze (theme, radius, streaming, tables, cards). Runs the real widget.js in jsdom with a scripted API.
//
//   NODE_PATH=/tmp/jsdom-scratch/node_modules node scripts/check_h5_page.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(resolve(process.env.NODE_PATH ?? '.', 'x.js'))
const { JSDOM } = require('jsdom')
const source = await readFile(resolve('backend/src/beework/static/widget.js'), 'utf8')
const frame = (name, payload) => `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`
const TABLE = '| 订单 | 状态 |\n|---|---|\n| A1 | 已发货 |'

async function page(config, widgets = []) {
  const calls = []
  const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://api.example.com/h5/acme/help', runScripts: 'outside-only', pretendToBeVisual: true })
  const { window } = dom
  window.TextDecoder = TextDecoder
  window.Response = Response
  window.fetch = (url, options = {}) => {
    const path = new URL(url).pathname
    const body = options.body ? JSON.parse(options.body) : {}
    calls.push({ path, body })
    if (path === '/v1/h5/session') return Promise.resolve(new Response(JSON.stringify({ token: 't', expires_in: 3600, visitor_id: 'visitor-0001', config: { assistant_name: '客服', welcome: '您好', primary_color: '#3568d4', ...config } }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const sse = frame('start', { conversation_id: 'c1', run_id: 'r1' }) + frame('delta', { text: '订单如下：\n' }) + frame('delta', { text: TABLE })
      + frame('done', { message: { role: 'assistant', content: `订单如下：\n${TABLE}` }, usage: {}, citations: [], widgets })
    return Promise.resolve(new Response(sse, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
  }
  const script = window.document.createElement('script')
  script.setAttribute('data-h5', 'acme/help')
  script.setAttribute('data-api-base', 'https://api.example.com')
  Object.defineProperty(window.document, 'currentScript', { value: script, configurable: true })
  window.eval(source)
  const root = window.document.querySelector('[data-beework-widget]').shadowRoot
  const until = async (predicate, label) => { for (let i = 0; i < 100; i += 1) { if (predicate()) return; await new Promise((r) => setTimeout(r, 20)) } assert.fail(label) }
  await until(() => root.querySelector('.bw-msg.bot'), 'welcome message')
  return { window, root, calls, until }
}

async function ask(h5) {
  h5.root.querySelector('.bw-input').value = '我的订单'
  h5.root.querySelector('.bw-form').dispatchEvent(new h5.window.Event('submit', { cancelable: true, bubbles: true }))
  await h5.until(() => !h5.root.querySelector('.bw-send').disabled && h5.root.querySelectorAll('.bw-msg.bot').length >= 2, 'reply finished')
  return [...h5.root.querySelectorAll('.bw-msg.bot')].at(-1).textContent
}

// 1. full screen: signs in through the H5 endpoint with the tenant and path, opens by itself, no launcher button, square corners
let h5 = await page({ entry: 'fullscreen', radius: 16 })
assert.deepEqual(h5.calls[0], { path: '/v1/h5/session', body: { tenant: 'acme', path: '/help' } })
const panel = h5.root.querySelector('.bw-panel')
assert.ok(panel.classList.contains('open') && panel.classList.contains('h5-full'), 'opened full screen')
assert.equal(h5.root.querySelector('.bw-fab').style.display, 'none', 'no launcher on an H5 page')
assert.equal(panel.style.borderRadius, '0px', 'a full-screen page has no rounded corners')
assert.equal(h5.root.querySelector('.bw-msg.bot').textContent, '您好')
assert.equal(h5.window.BeeWorkWidget.version, 3)

// 2. bottom drawer, dark theme, tables off: the table becomes lines, nothing is shown until the whole reply is there when streaming is off
h5 = await page({ entry: 'drawer', theme: 'dark', radius: 8, tables: false, streaming: false })
const drawer = h5.root.querySelector('.bw-panel')
assert.ok(drawer.classList.contains('h5-drawer') && drawer.classList.contains('dark'))
assert.equal(drawer.style.borderRadius, '8px')
const reply = await ask(h5)
assert.equal(reply, '订单如下：\n- 订单：A1；状态：已发货', 'the table is shown as a list')
assert.equal(h5.calls.at(-1).path, '/v1/widget/chat', 'the conversation goes through the same chat endpoint as the website widget')

// 3. streaming shows pieces as they arrive (checked through the final text: the page is idle again and the table stays a table when tables are on)
h5 = await page({ entry: 'fullscreen' })
assert.ok((await ask(h5)).includes('| 订单 | 状态 |'), 'tables stay as they are when the switch is on')

// 4. the cards and forms switches decide which components are shown
const card = { tag: 'product_card', type: 'card', mode: 'native', name: 'c', data: { name: 'N', price: '1', tags: [], rating: 1, stock: 1 } }
const form = { tag: 'appointment_picker', type: 'form', mode: 'native', name: 'f', data: { title: 'F', service: 'S', confirmLabel: 'OK', dates: [], slots: [] } }
h5 = await page({ cards: false }, [card, form])
await ask(h5)
assert.deepEqual([...h5.root.querySelectorAll('.bw-widget')].map((node) => node.getAttribute('data-component')), ['appointment_picker'])
h5 = await page({ forms: false }, [card, form])
await ask(h5)
assert.deepEqual([...h5.root.querySelectorAll('.bw-widget')].map((node) => node.getAttribute('data-component')), ['product_card'])

// 5. a website widget is unchanged: the launcher shows, nothing opens by itself, left-bottom is honoured
const site = new JSDOM('<!doctype html><body></body>', { url: 'https://www.example.com/', runScripts: 'outside-only', pretendToBeVisual: true })
site.window.fetch = () => Promise.reject(new Error('no network in this check'))
const tag = site.window.document.createElement('script')
tag.setAttribute('data-site-key', 'bw_site_x')
tag.setAttribute('data-api-base', 'https://api.example.com')
Object.defineProperty(site.window.document, 'currentScript', { value: tag, configurable: true })
site.window.eval(source)
const siteRoot = site.window.document.querySelector('[data-beework-widget]').shadowRoot
assert.ok(!siteRoot.querySelector('.bw-panel').classList.contains('open'))
assert.notEqual(siteRoot.querySelector('.bw-fab').style.display, 'none')
console.log('H5 page check passed')
