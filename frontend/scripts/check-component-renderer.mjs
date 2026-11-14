// T-043: the built-in component renderers must escape everything a payload carries (NFR-009).
// Builds the renderer for Node with Vite, renders hostile payloads and checks the markup.
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'vite'

const out = await mkdtemp(join(tmpdir(), 'renderer-check-'))
try {
  await build({
    logLevel: 'error',
    build: { ssr: resolve('src/pages/components/renderer-check.tsx'), outDir: out, emptyOutDir: true, rollupOptions: { output: { format: 'esm', entryFileNames: 'check.mjs' } } },
    ssr: { noExternal: true },
  })
  const { run } = await import(pathToFileURL(join(out, 'check.mjs')).href)
  const { markup, urls } = run()
  for (const [name, html] of Object.entries(markup)) {
    assert.ok(!/<script/i.test(html), `${name}: a <script> element was rendered`)
    assert.ok(!/<img/i.test(html), `${name}: an <img> element was rendered from data`)
    assert.ok(!/<[a-z][^>]*\son[a-z]+=/i.test(html), `${name}: an element carries an event handler attribute`)
    assert.ok(!/javascript:/i.test(html), `${name}: a javascript: URL reached the markup`)
    if (name !== 'broken') assert.ok(html.includes('&lt;img'), `${name}: the payload text should appear escaped`)
  }
  assert.deepEqual(urls, [null, null, 'https://ok.example.com/a?b=1', 'http://ok.example.com/', null, null])
  console.log('component renderer check passed')
} finally {
  await rm(out, { recursive: true, force: true })
}
