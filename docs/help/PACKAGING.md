# Packaging the help corpus into the app

This corpus is wired into the in-app Help section as a small offline starter. No
backend change is required. Full documentation belongs at `KAIDERA_OS_DOCS_URL`.

## What you have
- `docs/help/manifest.json` — the index: `topics[]` (group labels) + `guides[]`
  (`id, topic, title, file, summary, keywords[]`).
- `docs/help/guides/getting-started.md`, `first-project.md`, and `settings.md` —
  the three bundled starter guides.
- `spa/src/features/HelpContent.ts` — imports the manifest and exposes lazy guide
  body loaders.
- `spa/src/features/HelpView.tsx` — renders markdown and provides topic-scoped
  search.

## Current implementation

- `HelpContent.ts` statically imports `docs/help/manifest.json`.
- `HelpContent.ts` maps only the three bundled starter guides to dynamic Vite raw
  imports (`?raw`) so provider/model/autonomy/docs chunks do not inflate the app.
- The Help tab renders guide markdown with `react-markdown` and `remark-gfm`.
- Search filters within the selected topic by title, summary, keywords, and loaded
  body text.
- `HelpView.test.tsx` asserts every manifest guide has a lazy loader and loadable
  markdown body.

## Adding or renaming a bundled guide

1. Add or edit `docs/help/guides/<id>.md`.
2. Add or edit the matching `docs/help/manifest.json` guide entry.
3. Add a dynamic raw import entry in `HelpContent.ts`.
4. Run `npm test -- HelpView.test.tsx`; the manifest/body coverage test should fail
   if the new guide is not bundled.
5. Run `npm run build`, `npm run lint`, `npm run typecheck`, and the relevant tests
   before shipping.

## Notes
- Keep the in-app corpus intentionally small: setup, first project, and settings only.
- Provider/model/persona/autonomy/Cortex/portal/operations documentation lives on
  `KAIDERA_OS_DOCS_URL`, linked from the Help header when configured.

## Search keywords
packaging, ren, help, search, manifest, markdown, import.meta.glob, vitest
