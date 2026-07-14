# Kaidera OS Help Documentation — task list + delivery plan

**Owner:** kai@kaidera-os
**Audience:** Ren (packages this into the in-app Help section as a searchable guide) + end operators.
**Status:** delivered — slim starter wired into the in-app Help view; the full corpus URL is configured with `KAIDERA_OS_DOCS_URL`.

## What this is
A small offline starter corpus for the Kaidera OS console app. Each bundled guide is a
standalone Markdown file under `docs/help/guides/`. `manifest.json` is the in-app index
(topic grouping + title + path + search keywords). Deep guides now belong on
`KAIDERA_OS_DOCS_URL`; the app links there from Help instead of bundling everything.

## Task list (what is needed)
- [x] T0 — Plan + task list + manifest schema (this file + `manifest.json`)
- [x] T1 — Getting Started: install, run, first login, the console layout
- [x] T2 — Your First Project: bring a project online, repo roots, roster, default agent
- [x] T3 — Settings Deep-Dive: System / Providers / Flags / Cortex / Workspace / Extensions surfaces + how to configure each
- [ ] T4 — Move full provider/model/persona/autonomy/Cortex/portal/operations guides to the configured `KAIDERA_OS_DOCS_URL`
- [x] T5 — In-app packaging: `HelpContent.ts` imports only the three starter guide bodies; `HelpView.tsx` renders Markdown and links to hosted docs

## Plan (how to achieve + package)
1. **Author** each bundled starter guide as Markdown under `docs/help/guides/<id>.md`.
2. **Index** each bundled starter guide in `docs/help/manifest.json`:
   `{ id, topic, title, file, keywords[], summary }` so the Help view can group by
   topic and filter by keyword (the "searchable guide").
3. **Package**: `HelpContent.ts` imports `manifest.json` plus the markdown bodies as
   Vite raw assets; `HelpView.tsx` renders the selected guide with
   `react-markdown` and filters guides by title/summary/keywords/body text. No
   backend change is required.
4. **Link out**: all deep operational docs belong at `KAIDERA_OS_DOCS_URL`.

## Delivery order
T1 → T2 → T3 stay embedded. The remaining deep guides move to hosted docs.

## Files
- `docs/help/README.md` — this plan + task list
- `docs/help/manifest.json` — the packageable index
- `docs/help/guides/*.md` — the guides (one per task T1–T10)
- `docs/help/PACKAGING.md` — Ren's wiring note (T12)
