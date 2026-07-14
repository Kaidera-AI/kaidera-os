import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HELP_GUIDES, buildHelpLinks, hasHelpGuideLoader, loadHelpGuideBody } from './HelpContent'
import { HelpView } from './HelpView'

describe('HelpView', () => {
  it('has a lazy loader for every manifest guide body', async () => {
    expect(HELP_GUIDES).toHaveLength(3)
    for (const guide of HELP_GUIDES) {
      expect(hasHelpGuideLoader(guide)).toBe(true)
      await expect(loadHelpGuideBody(guide)).resolves.toMatch(/^# /)
    }
  })

  it('renders the searchable help corpus by default', async () => {
    render(<HelpView />)

    expect(screen.getByRole('heading', { name: 'Help' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Getting Started' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(
      screen.getByRole('heading', { name: 'Getting started with Kaidera OS' }),
    ).toBeInTheDocument()
    await waitFor(() => expect(document.body).toHaveTextContent(/Console reachable on/))
    expect(document.body).toHaveTextContent(/:8765/)
  })

  it('switches across bundled starter guide topics', async () => {
    const user = userEvent.setup()
    render(<HelpView />)

    await user.click(screen.getByRole('tab', { name: 'Settings' }))
    expect(screen.getByRole('heading', { name: 'Settings deep-dive' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Providers' })).not.toBeInTheDocument()
  })

  it('searches guide metadata and body within the selected topic', async () => {
    const user = userEvent.setup()
    render(<HelpView />)

    await user.click(screen.getByRole('tab', { name: 'First Project' }))
    await user.type(screen.getByRole('textbox', { name: 'Search help guides' }), 'roster')

    expect(screen.getByRole('heading', { name: 'Bring your first project online' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText(/Seed the roster/i).length).toBeGreaterThan(0))
  })

  it('does not guess hosted URLs when none are configured', () => {
    render(<HelpView />)

    expect(screen.queryByRole('navigation', { name: 'On the web' })).not.toBeInTheDocument()
  })

  it('accepts only configured HTTP(S) help links', () => {
    expect(
      buildHelpLinks({
        VITE_KAIDERA_OS_DOCS_URL: 'https://docs.example.test/os',
        VITE_KAIDERA_OS_DOWNLOADS_URL: 'javascript:alert(1)',
      }),
    ).toEqual([
      expect.objectContaining({ label: 'Full docs', href: 'https://docs.example.test/os' }),
    ])
  })
})
