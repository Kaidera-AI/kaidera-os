# Kaidera OS design overview

Kaidera OS is a project-agnostic local control plane for AI worker teams. It owns
project registration, worker orchestration, harness execution, provider
configuration, operator controls, and durable run state.

## Product boundaries

- Project and customer content stays outside the product source and release
  payload.
- Harness adapters isolate runtime-specific behavior behind a common worker
  contract.
- Provider integrations discover models and reasoning capabilities dynamically
  where supported.
- Consequential actions remain subject to explicit policy and review gates.

## Cortex identity

Cortex is the canonical, permanent name of the memory and coordination component.
Product or company renaming must not prefix, replace, or otherwise rebrand Cortex.

Cortex owns project-scoped decisions, handoffs, evidence, messages, work products,
artifacts, retrieval indexes, and coordination state. Kaidera OS consumes Cortex
through its API boundary rather than reading its database directly.

## Edition boundary

Git source checkouts remain unbaked and expose the development provider catalogue
for integration testing. Public release packaging performs a test-covered edition
transformation, writes the release marker, and restricts redistributed provider
configuration to Manifold.

Licensing is a separate axis. A missing or invalid signed grant falls back to the
community floor; it does not change the baked provider edition.
