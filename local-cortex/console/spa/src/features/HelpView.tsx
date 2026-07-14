import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { GlassPanel } from '../components/glass'
import { cx } from '../components/ui'
import {
  HELP_GUIDES,
  HELP_LINKS,
  HELP_TOPICS,
  guideMatches,
  loadHelpGuideBody,
  type HelpGuide,
} from './HelpContent'

function TopicTabs({
  topic,
  onSelect,
}: {
  topic: string
  onSelect: (topic: string) => void
}) {
  return (
    <div
      role="tablist"
      aria-label="Help topic"
      className="flex flex-wrap gap-1 rounded-xl border border-glass-line bg-base-900/30 p-1"
    >
      {HELP_TOPICS.map((t) => {
        const active = t.id === topic
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(t.id)}
            className={cx(
              'rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
              active
                ? 'bg-mint-500/15 text-mint-200 ring-1 ring-mint-400/35'
                : 'text-ink-400 hover:bg-base-800/50 hover:text-ink-200',
            )}
          >
            {t.label}
          </button>
        )
      })}
    </div>
  )
}

function HelpReader({
  body,
  guide,
  loading,
}: {
  body: string | undefined
  guide: HelpGuide
  loading: boolean
}) {
  const readableBody = body ? stripLeadingTitle(guide, body) : ''
  return (
    <article className="min-w-0 rounded-xl border border-glass-line bg-base-950/35">
      <header className="border-b border-glass-line px-5 py-4">
        <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-mint-300">
          {guide.topic}
        </p>
        <h3 className="mt-1 text-2xl font-semibold text-ink-100">{guide.title}</h3>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-ink-400">{guide.summary}</p>
      </header>
      <div className="plan-doc max-w-none px-5 py-4">
        {loading ? (
          <p className="text-sm text-ink-500">Loading guide...</p>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{readableBody}</ReactMarkdown>
        )}
      </div>
    </article>
  )
}

function stripLeadingTitle(guide: HelpGuide, body: string): string {
  const title = `# ${guide.title}`
  if (!body.startsWith(title)) return body
  return body.slice(title.length).replace(/^\s+/, '')
}

export function HelpView() {
  const firstTopic = HELP_TOPICS[0]?.id ?? 'getting-started'
  const firstGuide = HELP_GUIDES[0]?.id ?? ''
  const [topic, setTopic] = useState(firstTopic)
  const [selectedId, setSelectedId] = useState(firstGuide)
  const [query, setQuery] = useState('')
  const [bodyByGuideId, setBodyByGuideId] = useState<Record<string, string>>({})

  const activeTopic = HELP_TOPICS.find((t) => t.id === topic) ?? HELP_TOPICS[0]
  const topicGuides = useMemo(
    () => HELP_GUIDES.filter((guide) => guide.topic === topic),
    [topic],
  )
  const filteredGuides = useMemo(
    () =>
      topicGuides.filter((guide) =>
        guideMatches(guide, query, bodyByGuideId[guide.id] ?? ''),
      ),
    [bodyByGuideId, query, topicGuides],
  )
  const selectedGuide =
    filteredGuides.find((guide) => guide.id === selectedId) ??
    filteredGuides[0] ??
    HELP_GUIDES.find((guide) => guide.id === selectedId) ??
    HELP_GUIDES[0]
  const selectedBody = selectedGuide ? bodyByGuideId[selectedGuide.id] : undefined
  const selectedLoading = Boolean(selectedGuide && selectedBody === undefined)

  useEffect(() => {
    const missingGuides = topicGuides.filter((guide) => bodyByGuideId[guide.id] === undefined)
    if (missingGuides.length === 0) return

    let cancelled = false
    Promise.all(
      missingGuides.map(async (guide) => {
        try {
          return [guide.id, await loadHelpGuideBody(guide)] as const
        } catch (error) {
          return [
            guide.id,
            `# ${guide.title}\n\nUnable to load this help guide. ${String(error)}`,
          ] as const
        }
      }),
    ).then((entries) => {
      if (cancelled) return
      setBodyByGuideId((current) => {
        const next = { ...current }
        for (const [guideId, body] of entries) next[guideId] = body
        return next
      })
    })

    return () => {
      cancelled = true
    }
  }, [bodyByGuideId, topicGuides])

  function selectTopic(nextTopic: string) {
    setTopic(nextTopic)
    const first = HELP_GUIDES.find((guide) => guide.topic === nextTopic)
    if (first) setSelectedId(first.id)
  }

  return (
    <GlassPanel className="flex min-h-0 w-full flex-col overflow-hidden">
      <header className="border-b border-glass-line px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-mint-300">
              Operator help
            </p>
            <h2 className="mt-1 text-lg font-semibold text-ink-100">Help</h2>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-ink-400">
              Compact offline Kaidera OS starter guides for setup, first project, and settings.
            </p>
            {HELP_LINKS.length > 0 && (
              <nav
                aria-label="On the web"
                className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1"
              >
                <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">
                  On the web
                </span>
                {HELP_LINKS.map((link) => (
                  <a
                    key={link.href}
                    href={link.href}
                    target="_blank"
                    rel="noreferrer noopener"
                    title={link.description}
                    className="text-xs font-medium text-mint-300 underline-offset-2 hover:text-mint-200 hover:underline"
                  >
                    {link.label} ↗
                  </a>
                ))}
              </nav>
            )}
          </div>
          <TopicTabs topic={topic} onSelect={selectTopic} />
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <div className="mb-4 grid gap-3 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <section className="rounded-xl border border-glass-line bg-base-900/35 p-4">
            <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-ink-500">
              {activeTopic?.eyebrow ?? 'guide'}
            </p>
            <h3 className="mt-1 text-base font-semibold text-ink-100">
              {activeTopic?.label ?? 'Help'}
            </h3>
            <label className="mt-4 block">
              <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-ink-500">
                Search this topic
              </span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="install, repo root, settings..."
                aria-label="Search help guides"
                className="mt-1 w-full rounded-lg border border-glass-line bg-base-950/70 px-3 py-2 text-sm text-ink-100 outline-none transition focus:border-mint-400/60"
              />
            </label>
            <div className="mt-4 space-y-2" aria-label="Help guides">
              {filteredGuides.length === 0 ? (
                <p className="text-xs leading-5 text-ink-500">
                  No guide in this topic matches "{query.trim()}". Try a broader keyword or
                  switch topics.
                </p>
              ) : (
                filteredGuides.map((guide) => {
                  const active = selectedGuide?.id === guide.id
                  return (
                    <button
                      key={guide.id}
                      type="button"
                      onClick={() => setSelectedId(guide.id)}
                      className={cx(
                        'w-full rounded-lg border px-3 py-2 text-left transition',
                        active
                          ? 'border-mint-400/45 bg-mint-500/10'
                          : 'border-glass-line bg-base-950/35 hover:border-glass-line-strong',
                      )}
                    >
                      <span className="block text-sm font-semibold text-ink-100">
                        {guide.title}
                      </span>
                      <span className="mt-1 block text-xs leading-5 text-ink-500">
                        {guide.summary}
                      </span>
                    </button>
                  )
                })
              )}
            </div>
          </section>

          {selectedGuide ? (
            <HelpReader body={selectedBody} guide={selectedGuide} loading={selectedLoading} />
          ) : (
            <section className="rounded-xl border border-glass-line bg-base-950/35 p-5">
              <p className="text-sm text-ink-500">No help guide is bundled.</p>
            </section>
          )}
        </div>
      </div>
    </GlassPanel>
  )
}
