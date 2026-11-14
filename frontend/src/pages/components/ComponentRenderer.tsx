import type { ReactNode } from 'react'
import { safeUrl } from './renderer-utils'

export type RenderAction = (event: { component: string; action: string; payload: Record<string, unknown> }) => void

const text = (value: unknown): string => (value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value))
const list = <T,>(value: unknown): T[] => (Array.isArray(value) ? (value as T[]) : [])
const record = (value: unknown): Record<string, unknown> => (value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {})

function Card({ data, onAction }: { data: Record<string, unknown>; onAction?: RenderAction }) {
  const action = record(data.action)
  const url = safeUrl(action.url)
  return (
    <article className="wr wr-card">
      <div aria-hidden="true" className="wr-card-image" />
      <div className="wr-card-body">
        <div className="wr-tags">{list<unknown>(data.tags).map((tag, index) => <span key={index}>{text(tag)}</span>)}</div>
        <strong>{text(data.name)}</strong>
        <small>{text(data.subtitle)}</small>
        <p className="wr-price">
          <b>{text(data.price)}</b>
          {data.originalPrice !== undefined && <del>{text(data.originalPrice)}</del>}
        </p>
        <p className="wr-meta"><span>★ {text(data.rating)}</span><span>{text(data.stock)}</span></p>
        {action.label !== undefined && (
          <button className="wr-button" onClick={() => onAction?.({ component: 'card', action: 'open', payload: { url } })} type="button">{text(action.label)}</button>
        )}
      </div>
    </article>
  )
}

function Comparison({ data }: { data: Record<string, unknown> }) {
  const features = list<unknown>(data.features)
  const products = list<unknown>(data.products).map(record)
  return (
    <article className="wr wr-comparison">
      <strong>{text(data.title)}</strong>
      <table>
        <thead>
          <tr><th />{products.map((product, index) => <th className={product.recommended ? 'recommended' : ''} key={index}>{text(product.name)}{product.recommended === true && <em>★</em>}</th>)}</tr>
        </thead>
        <tbody>
          {features.map((feature, row) => (
            <tr key={row}><th scope="row">{text(feature)}</th>{products.map((product, column) => <td className={product.recommended ? 'recommended' : ''} key={column}>{text(list<unknown>(product.values)[row])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </article>
  )
}

function Timeline({ data }: { data: Record<string, unknown> }) {
  const steps = list<unknown>(data.steps).map(record)
  const current = steps.findIndex((step) => step.done !== true)
  return (
    <article className="wr wr-timeline">
      <header><strong>{text(data.orderNo)}</strong><span>{text(data.status)}</span></header>
      <small>{text(data.carrier)} {text(data.trackingNo)} · {text(data.eta)}</small>
      <ol>
        {steps.map((step, index) => (
          <li className={step.done === true ? 'done' : index === current ? 'current' : ''} key={index}>
            <b>{text(step.label)}</b><span>{text(step.time)}{step.location ? ` · ${text(step.location)}` : ''}</span>
          </li>
        ))}
      </ol>
    </article>
  )
}

function Form({ data, onAction }: { data: Record<string, unknown>; onAction?: RenderAction }) {
  const dates = list<unknown>(data.dates).map(record)
  const slots = list<unknown>(data.slots).map(record)
  return (
    <article className="wr wr-form">
      <strong>{text(data.title)}</strong>
      <small>{text(data.service)}</small>
      <div className="wr-grid">{dates.map((item, index) => (
        <button aria-pressed={item.date === data.selectedDate} className={item.date === data.selectedDate ? 'selected' : ''} disabled={item.available !== true} key={index} onClick={() => onAction?.({ component: 'form', action: 'select_date', payload: { date: item.date } })} type="button">{text(item.weekday)} {text(item.date)}</button>
      ))}</div>
      <div className="wr-grid">{slots.map((item, index) => (
        <button aria-pressed={item.time === data.selectedSlot} className={item.time === data.selectedSlot ? 'selected' : ''} disabled={item.available !== true} key={index} onClick={() => onAction?.({ component: 'form', action: 'select_slot', payload: { time: item.time } })} type="button">{text(item.time)}</button>
      ))}</div>
      <button className="wr-button" onClick={() => onAction?.({ component: 'form', action: 'confirm', payload: { date: data.selectedDate, time: data.selectedSlot } })} type="button">{text(data.confirmLabel)}</button>
    </article>
  )
}

/** The built-in renderers (CHN-046). An unknown type falls back to a neutral placeholder. */
export function ComponentRenderer({ kind, data, placeholder, onAction }: { kind: string; data: unknown; placeholder?: ReactNode; onAction?: RenderAction }) {
  const value = record(data)
  if (kind === 'card') return <Card data={value} onAction={onAction} />
  if (kind === 'comparison') return <Comparison data={value} />
  if (kind === 'timeline') return <Timeline data={value} />
  if (kind === 'form') return <Form data={value} onAction={onAction} />
  return <article className="wr wr-generic"><strong>{text(value.title)}</strong><small>{placeholder ?? ''}</small></article>
}
