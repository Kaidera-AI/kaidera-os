import helpManifest from '../../../../../docs/help/manifest.json'

export interface HelpTopicSpec {
  id: string
  label: string
  eyebrow: string
}

export interface HelpGuideSpec {
  id: string
  topic: string
  title: string
  file: string
  summary: string
  keywords: string[]
}

interface HelpManifest {
  version: string
  generated_for_release: string
  description: string
  topics: HelpTopicSpec[]
  guides: HelpGuideSpec[]
}

export type HelpGuide = HelpGuideSpec

type RawMarkdownModule = {
  default: string
}

const GUIDE_LOADERS: Record<string, () => Promise<RawMarkdownModule>> = {
  'guides/first-project.md': () =>
    import('../../../../../docs/help/guides/first-project.md?raw'),
  'guides/getting-started.md': () =>
    import('../../../../../docs/help/guides/getting-started.md?raw'),
  'guides/settings.md': () => import('../../../../../docs/help/guides/settings.md?raw'),
}

const HELP_MANIFEST = helpManifest as HelpManifest
const HELP_GUIDE_OVERRIDES: Partial<Record<string, Partial<HelpGuideSpec>>> = {
  'getting-started': { title: 'Getting started with Kaidera OS' },
}

export const HELP_TOPICS = HELP_MANIFEST.topics
export const HELP_GUIDES: HelpGuide[] = HELP_MANIFEST.guides.map((guide) => ({
  ...guide,
  ...HELP_GUIDE_OVERRIDES[guide.id],
}))

export interface HelpLink {
  label: string
  href: string
  description: string
}

function externalUrl(value: unknown): string {
  const raw = typeof value === 'string' ? value.trim() : ''
  if (!raw) return ''
  try {
    const parsed = new URL(raw)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : ''
  } catch {
    return ''
  }
}

export function buildHelpLinks(env: Record<string, unknown>): HelpLink[] {
  const docs = externalUrl(env.VITE_KAIDERA_OS_DOCS_URL)
  const downloads = externalUrl(env.VITE_KAIDERA_OS_DOWNLOADS_URL)
  return [
    docs
      ? {
          label: 'Full docs',
          href: docs,
          description: 'Complete Kaidera OS guides, operations, and reference',
        }
      : null,
    downloads
      ? {
          label: 'Downloads',
          href: downloads,
          description: 'Kaidera OS downloads and installation guides',
        }
      : null,
  ].filter((link): link is HelpLink => link !== null)
}

/** Optional public links. The rebrand does not assume a replacement web domain. */
export const HELP_LINKS = buildHelpLinks(import.meta.env)

export function hasHelpGuideLoader(guide: HelpGuide): boolean {
  return Boolean(GUIDE_LOADERS[guide.file])
}

export async function loadHelpGuideBody(guide: HelpGuide): Promise<string> {
  const loader = GUIDE_LOADERS[guide.file]
  if (!loader) {
    return `# Missing help guide\n\nThe guide file \`${guide.file}\` is listed in docs/help/manifest.json but is not bundled.`
  }
  const mod = await loader()
  return mod.default
}

function searchableText(guide: HelpGuide, body = ''): string {
  return [
    guide.id,
    guide.topic,
    guide.title,
    guide.summary,
    guide.keywords.join(' '),
    body,
  ]
    .join('\n')
    .toLowerCase()
}

export function guideMatches(guide: HelpGuide, query: string, body = ''): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return searchableText(guide, body).includes(q)
}
