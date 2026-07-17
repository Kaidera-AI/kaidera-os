/**
 * ChatComposer — the bottom composer in the agent-detail pane (the SPA twin of the
 * legacy `<form class="composer">`). It lives IN the agent-detail pane (not a new
 * page — the "stick to prototype, no invented surfaces" rule).
 *
 * It is PURE-PRESENTATIONAL over the useChatSend state threaded by AgentDetail:
 *   - an auto-grow <textarea> + a Send button + ⌘/Ctrl+↵ to send;
 *   - disabled-while-sending (textarea + Send), driven by `sending`;
 *   - the live streaming reply (`reply`) with a "thinking" pulse until the first
 *     tokens land — the DURABLE reply renders in the run transcript above (run-state
 *     SSE); this strip is the immediate optimistic turn so the operator sees the
 *     first tokens without waiting for the run-state catch-up. The operator's own
 *     message is NOT echoed here (it already appears in the continuous feed above);
 *   - a clean error bubble (`error`) on an `event: error` / transport failure.
 *
 * Attachments (feature-gap step 6): a real file picker (hidden <input type="file"
 * multiple>) + removable chips (name + size). Picked files are passed to `onSend` and
 * uploaded by the send flow (base64-in-JSON, no multipart). Disabled while sending.
 */

import { useCallback, useRef, useState } from 'react'
import { cx } from '../components/ui'
import { isImageFile } from './attachmentCapabilities'

/** Human-readable size for an attachment chip (B / KB / MB). */
function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

const TEXT_ACCEPT_TYPES = 'text/*,application/json,application/pdf'
const IMAGE_ACCEPT_TYPES = `${TEXT_ACCEPT_TYPES},image/*`

interface ChatComposerProps {
  /** The agent's display label (placeholder + reply attribution). */
  agentLabel: string
  /** True while a turn is in flight (disables the textarea + Send). */
  sending: boolean
  /** A clean error message to surface, or null. */
  error: string | null
  /** The assembled streaming reply text ('' until tokens arrive). */
  reply: string
  /**
   * Dispatch a turn. The composer trims + guards blank before calling, and passes any
   * picked attachment files (chat file-attachments, step 6) as the 2nd arg.
   */
  onSend: (message: string, files?: File[]) => void
  /**
   * Stop the in-flight turn (aborts the chat stream — the server's cancel). When wired,
   * a Stop button replaces Send while `sending`, and Escape (while sending) calls it.
   * Optional — absent ⇒ no Stop affordance (Send simply stays disabled while sending).
   */
  stop?: () => void
  /**
   * Prior turns in this conversation (multi-turn chat, feature-gap step 6). When > 0 a
   * subtle "thread: N turns" indicator shows that the backend is threading context.
   * Cosmetic; defaults to 0 (no indicator at the start of a conversation).
   */
  threadTurns?: number
  /** True when the selected harness/model can read image attachments. */
  imageAttachmentsEnabled?: boolean
}

