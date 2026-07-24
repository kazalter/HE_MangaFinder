import { useCallback, useEffect, useRef, useState } from 'react'

export function useAnimatedClose(onClose: () => void, durationMs = 180) {
  const [closing, setClosing] = useState(false)
  const onCloseRef = useRef(onClose)
  const timerRef = useRef<number | null>(null)
  onCloseRef.current = onClose

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
  }, [])

  const requestClose = useCallback(() => {
    if (closing) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      onCloseRef.current()
      return
    }
    setClosing(true)
    timerRef.current = window.setTimeout(() => onCloseRef.current(), durationMs)
  }, [closing, durationMs])

  return { closing, requestClose }
}
