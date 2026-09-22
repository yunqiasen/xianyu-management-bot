import { useRef } from 'react'
import type { PointerEvent } from 'react'

type Point = { x: number; y: number }
export type VerificationPointerCommand = ({ action: 'click' } & Point) | { action: 'drag'; points: Point[] }

/** Forward only the account holder's gestures; no generated verification movements. */
export function VerificationPointerSurface({ src, disabled, onCommand }: {
  src: string
  disabled: boolean
  onCommand: (command: VerificationPointerCommand) => void
}) {
  const gesture = useRef<{ id: number; points: Point[] } | null>(null)
  const position = (event: PointerEvent<HTMLImageElement>): Point => {
    const image = event.currentTarget
    const rect = image.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(image.naturalWidth - 1, (event.clientX - rect.left) * image.naturalWidth / rect.width)),
      y: Math.max(0, Math.min(image.naturalHeight - 1, (event.clientY - rect.top) * image.naturalHeight / rect.height)),
    }
  }
  return <img src={src} alt="验证浏览器，支持点击和拖动" draggable={false}
    className="w-full rounded border select-none" style={{ touchAction: 'none' }}
    onPointerDown={event => {
      if (disabled || event.button !== 0 || !event.currentTarget.naturalWidth || gesture.current) return
      event.preventDefault()
      event.currentTarget.setPointerCapture(event.pointerId)
      gesture.current = { id: event.pointerId, points: [position(event)] }
    }}
    onPointerMove={event => {
      const current = gesture.current
      if (!current || current.id !== event.pointerId) return
      // Preserve the user's initial and final points within the server's bounded input.
      if (current.points.length < 127) current.points.push(position(event))
    }}
    onPointerUp={event => {
      const current = gesture.current
      if (!current || current.id !== event.pointerId) return
      gesture.current = null
      event.currentTarget.releasePointerCapture(event.pointerId)
      if (disabled) return
      const end = position(event)
      const start = current.points[0]
      current.points.push(end)
      if (current.points.some(point => Math.hypot(point.x - start.x, point.y - start.y) >= 4)) {
        onCommand({ action: 'drag', points: current.points })
      } else {
        onCommand({ action: 'click', ...end })
      }
    }}
    onPointerCancel={() => { gesture.current = null }}
    onLostPointerCapture={() => { gesture.current = null }}
  />
}