export function ChatComposer({
  agentLabel,
  sending,
  error,
  reply,
  onSend,
  stop,
  threadTurns = 0,
  imageAttachmentsEnabled = false,
}: ChatComposerProps) {
  const [value, setValue] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const acceptTypes = imageAttachmentsEnabled ? IMAGE_ACCEPT_TYPES : TEXT_ACCEPT_TYPES
  const visibleError = attachmentError ?? error

  // Auto-grow the textarea up to a max height, then it scrolls (legacy parity).
  const grow = useCallback(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 320)}px`
  }, [])

  const submit = useCallback(() => {
    if (sending) return
    const text = value.trim()
    if (!text) return
    onSend(value, files.length > 0 ? files : undefined)
    setValue('')
    setFiles([])
    setAttachmentError(null)
    // Reset the auto-grown height after clearing.
    requestAnimationFrame(grow)
  }, [sending, value, files, onSend, grow])

  // Add picked files to the chip list (dedupe by name+size); clears the input so the
  // SAME file can be re-picked after a remove.
  const onPick = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? [])
    const accepted = picked.filter(
      (file) => imageAttachmentsEnabled || !isImageFile(file),
    )
    const rejectedImages = picked.length - accepted.length
    if (rejectedImages > 0) {
      setAttachmentError('Image attachments are not available for this harness/model.')
    } else if (picked.length > 0) {
      setAttachmentError(null)
    }
    if (accepted.length > 0) {
      setFiles((prev) => {
        const seen = new Set(prev.map((f) => `${f.name}:${f.size}`))
        const next = [...prev]
        for (const f of accepted) {
          const key = `${f.name}:${f.size}`
          if (!seen.has(key)) {
            seen.add(key)
            next.push(f)
          }
        }
        return next
      })
    }
    e.target.value = '' // allow re-picking the same file later
  }, [imageAttachmentsEnabled])

  const removeFile = useCallback((idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx))
    setAttachmentError(null)
  }, [])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter sends; Shift+Enter inserts a newline. (IME composition is left alone.)
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault()
        submit()
      } else if (e.key === 'Escape' && sending) {
        // While a turn is in flight, Escape stops it (aborts the stream — the cancel).
        e.preventDefault()
        stop?.()
      }
    },
    [submit, sending, stop],
  )

  return (
    <div className="shrink-0 border-t border-glass-line px-4 pb-4 pt-3">
      {/* The optimistic in-flight turn: the streaming reply (+ a "thinking" pulse
          until the first tokens land). The user's own message is NOT echoed here —
          it already shows in the continuous feed above; the durable reply also lives
          in the transcript above (run-state SSE). This is the instant local view so
          the operator isn't staring at a blank pane. */}
      {(reply || sending) && (
        <div className="mb-3 space-y-2">
          <div className="flex justify-start">
            <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-2xl rounded-bl-sm bg-base-800/60 px-3 py-2 text-[12.5px] text-ink-200">
              {reply || (
                <span className="inline-flex items-center gap-1.5 text-ink-500">
                  <svg
                    viewBox="0 0 24 24"
                    className="h-3.5 w-3.5 shrink-0 text-mint-300 animate-[kaidera-sparkle_1.6s_ease-in-out_infinite]"
                    fill="currentColor"
                    aria-hidden="true"
                  >
                    <path d="M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z" />
                  </svg>
                  {agentLabel} is thinking…
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* The clean error bubble (event: error / transport failure). */}
      {visibleError && (
        <div className="mb-3 flex items-start gap-2 rounded-xl border border-run-errored/25 bg-run-errored/10 px-3 py-2 text-xs text-run-errored">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="mt-0.5 h-3.5 w-3.5 shrink-0"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="9" />
            <path d="M12 16v-4M12 8h.01" />
          </svg>
          <span>{visibleError}</span>
        </div>
      )}

      {/* Attachment chips (name + size + remove) — picked files awaiting send. */}
      {files.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5" data-testid="attachment-chips">
          {files.map((f, i) => (
            <span
              key={`${f.name}:${f.size}:${i}`}
              className="inline-flex items-center gap-1.5 rounded-lg border border-glass-line bg-base-800/60 py-1 pl-2 pr-1 text-[11px] text-ink-200"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className="h-3 w-3 shrink-0 text-ink-400"
                aria-hidden="true"
              >
                <path d="M21.4 11.05 12.2 20.2a5 5 0 0 1-7.1-7.1l9.2-9.2a3.33 3.33 0 0 1 4.7 4.7l-9.2 9.2a1.67 1.67 0 0 1-2.35-2.35l8.5-8.5" />
              </svg>
              <span className="max-w-[160px] truncate" title={f.name}>
                {f.name}
              </span>
              <span className="text-ink-500">{fmtSize(f.size)}</span>
              <button
                type="button"
                disabled={sending}
                onClick={() => removeFile(i)}
                aria-label={`Remove ${f.name}`}
                className="rounded p-0.5 text-ink-500 hover:text-ink-200 disabled:opacity-40"
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  className="h-3 w-3"
                  aria-hidden="true"
                >
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            </span>
          ))}
        </div>
      )}

      {/* The composer box. */}
      <div className="glass-soft rounded-2xl p-2">
        {/* Hidden file input driven by the attach button. */}
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={acceptTypes}
          onChange={onPick}
          className="hidden"
          aria-hidden="true"
          tabIndex={-1}
          data-testid="attachment-input"
        />
        <textarea
          ref={taRef}
          rows={2}
          value={value}
          disabled={sending}
          onChange={(e) => setValue(e.target.value)}
          onInput={grow}
          onKeyDown={onKeyDown}
          placeholder={`Talk to ${agentLabel} — set a goal, ask a question, or steer the work…  (Enter to send · Shift+Enter for a newline)`}
          className={cx(
            'block max-h-80 w-full resize-none bg-transparent px-2 py-1.5 text-[13px] text-ink-100 placeholder:text-ink-500 focus:outline-none',
            sending && 'opacity-60',
          )}
          aria-label={`Message ${agentLabel}`}
        />
        <div className="mt-1 flex items-center gap-2 px-1">
          {/* Attach files (chat file-attachments, step 6) — opens the hidden picker. */}
          <button
            type="button"
            disabled={sending}
            onClick={() => fileRef.current?.click()}
            title={
              imageAttachmentsEnabled
                ? 'Attach a file or image'
                : 'Attach a file'
            }
            aria-label="Attach file"
            className={cx(
              'rounded-lg p-1.5 text-ink-400 transition-colors hover:bg-base-800/60 hover:text-ink-200',
              sending && 'cursor-not-allowed opacity-50',
            )}
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M21.4 11.05 12.2 20.2a5 5 0 0 1-7.1-7.1l9.2-9.2a3.33 3.33 0 0 1 4.7 4.7l-9.2 9.2a1.67 1.67 0 0 1-2.35-2.35l8.5-8.5" />
            </svg>
          </button>
          {/* Multi-turn thread indicator (cosmetic) — shows the backend is threading
              this conversation's prior turns into the prompt. */}
          {threadTurns > 0 && (
            <span
              className="ml-auto inline-flex items-center gap-1 rounded-full bg-base-800/60 px-2 py-0.5 text-[10px] text-ink-400 ring-1 ring-glass-line"
              title="Earlier turns in this conversation are threaded into the agent's context"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className="h-3 w-3"
                aria-hidden="true"
              >
                <path d="M7 8h10M7 12h10M7 16h6" />
              </svg>
              thread: {threadTurns} {threadTurns === 1 ? 'turn' : 'turns'}
            </span>
          )}
          <span
            className={cx(
              'text-[10px] uppercase tracking-wide text-ink-500',
              threadTurns > 0 ? 'ml-2' : 'ml-auto',
            )}
          >
            {sending ? 'sending…' : 'live'}
          </span>
          {sending && stop ? (
            // STOP — while a turn streams, swap Send for Stop. Aborting the stream IS
            // the server's cancel (no cancel endpoint; the disconnect marks the run
            // terminal). Esc does the same. Mint/glass styling matches Send.
            <button
              type="button"
              onClick={() => stop()}
              title="Stop this turn (Esc)"
              className="inline-flex items-center gap-1.5 rounded-lg bg-mint-500/15 px-3 py-1.5 text-xs font-medium text-mint-200 ring-1 ring-mint-400/40 transition-colors hover:bg-mint-500/25"
            >
              <svg
                viewBox="0 0 24 24"
                fill="currentColor"
                className="h-3 w-3"
                aria-hidden="true"
              >
                <rect x="6" y="6" width="12" height="12" rx="1.5" />
              </svg>
              Stop
              <kbd className="rounded bg-base-900/40 px-1 py-0.5 font-mono text-[9px] text-ink-400">
                Esc
              </kbd>
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={sending}
              className={cx(
                'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                sending
                  ? 'cursor-not-allowed bg-base-700/50 text-ink-500'
                  : 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/40 hover:bg-mint-500/25',
              )}
            >
              Send
              <kbd className="rounded bg-base-900/40 px-1 py-0.5 font-mono text-[9px] text-ink-400">
                ↵
              </kbd>
            </button>
          )}
        </div>
      </div>

    </div>
  )
}
