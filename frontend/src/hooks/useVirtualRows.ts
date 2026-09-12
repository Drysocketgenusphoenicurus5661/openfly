import { useCallback, useState } from 'react'

// Windowed rendering for long tables (375 replay steps). Fixed row height,
// a scroll container of known height, and a few rows of overscan.
export function useVirtualRows(
  count: number,
  rowHeight: number,
  viewportHeight: number,
  overscan = 6
) {
  const [scrollTop, setScrollTop] = useState(0)
  const onScroll = useCallback((event: React.UIEvent<HTMLElement>) => {
    setScrollTop(event.currentTarget.scrollTop)
  }, [])
  const visible = Math.ceil(viewportHeight / rowHeight)
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan)
  const end = Math.min(count, start + visible + overscan * 2)
  return {
    start,
    end,
    topPad: start * rowHeight,
    bottomPad: Math.max(0, (count - end) * rowHeight),
    onScroll,
  }
}
