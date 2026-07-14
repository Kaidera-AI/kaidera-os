import { describe, expect, it } from 'vitest'
import { isImageFile, supportsVisionAttachments } from './attachmentCapabilities'

describe('attachmentCapabilities', () => {
  it('enables image attachments only for verified pi vision models', () => {
    expect(supportsVisionAttachments('pi', 'gpt-5.4')).toBe(true)
    expect(supportsVisionAttachments('pi', 'gpt-5.3-codex')).toBe(true)
    expect(supportsVisionAttachments('pi', 'gpt-5.3-codex-spark')).toBe(false)
    expect(supportsVisionAttachments('claude-code', 'opus')).toBe(false)
    expect(supportsVisionAttachments('pi', 'not-a-pi-model')).toBe(false)
  })

  it('detects image files by MIME type or common extension', () => {
    expect(isImageFile(new File(['x'], 'shot.bin', { type: 'image/png' }))).toBe(true)
    expect(isImageFile(new File(['x'], 'shot.webp', { type: '' }))).toBe(true)
    expect(isImageFile(new File(['x'], 'notes.txt', { type: 'text/plain' }))).toBe(false)
  })
})
