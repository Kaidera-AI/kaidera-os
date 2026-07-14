# Cortex integration

Cortex is a mandatory part of the Kaidera OS redist and remains named Cortex on
every command, service, schema, and operator surface.

## Runtime contract

- The Cortex API is the supported read and write boundary.
- PostgreSQL and pgvector provide durable storage and retrieval indexes.
- Core writes survive optional embedding, graph, PDF, audio, or vision worker
  outages.
- Every identity, handoff, memory row, and retrieval query is project-scoped.
- Generated project identity and workspace state are created after installation
  and are never shipped from the development machine.

## Distribution contract

The public package includes the Cortex API, schema, migrations, CLI commands, and
core workers required for a useful local runtime. Optional multimodal workers can
be enabled through the documented runtime profile.

Release fitness verifies the Cortex name, required package paths, API-only command
surface, generated-state exclusions, and absence of customer payloads.

## Rebranding rule

Kaidera OS branding can change independently, but Cortex does not. A future
product rename must update product surfaces without replacing the Cortex component
name or removing it from redistributable packages.
