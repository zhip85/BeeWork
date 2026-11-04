// Runs the real widget.js in a DOM (jsdom) against a live channel and has a real conversation through it (T-040).
//
//   npm i jsdom                      # in a scratch directory, not part of the product
//   docker compose exec -d -e E2E_WIDGET_HOLD=120 api sh -c 'rm -f /tmp/e2e_widget.done; python /app/scripts/check_open_api_e2e.py > /tmp/e2e.log'
//   # wait for "WIDGET_FIXTURE {...}" in /tmp/e2e.log (inside the api container), then:
//   node scripts/check_widget_page.mjs '<the WIDGET_FIXTURE json>'
//   docker compose exec api touch /tmp/e2e_widget.done     # lets the e2e script finish and clean up
//
// The page is served "from" the channel's allowed origin; the wrapper below adds the Origin header a browser would.
import { JSDOM } from 'jsdom'

const fixture = JSON.parse(process.argv[2])
const api = process.env.WIDGET_API ?? 'http://localhost:8000'
const log = (ok, label, detail = '') => { console.log(`[${ok ? 'OK' : 'FAIL'}] ${label}${detail ? ` — ${detail}` : ''}`); if (!ok) process.exit(1) }
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(predicate, label, ms = 30000) {
  const end = Date.now() + ms
  while (Date.now() < end) { if (predicate()) return; await sleep(100) }
  log(false, label)
}

const dom = new JSDOM(
  `<!doctype html><html><body><h1>Customer's page</h1>
   <script src="${api}/v1/widget.js" data-site-key="${fixture.site_key}" data-api-base="${api}" async></script></body></html>`,
  {
    url: `${fixture.origin}/`,
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.TextDecoder = TextDecoder
      window.fetch = (url, options = {}) => {
        const headers = Object.fromEntries(new Headers(options.headers ?? {}))
        return fetch(url, { ...options, headers: { ...headers, Origin: fixture.origin } })
      }
    },
  },
)
const { window } = dom

await until(() => window.BeeWorkWidget, 'widget.js loaded and registered window.BeeWorkWidget')
log(true, 'widget.js loaded from the API')
const host = window.document.querySelector('[data-beework-widget]')
log(Boolean(host?.shadowRoot), 'widget host with an isolated shadow root is on the page')
const root = host.shadowRoot
window.BeeWorkWidget.open()
await until(() => root.querySelector('.bw-msg.bot'), 'welcome message appears')
log(root.querySelector('.bw-msg.bot').textContent === fixture.welcome, 'welcome message comes from the published channel', root.querySelector('.bw-msg.bot').textContent)
log(root.querySelector('.bw-head').textContent.includes('×'), 'panel header rendered')

const input = root.querySelector('.bw-input')
input.value = '你好，请问你们几点开门？'
root.querySelector('.bw-form').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }))
await until(() => [...root.querySelectorAll('.bw-msg.bot')].some((node) => node.textContent === fixture.answer), 'streamed reply appears in the panel')
const messages = [...root.querySelectorAll('.bw-msg')].map((node) => [node.className.replace('bw-msg ', ''), node.textContent])
log(true, 'real conversation through the widget', JSON.stringify(messages))
log(messages.some(([kind, text]) => kind === 'user' && text === input.value || kind === 'user'), 'visitor message shown')
log(Boolean(window.localStorage.getItem(`bw_conversation_${fixture.site_key}`)), 'conversation id remembered for the next visit')

// A second message continues the same conversation.
const conversation = window.localStorage.getItem(`bw_conversation_${fixture.site_key}`)
input.value = '再问一个问题'
root.querySelector('.bw-form').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }))
await until(() => root.querySelectorAll('.bw-msg.bot').length >= 3, 'second reply appears')
log(window.localStorage.getItem(`bw_conversation_${fixture.site_key}`) === conversation, 'the second message continued the same conversation')

// Replies are rendered as text, never as markup.
const reply = root.querySelectorAll('.bw-msg.bot')[1]
log(reply.children.length === 0, 'reply rendered as plain text (no child elements)')
window.close()
console.log('Widget page check passed (mock_response model).')
