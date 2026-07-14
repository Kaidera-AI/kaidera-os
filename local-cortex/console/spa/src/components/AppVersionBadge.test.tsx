import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AppVersionBadge } from './AppVersionBadge'

describe('AppVersionBadge', () => {
  it('renders the build version as a quiet bottom-right corner badge', () => {
    render(<AppVersionBadge version="0.1.63" />)

    const badge = screen.getByTestId('app-version-badge')
    expect(badge).toHaveTextContent('v0.1.63')
    expect(badge).toHaveAttribute('aria-label', 'Console version v0.1.63')
    // Pinned BOTTOM-RIGHT (the operator's requested spot): the live console/feed sits
    // bottom-left, so the right corner reads cleanly and never overlaps the stream.
    expect(badge.className).toContain('fixed')
    expect(badge.className).toContain('bottom-2')
    expect(badge.className).toContain('right-3')
    expect(badge.className).not.toContain('left-3')
    // It carries a clear "Version" label alongside the stamp.
    expect(badge).toHaveTextContent(/version/i)
  })

  it('shows a stable placeholder while the version read is loading', () => {
    render(<AppVersionBadge version={null} />)
    expect(screen.getByTestId('app-version-badge')).toHaveTextContent('v...')
  })

  it('announces when a newer signed release is available', () => {
    render(
      <AppVersionBadge
        version="0.1.63"
        updateStatus={{
          current_version: '0.1.63',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: true,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
        }}
      />,
    )

    const badge = screen.getByTestId('app-version-badge')
    expect(badge).toHaveTextContent('Update v0.1.64')
    expect(badge).toHaveAttribute(
      'aria-label',
      'Update available: v0.1.64. Run ./update.sh',
    )
  })

  it('starts the update apply callback from the update chip', async () => {
    const user = userEvent.setup()
    const onApplyUpdate = vi.fn().mockResolvedValue(undefined)
    render(
      <AppVersionBadge
        version="0.1.63"
        updateStatus={{
          current_version: '0.1.63',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: true,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
        }}
        onApplyUpdate={onApplyUpdate}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Apply' }))

    expect(onApplyUpdate).toHaveBeenCalledTimes(1)
  })

  it('opens release details with impact and rollback guidance', async () => {
    const user = userEvent.setup()
    render(
      <AppVersionBadge
        version="0.1.63"
        updateStatus={{
          current_version: '0.1.63',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: true,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
          release_name: 'Release v0.1.64',
          release_notes: 'Fixes update drift.',
          impact: ['Rebuilds Cortex services'],
          backup_guidance: ['Take a VM snapshot for major upgrades'],
          rollback_guidance: ['Run KAIDERA_RELEASE=v0.1.63 ./update.sh'],
          post_update_checks: ['console /console/version responds'],
          release_url: 'https://example.test/release',
        }}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Details' }))

    expect(screen.getByText('Release v0.1.64')).toBeInTheDocument()
    expect(screen.getByText('Fixes update drift.')).toBeInTheDocument()
    expect(screen.getByText('Rebuilds Cortex services')).toBeInTheDocument()
    expect(screen.getByText('Run KAIDERA_RELEASE=v0.1.63 ./update.sh')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open release' })).toHaveAttribute(
      'href',
      'https://example.test/release',
    )
  })

  it('shows the running state while an update job is active', () => {
    render(
      <AppVersionBadge
        version="0.1.63"
        updateStatus={{
          current_version: '0.1.63',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: true,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
        }}
        updateJob={{ status: 'running', job_id: 'job_1' }}
        onApplyUpdate={vi.fn()}
      />,
    )

    expect(screen.getByTestId('app-version-badge')).toHaveTextContent('Updating')
    expect(screen.getByRole('button', { name: 'Running' })).toBeDisabled()
  })

  it('shows post-update health checks after a completed job', async () => {
    const user = userEvent.setup()
    render(
      <AppVersionBadge
        version="0.1.64"
        updateStatus={{
          current_version: '0.1.64',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: false,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
        }}
        updateJob={{
          status: 'succeeded',
          return_code: 0,
          log_path: '/tmp/update.log',
          health_checks: [
            { name: 'Console version', status: 'ok', detail: 'HTTP 200' },
            { name: 'Cortex admin status', status: 'failed', detail: 'mismatch' },
          ],
        }}
      />,
    )

    expect(screen.getByTestId('app-version-badge')).toHaveTextContent('Needs check')
    await user.click(screen.getByRole('button', { name: 'Details' }))
    expect(screen.getByText('Post-update health')).toBeInTheDocument()
    expect(screen.getByText('Console version')).toBeInTheDocument()
    expect(screen.getByText('Cortex admin status')).toBeInTheDocument()
    expect(screen.getByText('mismatch')).toBeInTheDocument()
  })

  it('shows admin-required instead of an apply button for non-admin users', () => {
    render(
      <AppVersionBadge
        version="0.1.63"
        canManageUpdates={false}
        updateStatus={{
          current_version: '0.1.63',
          latest_version: '0.1.64',
          latest_tag: 'v0.1.64',
          update_available: true,
          check_ok: true,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
          admin_required: true,
        }}
        onApplyUpdate={vi.fn()}
      />,
    )

    const badge = screen.getByTestId('app-version-badge')
    expect(badge).toHaveTextContent('Admin required')
    expect(screen.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument()
    expect(badge).toHaveAttribute('aria-label', 'Update available: v0.1.64. Admin required to apply.')
  })

  it('degrades quietly when the update check cannot run', () => {
    render(
      <AppVersionBadge
        version="0.1.63"
        updateStatus={{
          current_version: '0.1.63',
          latest_version: null,
          latest_tag: null,
          update_available: null,
          check_ok: false,
          source: 'github-release',
          repo: 'Kaidera-AI/homebrew-kaidera',
          update_command: './update.sh',
          error: 'GitHub CLI not installed',
        }}
      />,
    )

    const badge = screen.getByTestId('app-version-badge')
    expect(badge).toHaveTextContent('Check unavailable')
    expect(badge).toHaveAttribute(
      'aria-label',
      'Update check unavailable: GitHub CLI not installed',
    )
  })
})
