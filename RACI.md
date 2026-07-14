# Kaidera OS RACI

Kaidera OS is a project-agnostic harness. Roles are runtime/project data, not
hardcoded product identities.

## Operating Rules

- The owner delegates repository operation to the engineering agent acting in the
  current session; direct human pushes are not assumed.
- No direct pushes to protected branches unless explicitly requested and policy
  gates pass.
- Product fixes land in the canonical Kaidera OS source and are deployed centrally
  to downstream installations.
- Deployment-specific bugs must be classified first as either harness core,
  extension/project config, or infrastructure.
- Licensing is a soft notification until the hosted Kaidera AI licensing service is
  formally live.

## Responsibility Model

| Area | Responsible | Accountable | Consulted |
|---|---|---|---|
| Harness architecture | Lead maintainer | Owner | Implementer/reviewer |
| Core implementation | Assigned worker | Lead maintainer | Reviewer |
| Project extensions | Project owner / extension maintainer | Owner | Harness maintainer |
| Provider/runtime config | Deployment admin | Owner | Harness maintainer |
| Release/deployment | Release operator | Owner | Reviewer |
| Security/destructive operations | Owner-approved operator | Owner | Reviewer |

## Separation Policy

- Core Kaidera OS must not name customer projects, proof-of-concepts, or dogfood
  workers in defaults or runtime routing.
- The first project is created from startup wizard input.
- Later projects are created through Add Project/API.
- Worker names, roles, personas, and knowledge come from project data and Cortex,
  not source literals.
- Extensions are loaded explicitly and must not be imported by core on a clean
  install.
