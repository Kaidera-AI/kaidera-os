import { useEffect, useLayoutEffect, useRef } from 'react'

/**
 * Keep a scroll container pinned to its TAIL (latest content), robustly.
 *
 * Returns a ref to attach to the scrolling element. Behaviour:
 *  - While the user is at the bottom it follows new content (streaming tokens,
 *    appended turns). The instant they scroll UP to read history it stops yanking
 *    them; when they return to the bottom it re-pins.
 *  - When `resetKey` changes — a new conversation / run / feed, INCLUDING the first
 *    mount and a page refresh — it re-pins to the bottom so the latest message shows.
 *
 * Pass via `deps` every value whose change means "content may have grown" (the
 * transcript object, a segment count, a body length, …) so the pinned scroll
 * re-applies as content loads/streams.
 *
 * WHY THIS SHAPE — the root-cause fix for "refresh lands at the top", and why it is
 * ONE shared hook (the bug kept coming back because two views each had their own
 * fragile copy of this logic):
 *  - `useLayoutEffect`, not `useEffect`: the scroll lands BEFORE the browser paints,
 *    so there is no top-flash and the height read is accurate.
 *  - It re-applies on EVERY `deps` change while pinned. The old one-shot "first
 *    paint" scroll fired ONCE against a height that was not yet laid out (the REST
 *    first-paint before the SSE swap, markdown/layout still settling), scrolled to a
 *    stale small height, marked the run "pinned", then SKIPPED every later update —
 *    because it was no longer "first paint" and the user was not "near bottom" —
 *    stranding the view at the top. Re-applying while pinned self-corrects as the
 *    true height grows.
 *  - Pinned-ness is read from the user's ACTUAL scroll position via a passive
 *    listener, stored in a ref, so a scroll event never triggers a re-render.
 */
export function useStickToBottom<T extends HTMLElement>(
  resetKey: unknown,
  deps: unknown[],
  threshold = 120,
) {
  const ref = useRef<T>(null)
  const stick = useRef(true)
  // A unique sentinel so the FIRST resetKey (even null/'') always counts as changed
  // → the first mount re-pins to the bottom.
  const lastReset = useRef<unknown>(Symbol('init'))

  // Track whether the user is parked at the bottom. Re-attached when `resetKey`
  // changes — which is also when the scroll element first mounts, once content
  // exists (the views early-return a non-scrolling empty state before then).
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey])

  // Apply the pinned scroll on every content change (see WHY above).
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (resetKey !== lastReset.current) {
      lastReset.current = resetKey
      stick.current = true // new conversation / run / refresh → show the latest
    }
    if (stick.current) el.scrollTop = el.scrollHeight
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey, ...deps])

  return ref
}
