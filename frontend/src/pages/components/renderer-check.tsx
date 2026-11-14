import { renderToStaticMarkup } from 'react-dom/server'
import { ComponentRenderer } from './ComponentRenderer'
import { safeUrl } from './renderer-utils'

const PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>'

/** Entry for ``npm run test:renderer``: renders hostile payloads and reports the markup. */
export function run() {
  const card = renderToStaticMarkup(<ComponentRenderer kind="card" data={{ name: PAYLOAD, subtitle: PAYLOAD, price: PAYLOAD, tags: [PAYLOAD], rating: PAYLOAD, stock: PAYLOAD, action: { label: PAYLOAD, url: 'javascript:alert(3)' } }} />)
  const comparison = renderToStaticMarkup(<ComponentRenderer kind="comparison" data={{ title: PAYLOAD, features: [PAYLOAD], products: [{ name: PAYLOAD, values: [PAYLOAD], recommended: true }] }} />)
  const timeline = renderToStaticMarkup(<ComponentRenderer kind="timeline" data={{ orderNo: PAYLOAD, status: PAYLOAD, carrier: PAYLOAD, trackingNo: PAYLOAD, eta: PAYLOAD, steps: [{ label: PAYLOAD, time: PAYLOAD, location: PAYLOAD, done: false }] }} />)
  const form = renderToStaticMarkup(<ComponentRenderer kind="form" data={{ title: PAYLOAD, service: PAYLOAD, confirmLabel: PAYLOAD, dates: [{ date: PAYLOAD, weekday: PAYLOAD, available: true }], slots: [{ time: PAYLOAD, available: false }] }} />)
  const generic = renderToStaticMarkup(<ComponentRenderer kind="other" data={{ title: PAYLOAD }} />)
  const broken = renderToStaticMarkup(<ComponentRenderer kind="card" data="not an object" />)
  return {
    markup: { card, comparison, timeline, form, generic, broken },
    urls: [safeUrl('javascript:alert(1)'), safeUrl('data:text/html,x'), safeUrl('https://ok.example.com/a?b=1'), safeUrl('http://ok.example.com'), safeUrl(42), safeUrl('not a url')],
  }
}
