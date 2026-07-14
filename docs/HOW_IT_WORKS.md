# How Kaidera OS works

Kaidera OS is a local control plane for AI worker teams. It connects project
workspaces, model providers, harnesses, and Cortex so work can be planned,
executed, reviewed, and resumed without losing project context.

## System overview

```mermaid
flowchart LR
    Human[Operator and reviewers]
    Console[Kaidera OS console]
    Orchestrator[Worker orchestration]
    Harnesses[AI harness adapters]
    Providers[Configured model providers]
    Cortex[Cortex API]
    Memory[(Postgres and pgvector)]
    Workspace[Project workspaces]

    Human --> Console
    Console --> Orchestrator
    Orchestrator --> Harnesses
    Harnesses --> Providers
    Orchestrator <--> Cortex
    Cortex <--> Memory
    Harnesses <--> Workspace
    Console <--> Workspace
```

## Main components

### Kaidera OS console

The console is the operator surface. It registers projects and workers, shows
handoffs and run state, configures supported providers, and exposes controls for
starting, stopping, reviewing, and recovering work.

### Worker orchestration

The orchestration layer watches project handoffs and schedules eligible workers.
It maintains run state, heartbeats, approval gates, and failure recovery. A worker
is not just a chat tab: it has a project identity, role, scope, and auditable work
item.

When project autonomy is enabled, the orchestrator keeps the project management
heartbeat active, runs eligible scheduled work, and dispatches pending handoffs
to available workers. Runtime policies still control approvals, sandboxes,
retries, budgets, and consequential actions.

### Harness adapters

Harness adapters translate a common worker contract into each supported coding or
agent runtime. They discover current models, capability metadata, and available
reasoning-effort levels dynamically when a provider or harness exposes them.
Static compatibility data is a fallback, not the primary
catalogue.

Harness-specific process details remain inside the adapter. The orchestrator sees
consistent identity, workspace, prompt, run-state, cancellation, and evidence
contracts.

### Provider layer

Provider configuration supplies endpoints and local credentials to harnesses.
The packaged public edition uses the OpenAI-compatible Manifold edge. Manifold
keys are obtained through the Kaidera platform entitlement flow; usage metering
and settlement happen on the server side rather than through client
self-reporting.

Unbaked Git checkouts use the development edition so maintainers can test the full
provider integration catalogue. Release packaging explicitly bakes the public
edition and restricts redistributed builds to Manifold. The generated edition
marker belongs in release artifacts, not source control.

A missing or revoked provider credential disables the affected worker cleanly. It
must not crash the Console or expose another provider's credential.

### Cortex

Cortex is the permanent name of Kaidera's project memory and coordination layer.
It stores decisions, handoffs, evidence, work products, messages, artifacts, and
retrieval indexes. Project boundaries are enforced so one project's context does
not silently become another project's instructions.

Cortex is shared infrastructure inside Kaidera OS and is included in the public
runtime. Its name is independent from product or company rebrands.

### Project workspaces

Customer and project files live outside the Kaidera OS product payload. A fresh
installation contains no baked project or worker team. The startup flow registers
the first workspace and creates local runtime configuration for that project.

## A typical work cycle

1. An operator registers a project workspace and worker team.
2. A lead worker turns an objective into scoped handoffs and acceptance criteria.
3. The orchestrator assigns eligible work to specialist workers.
4. Harness adapters run those workers against the selected model provider.
5. Workers read and update the project workspace within their configured sandbox.
6. Cortex records run state, decisions, evidence, and completed work products.
7. Human review gates approve material changes before merge, release, or delivery.
8. Later workers resume from Cortex context instead of rediscovering completed work.

## Licensing and the community floor

Kaidera OS remains usable without a signed feature grant. The application enforces
a nonzero community floor and treats absent, expired, revoked, or unreachable
license state as that floor rather than as unlimited access.

Signed grants can raise capacity or enable named advanced features. The effective
value for each capacity axis is the greater of the community floor and the signed
grant. Advanced features are off unless explicitly granted. Platform services such
as Manifold also enforce their own server-side entitlements.

The source license and the runtime feature-grant system solve different problems:
the AGPL grants rights to study, modify, and share the community source, while a
platform grant controls access to separately operated services and capacity.

## Installation channels

Public installation channels resolve to a versioned release payload:

- **macOS Console DMG:** the full runtime in a signed and notarized disk image.
- **macOS Operator DMG:** the optional native controller for an existing install.
- **Homebrew:** the versioned runtime plus the `kaidera-os` CLI.
- **npm:** a small launcher that downloads and verifies the matching runtime.
- **curl:** the release archive and checksum followed by the canonical installer.

Release archives and DMGs are versioned. Installers verify SHA-256 before using
a downloaded runtime. npm publications use GitHub OIDC trusted publishing rather
than a long-lived registry token.

## Security boundaries

- Secrets belong in local environment or credential stores, never in Git.
- Cortex commands use the API boundary rather than direct database access.
- Project identity, memory, and handoff routing are project-scoped.
- Public release files are checksummed; macOS images are signed and notarized.
- Untrusted pull-request code must never receive release, signing, registry, or
  production credentials.
- Customer payloads and generated runtime state are excluded from public source.

## Community and enterprise

Kaidera OS provides the open-source local worker and Cortex runtime. The Kaidera
AI enterprise service adds managed identity, governed workspaces, model routing,
organization-level controls, and implementation support.

- [Kaidera OS source](https://github.com/Kaidera-AI/kaidera-os)
- [Public distributions](https://github.com/Kaidera-AI/homebrew-kaidera)
- [Install on macOS](https://kaidera.ai/downloads/kaidera-os/macos)
- [Enterprise service](https://kaidera.ai/for-enterprise)
- [Documentation](https://docs.kaidera.ai)
