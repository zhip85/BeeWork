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
  async function streamChat(message, handlers, retried) {
    var current = await ensureSession()
    var response = await fetch(apiBase + '/v1/widget/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + current.token },
      body: JSON.stringify({ message: message, conversation_id: conversationId || undefined, stream: true }),
    })
    if (!response.ok) {
      var failure = await response.json().catch(function () { return {} })
      var code = failure.error && failure.error.code
      if (!retried && code === 'expired_token') { session = null; return streamChat(message, handlers, true) }
      if (code === 'conversation_not_found') { conversationId = null; write(STORE.conversation, null); return streamChat(message, handlers, true) }
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
        else if (name === 'done') { handlers.done(payload.message.content); return }
        else if (name === 'error') throw new Error(payload.message || '助理暂时无法回答')
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
  form.addEventListener('submit', async function (event) {
    event.preventDefault()
    var text = input.value.trim()
    if (!text || busy) return
    busy = true
    send.disabled = true
    input.value = ''
    addMessage('user', text)
    var reply = addMessage('bot', '…')
    var received = ''
    try {
      await streamChat(text, {
        delta: function (piece) { received += piece; reply.textContent = received; list.scrollTop = list.scrollHeight },
        done: function (full) { reply.textContent = full || received },
      })
    } catch (error) {
      reply.remove()
      addMessage('err', error.message)
    } finally {
      busy = false
      send.disabled = false
      input.focus()
    }
  })

  document.body.appendChild(host)
  window.BeeWorkWidget = { open: open, close: hide, version: 1 }
})()
