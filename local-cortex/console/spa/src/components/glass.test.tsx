import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GlassModal, StatPill, StatusDot } from './glass'
import { statusKind } from './ui'

describe('statusKind', () => {
  it('maps backend status labels + raw statuses to a dot kind', () => {
    expect(statusKind('running')).toBe('running')
    expect(statusKind('queued')).toBe('queued')
    expect(statusKind('completed')).toBe('completed')
    expect(statusKind('ok')).toBe('completed') // raw status
    expect(statusKind('errored')).toBe('errored')
    expect(statusKind('error')).toBe('errored') // raw status
    expect(statusKind('')).toBe('idle')
    expect(statusKind(null)).toBe('idle')
    expect(statusKind(undefined)).toBe('idle')
  })
})

describe('StatusDot', () => {
  it('renders a title (status) and pulses only when running', () => {
    const { container } = render(<StatusDot status="running" pulse />)
    expect(screen.getByTitle('running')).toBeInTheDocument()
    // the ping layer is present for a live running dot
    expect(container.querySelector('.animate-ping')).not.toBeNull()
  })

  it('does not pulse a non-running status even with pulse set', () => {
    const { container } = render(<StatusDot status="completed" pulse />)
    expect(container.querySelector('.animate-ping')).toBeNull()
  })
})

describe('StatPill', () => {
  it('shows the value and the (uppercased) label', () => {
    render(<StatPill label="Agents" value={3} tone="mint" />)
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('Agents')).toBeInTheDocument()
  })
})

describe('GlassModal', () => {
  it('renders nothing when closed (no dialog, no children)', () => {
    render(
      <GlassModal open={false} onClose={() => {}} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('panel body')).not.toBeInTheDocument()
  })

  it('renders a labelled dialog with its children when open', () => {
    render(
      <GlassModal open onClose={() => {}} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // the title labels the dialog
    expect(screen.getByText('Configure')).toBeInTheDocument()
    expect(screen.getByText('panel body')).toBeInTheDocument()
  })

  it('closes via the × close button', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(
      <GlassModal open onClose={onClose} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    await user.click(screen.getByRole('button', { name: /close/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes when the backdrop is clicked, but NOT when the panel is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(
      <GlassModal open onClose={onClose} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    // a click inside the panel does NOT close
    await user.click(screen.getByText('panel body'))
    expect(onClose).not.toHaveBeenCalled()
    // a click on the backdrop closes
    await user.click(screen.getByTestId('modal-backdrop'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on the Escape key', () => {
    const onClose = vi.fn()
    render(
      <GlassModal open onClose={onClose} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not listen for Escape while closed', () => {
    const onClose = vi.fn()
    render(
      <GlassModal open={false} onClose={onClose} title="Configure">
        <p>panel body</p>
      </GlassModal>,
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })
})
