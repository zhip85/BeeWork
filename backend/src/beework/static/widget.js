/*! BeeWork website widget — one self-contained script, no dependencies.
 *  Embed:  <script src="https://API_HOST/v1/widget.js" data-site-key="bw_site_..." async></script>
 *  Optional attributes: data-api-base (override the API address).
 */
(function () {
  'use strict'
  if (window.BeeWorkWidget) return

  var script = document.currentScript || document.querySelector('script[data-site-key]')
  if (!script) return
  var siteKey = script.getAttribute('data-site-key')
  if (!siteKey) return
  var apiBase = (script.getAttribute('data-api-base') || new URL(script.src, window.location.href).origin).replace(/\/$/, '')

  var STORE = { visitor: 'bw_visitor_' + siteKey, conversation: 'bw_conversation_' + siteKey }
  function read(key) { try { return window.localStorage.getItem(key) } catch (e) { return null } }
  function write(key, value) {
    try { if (value === null) window.localStorage.removeItem(key); else window.localStorage.setItem(key, value) } catch (e) { /* private mode */ }
  }

  var session = null // { token, visitorId, expiresAt, config }
  var conversationId = read(STORE.conversation)
  var busy = false

  // -- API ------------------------------------------------------------------------------------------------

  async function openSession() {
    var response = await fetch(apiBase + '/v1/widget/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ site_key: siteKey, visitor_id: read(STORE.visitor) || undefined }),
    })
    var body = await response.json().catch(function () { return {} })
    if (!response.ok) throw new Error((body.error && body.error.message) || '无法连接客服')
    write(STORE.visitor, body.visitor_id)
    session = { token: body.token, visitorId: body.visitor_id, expiresAt: Date.now() + (body.expires_in - 60) * 1000, config: body.config }
    return session
  }

  async function ensureSession() {
    if (!session || Date.now() >= session.expiresAt) await openSession()
    return session
  }

  // Reads the server-sent events of a streamed reply and reports them through the callbacks.
  async function streamChat(request, handlers, retried) {
    var current = await ensureSession()
    var response = await fetch(apiBase + '/v1/widget/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + current.token },
      body: JSON.stringify(Object.assign({ conversation_id: conversationId || undefined, stream: true }, request)),
    })
    if (!response.ok) {
      var failure = await response.json().catch(function () { return {} })
      var code = failure.error && failure.error.code
      if (!retried && code === 'expired_token') { session = null; return streamChat(request, handlers, true) }
      if (code === 'conversation_not_found') { conversationId = null; write(STORE.conversation, null); return streamChat(request, handlers, true) }
      throw new Error(response.status === 429 ? '消息发送太频繁，请稍后再试' : (failure.error && failure.error.message) || '发送失败')
    }
    var reader = response.body.getReader()
    var decoder = new TextDecoder()
    var buffer = ''
    for (;;) {
      var chunk = await reader.read()
      if (chunk.done) return
      buffer += decoder.decode(chunk.value, { stream: true })
      var boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        var block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        var name = ''
        var data = ''
        block.split('\n').forEach(function (line) {
          if (line.indexOf('event: ') === 0) name = line.slice(7)
          else if (line.indexOf('data: ') === 0) data += line.slice(6)
        })
        if (!name || !data) continue
        var payload = JSON.parse(data)
        if (name === 'start') { conversationId = payload.conversation_id; write(STORE.conversation, conversationId) }
        else if (name === 'delta') handlers.delta(payload.text)
        else if (name === 'done') { handlers.done(payload.message.content, payload.widgets || []); return }
        else if (name === 'error') throw new Error(payload.message || '智能体暂时无法回答')
      }
    }
  }

  // -- UI -----------------------------------------------------------------------------------------------------

  var host = document.createElement('div')
  host.setAttribute('data-beework-widget', '')
  var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host
  var color = '#6558d9'

  function el(tag, className, text) {
    var node = document.createElement(tag)
    if (className) node.className = className
    if (text) node.textContent = text // never innerHTML: replies are untrusted text
    return node
  }

  var style = el('style')
  style.textContent = [
    ':host{all:initial}',
    '.bw-fab{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;color:#fff;font-size:24px;box-shadow:0 6px 20px rgba(0,0,0,.25);z-index:2147483000}',
    '.bw-panel{position:fixed;right:20px;bottom:88px;width:360px;max-width:calc(100vw - 24px);height:520px;max-height:calc(100vh - 110px);display:none;flex-direction:column;background:#fff;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.28);overflow:hidden;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1c2321;z-index:2147483000}',
    '.bw-panel.open{display:flex}',
    '.bw-head{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;color:#fff;font-weight:600}',
    '.bw-close{background:none;border:0;color:#fff;font-size:20px;cursor:pointer;line-height:1}',
    '.bw-list{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f6f7f6}',
    '.bw-msg{max-width:85%;padding:8px 11px;border-radius:10px;white-space:pre-wrap;word-break:break-word}',
    '.bw-msg.bot{align-self:flex-start;background:#fff;border:1px solid #e3e8e5}',
    '.bw-msg.user{align-self:flex-end;color:#fff}',
    '.bw-msg.err{align-self:center;background:#fbe4e4;color:#9b2c2c;font-size:12.5px}',
    '.bw-form{display:flex;gap:8px;padding:10px;border-top:1px solid #e3e8e5;background:#fff}',
    '.bw-input{flex:1;resize:none;border:1px solid #cfd8d3;border-radius:8px;padding:8px;font:inherit;max-height:90px}',
    '.bw-send{border:0;border-radius:8px;padding:0 14px;color:#fff;cursor:pointer;font:inherit}',
    '.bw-send[disabled]{opacity:.5;cursor:default}',
    '.bw-widget{align-self:flex-start;max-width:92%;background:#fff;border:1px solid #e3e8e5;border-radius:10px;padding:10px;display:grid;gap:6px;font-size:13px}',
    '.bw-widget strong{font-size:14px}.bw-widget small{color:#667}.bw-w-tags span{display:inline-block;margin-right:4px;padding:0 6px;border-radius:99px;background:#eef}',
    '.bw-w-price{font-weight:700;color:#d33}.bw-w-price del{margin-left:6px;color:#889;font-weight:400}',
    '.bw-w-btn{justify-self:start;border:0;border-radius:6px;padding:4px 12px;color:#fff;cursor:pointer;font:inherit}',
    '.bw-w-grid{display:flex;flex-wrap:wrap;gap:4px}.bw-w-grid button{border:1px solid #cfd8d3;border-radius:6px;background:#fff;padding:3px 8px;cursor:pointer;font:inherit}',
    '.bw-w-grid button[disabled]{opacity:.4;cursor:default}.bw-w-grid button.sel{border-color:#6558d9;background:#eef}',
    '.bw-widget table{border-collapse:collapse;width:100%}.bw-widget th,.bw-widget td{padding:3px 6px;border-bottom:1px solid #e3e8e5;text-align:left;font-weight:400}',
    '.bw-widget .rec{background:#eef}.bw-widget ol{margin:0;padding-left:16px}.bw-widget li.done{color:#222}.bw-widget li{color:#889}.bw-widget li.cur{color:#6558d9;font-weight:600}',
  ].join('')

  var fab = el('button', 'bw-fab', '💬')
  fab.setAttribute('aria-label', 'Open chat')
  var panel = el('div', 'bw-panel')
  panel.setAttribute('role', 'dialog')
  var head = el('div', 'bw-head')
  var title = el('span', '', '在线客服')
  var close = el('button', 'bw-close', '×')
  close.setAttribute('aria-label', 'Close chat')
  head.appendChild(title)
  head.appendChild(close)
  var list = el('div', 'bw-list')
  var form = el('form', 'bw-form')
  var input = el('textarea', 'bw-input')
  input.rows = 1
  input.placeholder = '输入消息…'
  var send = el('button', 'bw-send', '发送')
  send.type = 'submit'
  form.appendChild(input)
  form.appendChild(send)
  panel.appendChild(head)
  panel.appendChild(list)
  panel.appendChild(form)
  root.appendChild(style)
  root.appendChild(fab)
  root.appendChild(panel)

  function paint() {
    fab.style.background = color
    head.style.background = color
    send.style.background = color
    Array.prototype.forEach.call(list.querySelectorAll('.bw-msg.user'), function (node) { node.style.background = color })
  }

  function addMessage(kind, text) {
    var node = el('div', 'bw-msg ' + kind, text)
    if (kind === 'user') node.style.background = color
    list.appendChild(node)
    list.scrollTop = list.scrollHeight
    return node
  }

  // -- Components (T-059): rendered from the widgets of a reply. Every value is set with textContent, never as markup. --------------------

  function safeUrl(value) {
    try { var url = new URL(String(value)); return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null } catch (e) { return null }
  }
  function str(value) { return value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value) }
  function arr(value) { return Array.isArray(value) ? value : [] }
  function obj(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {} }

  function renderWidget(widget, onEvent) {
    var data = obj(widget.data)
    var box = el('div', 'bw-widget')
    box.setAttribute('data-component', str(widget.tag))
    function button(label, className, handler) {
      var b = el('button', className, str(label))
      b.type = 'button'
      b.addEventListener('click', handler)
      return b
    }
    if (widget.type === 'card') {
      var tags = el('div', 'bw-w-tags')
      arr(data.tags).forEach(function (tag) { tags.appendChild(el('span', '', str(tag))) })
      box.appendChild(tags)
      box.appendChild(el('strong', '', str(data.name)))
      box.appendChild(el('small', '', str(data.subtitle)))
      var price = el('div', 'bw-w-price', str(data.price))
      if (data.originalPrice !== undefined) price.appendChild(el('del', '', str(data.originalPrice)))
      box.appendChild(price)
      box.appendChild(el('small', '', '★ ' + str(data.rating) + ' · ' + str(data.stock)))
      var action = obj(data.action)
      if (action.label !== undefined) {
        var go = button(action.label, 'bw-w-btn', function () {
          var url = safeUrl(action.url)
          if (url) window.open(url, '_blank', 'noopener')
          onEvent(widget.tag, 'open', { url: url }, str(action.label))
        })
        go.style.background = color
        box.appendChild(go)
      }
    } else if (widget.type === 'comparison') {
      box.appendChild(el('strong', '', str(data.title)))
      var table = el('table')
      var products = arr(data.products).map(obj)
      var headRow = el('tr')
      headRow.appendChild(el('th'))
      products.forEach(function (p) { var th = el('th', p.recommended ? 'rec' : '', str(p.name) + (p.recommended ? ' ★' : '')); headRow.appendChild(th) })
      table.appendChild(headRow)
      arr(data.features).forEach(function (feature, row) {
        var tr = el('tr')
        tr.appendChild(el('th', '', str(feature)))
        products.forEach(function (p) { tr.appendChild(el('td', p.recommended ? 'rec' : '', str(arr(p.values)[row]))) })
        table.appendChild(tr)
      })
      box.appendChild(table)
    } else if (widget.type === 'timeline') {
      box.appendChild(el('strong', '', str(data.orderNo) + ' · ' + str(data.status)))
      box.appendChild(el('small', '', str(data.carrier) + ' ' + str(data.trackingNo) + ' · ' + str(data.eta)))
      var steps = arr(data.steps).map(obj)
      var current = steps.findIndex(function (s) { return s.done !== true })
      var ol = el('ol')
      steps.forEach(function (step, index) {
        ol.appendChild(el('li', step.done === true ? 'done' : index === current ? 'cur' : '', str(step.label) + ' ' + str(step.time) + (step.location ? ' · ' + str(step.location) : '')))
      })
      box.appendChild(ol)
    } else if (widget.type === 'form') {
      var chosen = { date: data.selectedDate, time: data.selectedSlot }
      box.appendChild(el('strong', '', str(data.title)))
      box.appendChild(el('small', '', str(data.service)))
      var dates = el('div', 'bw-w-grid')
      var slots = el('div', 'bw-w-grid')
      function pick(group, key, value, node) {
        Array.prototype.forEach.call(group.children, function (c) { c.classList.remove('sel') })
        node.classList.add('sel')
        chosen[key] = value
      }
      arr(data.dates).map(obj).forEach(function (d) {
        var b = button(str(d.weekday) + ' ' + str(d.date), d.date === chosen.date ? 'sel' : '', function () { pick(dates, 'date', d.date, b) })
        b.disabled = d.available !== true
        dates.appendChild(b)
      })
      arr(data.slots).map(obj).forEach(function (sl) {
        var b = button(str(sl.time), sl.time === chosen.time ? 'sel' : '', function () { pick(slots, 'time', sl.time, b) })
        b.disabled = sl.available !== true
        slots.appendChild(b)
      })
      box.appendChild(dates)
      box.appendChild(slots)
      var confirm = button(data.confirmLabel || '确认', 'bw-w-btn', function () { onEvent(widget.tag, 'confirm', { date: chosen.date, time: chosen.time }, str(data.confirmLabel || '确认')) })
      confirm.style.background = color
      box.appendChild(confirm)
    } else {
      box.appendChild(el('strong', '', str(data.title || widget.name)))
    }
    return box
  }

  var started = false
  async function open() {
    panel.classList.add('open')
    input.focus()
    if (started) return
    started = true
    try {
      var current = await ensureSession()
      color = current.config.primary_color || color
      title.textContent = current.config.assistant_name || title.textContent
      paint()
      addMessage('bot', current.config.welcome)
    } catch (error) {
      started = false
      addMessage('err', error.message)
    }
  }
  function hide() { panel.classList.remove('open') }

  fab.addEventListener('click', function () { if (panel.classList.contains('open')) hide(); else open() })
  close.addEventListener('click', hide)
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true })) }
  })
  // One turn: ``request`` is {message} or {event}; ``label`` is what the visitor sees as their own bubble.
  async function converse(label, request) {
    if (busy) return
    busy = true
    send.disabled = true
    addMessage('user', label)
    var reply = addMessage('bot', '…')
    var received = ''
    try {
      await streamChat(request, {
        delta: function (piece) { received += piece; reply.textContent = received; list.scrollTop = list.scrollHeight },
        done: function (full, widgets) {
          var shown = full || received
          if (shown) reply.textContent = shown; else if (widgets.length) reply.remove()
          widgets.forEach(function (widget) {
            list.appendChild(renderWidget(widget, function (tag, action, data, text) { converse(text, { event: { tag: tag, action: action, data: data } }) }))
          })
          list.scrollTop = list.scrollHeight
        },
      })
    } catch (error) {
      reply.remove()
      addMessage('err', error.message)
    } finally {
      busy = false
      send.disabled = false
      input.focus()
    }
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault()
    var text = input.value.trim()
    if (!text || busy) return
    input.value = ''
    converse(text, { message: text })
  })

  document.body.appendChild(host)
  window.BeeWorkWidget = { open: open, close: hide, version: 2 }
})()
