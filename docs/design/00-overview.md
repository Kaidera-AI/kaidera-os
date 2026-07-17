# Kaidera OS Design Overview

Kaidera OS separates four responsibilities:

1. The console owns operator workflows and project configuration.
2. Orchestration owns dispatch, autonomy, leases, and run state.
3. External CLI harnesses own AI model execution and authentication.
4. Cortex owns durable project memory, graph data, events, and coordination.

The public source supports Claude Code, Codex, and PI. Model and effort metadata is
discovered dynamically from each installed CLI. A discovery or authentication
failure disables that harness without taking down the console.

## Source Boundary

Community source is immutable at build and runtime. It does not contain a runtime
edition switch, commercial licensing, Manifold execution, built-in provider-key
configuration, or native commercial installer sources. Structural fitness checks
reject those paths and API markers before release.

The commercial edition is maintained in the private engineering repository. It is
not produced by mutating the community tree at install time.

## Cortex Naming

Cortex is the canonical, permanent name for this shared component. Product or company
renames must not rename Cortex. Cortex remains included in every Kaidera OS package.

## Project Isolation

Every worker run resolves a selected project, canonical workspace, agent identity,
and Cortex project scope before execution. Cross-project actions require an explicit
governed relay; local source paths and state are never inferred from another project.
