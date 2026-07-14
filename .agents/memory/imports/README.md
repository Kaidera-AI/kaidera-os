# External Imports

This directory is for imported artifacts from external projects or customers.

Use it when material is valuable for dogfooding or learning but is not
authoritative Kaidera AI workspace knowledge by default.

Rules:

- keep external material under `.agents/memory/imports/external/`
- label the source project clearly in the filename and document heading
- do not store imported external artifacts under `.agents/memory/knowledge/`
- ingest external material intentionally with `cortex-ingest-memories --path ...`
  or `cortex-ingest-artifact ...` when you want it searchable
- external imports are reference material, not active Kaidera AI deployment truth
