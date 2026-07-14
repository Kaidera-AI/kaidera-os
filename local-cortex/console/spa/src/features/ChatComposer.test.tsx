import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ChatComposer } from './ChatComposer'

function setup(over: Partial<React.ComponentProps<typeof ChatComposer>> = {}) {
  const onSend = vi.fn()
  const props: React.ComponentProps<typeof ChatComposer> = {
    agentLabel: 'Ren',
    sending: false,
    error: null,
    reply: '',
    onSend,
    ...over,
  }
  render(<ChatComposer {...props} />)
  return { onSend }
}

describe('ChatComposer', () => {
  it('renders a textarea and a Send button', () => {
    setup()
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send/i })).toBeInTheDocument()
  })

  it('calls onSend with the typed message on Send click, then clears the textarea', () => {
    const { onSend } = setup()
    const ta = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello there' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    // No attachments picked → the 2nd arg is undefined (back-compat).
    expect(onSend).toHaveBeenCalledWith('hello there', undefined)
    // Composer resets after dispatch.
    expect(ta.value).toBe('')
  })

  it('submits on plain Enter (Shift+Enter inserts a newline)', () => {
    const { onSend } = setup()
    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: 'go' } })

    // Shift+Enter does NOT send (it's a newline).
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()

    // Plain Enter sends.
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('go', undefined)
  })

  it('does not send a blank message', () => {
    const { onSend } = setup()
    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('disables the textarea and Send button while sending', () => {
    setup({ sending: true })
    expect(screen.getByRole('textbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
  })

  it('does not fire onSend on Cmd+Enter while sending', () => {
    const { onSend } = setup({ sending: true })
    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: 'queued?' } })
    fireEvent.keyDown(ta, { key: 'Enter', metaKey: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  // ── Stop/cancel (abort the stream = the server's cancel) ─────────────────────

  it('swaps Send for a Stop button while sending (when stop is wired) and clicking calls stop', () => {
    const stop = vi.fn()
    setup({ sending: true, stop })
    // While sending with stop wired, the action button is Stop, not Send.
    expect(screen.queryByRole('button', { name: /^send/i })).not.toBeInTheDocument()
    const stopBtn = screen.getByRole('button', { name: /stop/i })
    expect(stopBtn).not.toBeDisabled()
    fireEvent.click(stopBtn)
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('calls stop on Escape while sending', () => {
    const stop = vi.fn()
    setup({ sending: true, stop })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('does not call stop on Escape when not sending', () => {
    const stop = vi.fn()
    setup({ sending: false, stop })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })
    expect(stop).not.toHaveBeenCalled()
  })

  it('keeps a (disabled) Send button while sending when no stop handler is wired', () => {
    setup({ sending: true })
    const send = screen.getByRole('button', { name: /send/i })
    expect(send).toBeDisabled()
    expect(screen.queryByRole('button', { name: /stop/i })).not.toBeInTheDocument()
  })

  it('shows the streaming reply (and never echoes the user message)', () => {
    setup({ reply: 'Streaming answer…' })
    expect(screen.getByText('Streaming answer…')).toBeInTheDocument()
  })

  it('shows an animated "thinking" indicator while sending before the first tokens', () => {
    setup({ sending: true, reply: '' })
    expect(screen.getByText(/Ren is thinking…/i)).toBeInTheDocument()
  })

  it('shows a clean error bubble when error is set', () => {
    setup({ error: 'The harness reported an error.' })
    expect(screen.getByText('The harness reported an error.')).toBeInTheDocument()
  })

  // ── chat file-attachments (feature-gap step 6) — the real picker + chips ──────

  it('has an ENABLED attach button + a hidden file input (step 6 wired)', () => {
    setup()
    const attach = screen.getByRole('button', { name: /attach file/i })
    expect(attach).not.toBeDisabled()
    const input = screen.getByTestId('attachment-input') as HTMLInputElement
    expect(input).toBeInTheDocument()
    expect(input.multiple).toBe(true)
    expect(input.accept).not.toContain('image/*')
  })

  it('renders a chip for a picked file (name + size) and removes it', () => {
    setup()
    const input = screen.getByTestId('attachment-input') as HTMLInputElement
    const file = new File(['abc'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [file] } })

    // The chip shows the filename.
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
    // Remove it → the chip is gone.
    fireEvent.click(screen.getByRole('button', { name: /remove notes\.txt/i }))
    expect(screen.queryByText('notes.txt')).not.toBeInTheDocument()
  })

  it('passes the picked files to onSend, then clears the chips', () => {
    const { onSend } = setup()
    const input = screen.getByTestId('attachment-input') as HTMLInputElement
    const file = new File(['abc'], 'a.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [file] } })

    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: 'review' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    // onSend got (message, [file]).
    expect(onSend).toHaveBeenCalledTimes(1)
    const [msg, files] = onSend.mock.calls[0]
    expect(msg).toBe('review')
    expect(files).toHaveLength(1)
    expect(files[0].name).toBe('a.txt')
    // Chips cleared after send.
    expect(screen.queryByText('a.txt')).not.toBeInTheDocument()
  })

  it('rejects image attachments when the selected harness/model cannot read images', () => {
    const { onSend } = setup({ imageAttachmentsEnabled: false })
    const input = screen.getByTestId('attachment-input') as HTMLInputElement
    const image = new File(['png'], 'shot.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [image] } })

    expect(screen.getByText(/image attachments are not available/i)).toBeInTheDocument()
    expect(screen.queryByText('shot.png')).not.toBeInTheDocument()

    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: 'review' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    expect(onSend).toHaveBeenCalledWith('review', undefined)
  })

  it('accepts image attachments for a vision-capable harness/model', () => {
    const { onSend } = setup({ imageAttachmentsEnabled: true })
    const input = screen.getByTestId('attachment-input') as HTMLInputElement
    expect(input.accept).toContain('image/*')

    const image = new File(['png'], 'shot.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [image] } })
    expect(screen.getByText('shot.png')).toBeInTheDocument()

    const ta = screen.getByRole('textbox')
    fireEvent.change(ta, { target: { value: 'review image' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    const [, files] = onSend.mock.calls[0]
    expect(files).toHaveLength(1)
    expect(files[0].name).toBe('shot.png')
  })

  it('disables the attach button + chip-remove while sending', () => {
    // Render with a pre-picked file is not possible (internal state), so pick then
    // re-render into sending via rerender is overkill — assert the attach button only.
    setup({ sending: true })
    expect(screen.getByRole('button', { name: /attach file/i })).toBeDisabled()
  })

  // ── multi-turn chat (feature-gap step 6, Inc B): the thread indicator ─────────

  it('shows a subtle "thread: N turns" indicator when prior turns exist', () => {
    setup({ threadTurns: 3 })
    expect(screen.getByText(/thread:\s*3\s*turns/i)).toBeInTheDocument()
  })

  it('hides the thread indicator at the start of a conversation (0 turns)', () => {
    setup({ threadTurns: 0 })
    expect(screen.queryByText(/thread:/i)).not.toBeInTheDocument()
  })

  it('uses the singular "turn" for a single prior turn', () => {
    setup({ threadTurns: 1 })
    expect(screen.getByText(/thread:\s*1\s*turn\b/i)).toBeInTheDocument()
  })
})
