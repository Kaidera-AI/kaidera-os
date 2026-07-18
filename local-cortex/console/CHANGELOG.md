# Changelog

All notable changes to Kaidera OS community source are recorded here.

## Unreleased

- Published the clean community source under `AGPL-3.0-only`.
- Added contribution, security, architecture, and maintainer documentation.
- Changed public update and bootstrap defaults to the public distribution
  repository.
- Restored build-time Help sources and clean-checkout test fixtures.

## v0.1.233 - 2026-07-17

**Provider-free community source boundary.** The AGPL edition contains Cortex,
project coordination, local orchestration, supported external harness integration,
graph, memory, approvals, audit, and collaboration surfaces without commercial
trial/licensing code or built-in model-provider implementations. Provider secrets,
Manifold activation, BYOK adapters, and commercial native packaging are absent from
the public source rather than disabled at runtime.

The public release also adds reproducible archive construction, SHA-256 checksums,
structural source-boundary tests, and the shared Kaidera OS release workflow. Cortex
remains named Cortex and is included in the distribution.

## v0.1.231 - 2026-07-13

- Completed the Kaidera OS product naming migration while preserving Cortex as a
  permanent component name.
- Added dynamic model and reasoning-effort discovery across supported harnesses.
- Added project-scoped Cortex graph, durable run state, and autonomy controls.
- Added external harness adapters and initial package-channel launchers.
