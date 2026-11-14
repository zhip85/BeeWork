// T-059: widget.js renders the widgets of a reply as DOM (never markup), and sends what the visitor does on a component back as an event.
// Runs the real widget.js in jsdom with a scripted API (no stack needed).
//
//   mkdir -p /tmp/jsdom-scratch && cd /tmp/jsdom-scratch && npm i jsdom     # a scratch directory, not part of the product
//   NODE_PATH=/tmp/jsdom-scratch/node_modules node scripts/check_widget_components.mjs
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'

const require = createRequire(resolve(process.env.NODE_PATH ?? '.', 'x.js'))
const { JSDOM } = require('jsdom')

const PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
const widgets = [
  { tag: 'product_card', type: 'card', mode: 'native', name: '卡片', data: { name: PAYLOAD, subtitle: PAYLOAD, price: PAYLOAD, tags: [PAYLOAD], rating: 4.8, stock: 3, action: { label: PAYLOAD, url: 'javascript:alert(3)' } } },
  { tag: 'product_comparison', type: 'comparison', mode: 'native', data: { title: PAYLOAD, features: [PAYLOAD], products: [{ name: PAYLOAD, values: [PAYLOAD], recommended: true }] } },
  { tag: 'order_tracking', type: 'timeline', mode: 'native', data: { orderNo: PAYLOAD, status: PAYLOAD, carrier: 'c', trackingNo: 't', eta: 'e', steps: [{ label: PAYLOAD, time: 't', done: false }] } },
  { tag: 'appointment_picker', type: 'form', mode: 'native', data: { title: 'T', service: 'S', confirmLabel: '确认预约', dates: [{ date: '11-15', weekday: '周六', available: true }, { date: '11-16', weekday: '周日', available: false }], slots: [{ time: '10:00', available: true }] } },
]

const requests = []
const frame = (name, payload) => `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`
function fakeFetch(url, options = {}) {
  const path = new URL(url).pathname
  const body = options.body ? JSON.parse(options.body) : {}
  if (path === '/v1/widget/session') {
    return Promise.resolve(new Response(JSON.stringify({ token: 't', expires_in: 3600, visitor_id: 'visitor-0001', config: { assistant_name: '客服', welcome: '您好', primary_color: '#6558d9' } }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  requests.push(body)
  const sse = frame('start', { conversation_id: 'c1', run_id: 'r1' }) + frame('delta', { text: '推荐如下' })
    + frame('done', { message: { role: 'assistant', content: '推荐如下' }, usage: {}, citations: [], widgets: body.event ? [] : widgets })
  return Promise.resolve(new Response(sse, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
}

const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://www.example.com/', runScripts: 'outside-only', pretendToBeVisual: true })
const { window } = dom
window.fetch = fakeFetch
window.Response = Response
window.TextDecoder = TextDecoder
const opened = []
window.open = (url) => opened.push(url)
const script = window.document.createElement('script')
script.setAttribute('data-site-key', 'bw_site_test')
script.setAttribute('data-api-base', 'http://api.test')
Object.defineProperty(window.document, 'currentScript', { value: script, configurable: true })
window.eval(await readFile(resolve('backend/src/beework/static/widget.js'), 'utf8'))

const root = window.document.querySelector('[data-beework-widget]').shadowRoot
const until = async (predicate, label) => { for (let i = 0; i < 100; i += 1) { if (predicate()) return; await new Promise((r) => setTimeout(r, 20)) } assert.fail(label) }
window.BeeWorkWidget.open()
await until(() => root.querySelector('.bw-msg.bot'), 'welcome')
root.querySelector('.bw-input').value = '推荐一款耳机'
root.querySelector('.bw-form').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }))
await until(() => root.querySelectorAll('.bw-widget').length === 4, 'four widgets rendered')

const rendered = root.querySelector('.bw-list')
assert.equal(rendered.querySelectorAll('script, img').length, 0, 'no <script> or <img> element came out of the data')
assert.ok(![...rendered.querySelectorAll('*')].some((node) => [...node.attributes].some((a) => a.name.startsWith('on'))), 'no event handler attribute')
assert.ok(rendered.querySelector('.bw-widget').textContent.includes('<img src=x onerror="alert(1)">'), 'the payload is shown as text')
assert.equal(requests[0].message, '推荐一款耳机')

// a card button: a javascript: URL is never opened, the click is still reported as an event
const cardButton = rendered.querySelector('[data-component="product_card"] button')
cardButton.click()
await until(() => requests.length === 2, 'card event sent')
assert.deepEqual(opened, [], 'javascript: URLs are not opened')
assert.deepEqual(requests[1].event, { tag: 'product_card', action: 'open', data: { url: null } })
assert.equal(requests[1].message, undefined, 'an event replaces the message')

// the appointment form: an unavailable date is disabled, the choice goes back on confirm
const form = rendered.querySelector('[data-component="appointment_picker"]')
const [open, closed] = form.querySelectorAll('.bw-w-grid')[0].querySelectorAll('button')
assert.equal(closed.disabled, true)
await until(() => !root.querySelector('.bw-send').disabled, 'idle again')
open.click()
form.querySelectorAll('.bw-w-grid')[1].querySelector('button').click()
;[...form.querySelectorAll('button')].find((b) => b.textContent === '确认预约').click()
await until(() => requests.length === 3, 'confirm event sent')
assert.deepEqual(requests[2].event, { tag: 'appointment_picker', action: 'confirm', data: { date: '11-15', time: '10:00' } })
console.log('widget component rendering check passed')
window.close()
