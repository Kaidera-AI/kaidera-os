const PI_VISION_ATTACHMENT_MODELS = new Set([
  'gpt-5.5',
  'gpt-5.4',
  'gpt-5.4-mini',
  'gpt-5.3-codex',
  'gpt-5.2',
])

function cleanString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function supportsVisionAttachments(harness: unknown, model: unknown): boolean {
  const h = cleanString(harness).toLowerCase()
  const m = cleanString(model)
  if (h !== 'pi' || !m) return false
  return PI_VISION_ATTACHMENT_MODELS.has(m)
}

export function isImageFile(file: File): boolean {
  if (file.type.toLowerCase().startsWith('image/')) return true
  return /\.(avif|bmp|gif|heic|jpeg|jpg|png|tiff|webp)$/i.test(file.name)
}
