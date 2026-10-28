// Fails when zh-CN and en dictionaries differ in namespaces or keys.
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'i18n')
const LOCALES = ['zh-CN', 'en']

function flatten(value, prefix = '') {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key
    return child && typeof child === 'object' ? flatten(child, path) : [path]
  })
}

function load(locale) {
  const dir = join(root, locale)
  const files = readdirSync(dir).filter((name) => name.endsWith('.json')).sort()
  return Object.fromEntries(
    files.map((name) => [name.replace(/\.json$/, ''), new Set(flatten(JSON.parse(readFileSync(join(dir, name), 'utf8'))))]),
  )
}

const [base, other] = LOCALES.map(load)
const problems = []
for (const ns of new Set([...Object.keys(base), ...Object.keys(other)])) {
  if (!base[ns] || !other[ns]) {
    problems.push(`namespace "${ns}" exists only in ${base[ns] ? LOCALES[0] : LOCALES[1]}`)
    continue
  }
  for (const key of base[ns]) if (!other[ns].has(key)) problems.push(`${ns}:${key} missing in ${LOCALES[1]}`)
  for (const key of other[ns]) if (!base[ns].has(key)) problems.push(`${ns}:${key} missing in ${LOCALES[0]}`)
}

if (problems.length) {
  console.error(`i18n check failed:\n  ${problems.join('\n  ')}`)
  process.exit(1)
}
console.log(`i18n check passed (${Object.keys(base).length} namespaces).`)
