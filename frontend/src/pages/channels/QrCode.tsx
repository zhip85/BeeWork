import { useMemo } from 'react'
import qrcode from 'qrcode-generator'

/** A QR code drawn as SVG rects (no innerHTML), generated in the browser from the text (CHN-026: the H5 link). */
export function QrCode({ text, label, size = 168 }: { text: string; label: string; size?: number }) {
  const cells = useMemo(() => {
    const code = qrcode(0, 'M') // type number 0: the smallest version that fits
    code.addData(text)
    code.make()
    const count = code.getModuleCount()
    const dark: [number, number][] = []
    for (let row = 0; row < count; row += 1) for (let column = 0; column < count; column += 1) if (code.isDark(row, column)) dark.push([column, row])
    return { count, dark }
  }, [text])
  const quiet = 2 // modules of white around the code, as the standard asks for
  const span = cells.count + quiet * 2
  return (
    <svg aria-label={label} className="qr-code" height={size} role="img" viewBox={`0 0 ${span} ${span}`} width={size} xmlns="http://www.w3.org/2000/svg">
      <rect fill="#fff" height={span} width={span} />
      {cells.dark.map(([x, y]) => <rect fill="#000" height="1" key={`${x}-${y}`} width="1" x={x + quiet} y={y + quiet} />)}
    </svg>
  )
}
