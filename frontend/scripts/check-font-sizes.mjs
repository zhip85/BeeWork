// Fails when a stylesheet sets a hard-coded px font size: all sizes must come from the tokens in index.css.
import { readFileSync } from 'node:fs'

const files = ['src/App.css', 'src/design-system.css']
let failed = false
for (const file of files) {
  const lines = readFileSync(file, 'utf8').split('\n')
  lines.forEach((line, index) => {
    if (/font-size:\s*[0-9.]+px/.test(line)) {
      console.error(`${file}:${index + 1}: use a var(--fs-*) token instead of a px font size`)
      failed = true
    }
  })
}
if (failed) process.exit(1)
console.log('font size check passed')
