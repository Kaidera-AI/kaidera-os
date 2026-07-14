/**
 * The Kaidera isometric-cube mark — reused verbatim from the official logo
 * (`app/static/kaidera-logo-official-white.svg`, the two cube paths), tightly
 * cropped to the cube glyph and recoloured to the mint accent via `currentColor`.
 * This is the SPA's logo; the wordmark lives in the brand header next to it.
 *
 * The viewBox is cropped to the cube's bounds in the source artwork
 * (x≈14.66‑53.89, y≈10.57‑54.71) so the glyph fills the box with no wordmark.
 */

interface LogoProps {
  className?: string
  title?: string
}

export function CubeMark({ className, title = 'Kaidera OS' }: LogoProps) {
  return (
    <svg
      viewBox="14.4 10.3 39.9 44.9"
      className={className}
      role="img"
      aria-label={title}
      fill="currentColor"
    >
      <title>{title}</title>
      <path d="M34.27,10.57l-19.61,11.33v22.65l17.61,10.16v-11.91s-5.52-4.2-5.52-4.2c.06-.23.09-.47.09-.72,0-1.81-1.47-3.28-3.29-3.28s-3.28,1.47-3.28,3.28,1.47,3.29,3.28,3.29c.6,0,1.17-.16,1.65-.45l4.45,3.38v5.42l-11.68-6.74v-18.82l16.3-9.41,16.3,9.41v18.82l-11.67,6.73v-3.25l6.23-3.58v-9.71c1.2-.48,2.05-1.66,2.05-3.03,0-1.82-1.47-3.29-3.28-3.29-.63,0-1.23.18-1.73.5l-5.1-4.58c.08-.29.13-.59.13-.91,0-1.82-1.47-3.29-3.29-3.29s-3.28,1.47-3.28,3.29,1.47,3.28,3.28,3.28c.53,0,1.04-.13,1.48-.35l5.28,4.74c-.04.2-.06.4-.06.61,0,1.31.77,2.44,1.89,2.96v8.11l-6.23,3.5v10.14l3.04-1.69,14.58-8.41v-22.65l-19.62-11.33ZM23.55,38.81c-.51,0-.93-.41-.93-.93s.42-.93.93-.93.93.42.93.93-.41.93-.93.93ZM33.91,22.58c-.51,0-.93-.41-.93-.92s.42-.93.93-.93.93.41.93.93-.41.92-.93.92ZM43.9,29.01c.51,0,.93.41.93.93s-.42.92-.93.92-.93-.41-.93-.92.42-.93.93-.93Z" />
      <path d="M34.84,31.65c-.57,0-1.11.15-1.58.41l-4.19-3.63c.08-.27.12-.56.12-.85,0-1.81-1.47-3.29-3.28-3.29s-3.29,1.48-3.29,3.29,1.47,3.28,3.29,3.28c.55,0,1.07-.13,1.52-.37l4.23,3.66c-.07.25-.1.51-.1.78,0,1.82,1.47,3.29,3.28,3.29s3.29-1.47,3.29-3.29-1.47-3.28-3.29-3.28ZM25.91,28.51c-.51,0-.93-.42-.93-.93s.42-.93.93-.93.93.42.93.93-.42.93-.93.93ZM34.84,35.86c-.51,0-.93-.41-.93-.93s.42-.93.93-.93.93.42.93.93-.41.93-.93.93Z" />
    </svg>
  )
}

/** The full brand lockup — cube + wordmark — for the top-left header. */
export function BrandLockup({ className }: { className?: string }) {
  return (
    <div className={`flex items-center gap-2.5 ${className ?? ''}`}>
      <CubeMark className="h-7 w-7 text-mint-400 drop-shadow-[0_0_10px_rgba(67,224,182,0.45)]" />
      <div className="leading-none">
        <div className="text-[15px] font-semibold tracking-tight text-ink-100">
          Kaidera OS
        </div>
        <div className="text-[10px] font-medium uppercase tracking-[0.22em] text-mint-400/80">
          AI Worker OS
        </div>
      </div>
    </div>
  )
}
