# Contributing to Kaidera OS

Kaidera OS is built in public under the GNU AGPL v3. Bug reports, focused fixes,
tests, documentation, accessibility improvements, and portable runtime work are
welcome.

## Choose an issue

Search existing issues and pull requests before starting. For a material behavior
change, open an issue first so maintainers and contributors can agree on scope and
acceptance criteria. Small documentation and test fixes can go directly to a pull
request.

Useful contribution areas include:

- Cortex API reliability, retrieval, project isolation, and observability
- worker orchestration, handoff execution, heartbeats, and recovery
- harness compatibility and dynamic model/capability discovery
- Console usability, accessibility, and responsive behavior
- Linux and macOS installation portability
- tests, release fitness gates, security hardening, and documentation

Issues marked `good first issue` should be self-contained and include a verified
reproduction or acceptance check.

## Repository boundaries

- **Kaidera OS** is the local worker control plane.
- **Cortex** is the permanent name of the memory and coordination component.
- Customer projects, generated workers, credentials, runtime databases, chat
  histories, and private paths do not belong in source.
- The public edition and the separately operated Kaidera AI enterprise service
  have distinct deployment and licensing boundaries.
- Source checkouts remain unbaked for provider integration testing. Public release
  packaging owns the tested edition-bake step and generated edition marker.
- Distribution-channel changes belong in
  [`Kaidera-AI/homebrew-kaidera`](https://github.com/Kaidera-AI/homebrew-kaidera).

## Development setup

Fork the repository, then create a focused branch:

```sh
git clone https://github.com/YOUR-USER/kaidera-os.git
cd kaidera-os
git remote add upstream https://github.com/Kaidera-AI/kaidera-os.git
git switch -c fix/short-description
```

The complete development environment uses Python 3.12, Node.js 22, Docker, and
ShellCheck. The Cortex API and Console pin different FastAPI versions, so keep
their Python dependencies in separate virtual environments. Do not commit virtual
environments or generated runtime files.

```sh
python3.12 -m venv .venv/api
.venv/api/bin/python -m pip install -r .agents/api/requirements-dev.txt

python3.12 -m venv local-cortex/console/.venv
local-cortex/console/.venv/bin/python -m pip install \
  -r local-cortex/console/requirements-dev.txt

cd local-cortex/console/spa && npm ci
```

## Validation

Run tests proportional to the change. Once the two virtual environments above
exist, the full quality command discovers them automatically:

```sh
make qa
```

For a narrow change, run the relevant subset plus these baseline checks:

```sh
git diff --check
bash scripts/fitness/check-oss-package-hygiene.sh
python3 redistributable/scripts/validate-cortex-project-config.py \
  redistributable/examples/blank.project.json
```

Backend tests live under `.agents/api/tests`, `.agents/tests`,
`local-cortex/console/tests`, and `scripts/fitness/tests`. SPA tests are run with:

```sh
cd local-cortex/console/spa
npm test
npm run typecheck
npm run lint
npm run build
```

Add regression coverage when behavior changes. A pull request that fixes a bug
should normally demonstrate the failure before the fix and the passing behavior
after it.

## Pull-request expectations

A useful pull request explains:

- the user-visible problem and intended behavior
- why the proposed change is the smallest correct fix
- the components and compatibility surfaces affected
- the exact validation commands and results
- security, privacy, migration, and release implications

Keep commits reviewable and do not combine unrelated cleanup. Keep your branch
current with `main`, resolve review conversations, and re-run checks after rebasing.
Avoid force-pushing after review has begun unless you are removing a secret or
repairing another serious history problem.

## Licensing contributions

Kaidera OS is licensed under `AGPL-3.0-only`. By submitting a contribution, you
confirm that you have the right to submit it and agree that it is licensed under
the same terms. No separate contributor license agreement is currently required.

Do not copy code, media, model output, or documentation into the project unless
its license is compatible and its attribution requirements are preserved. Call
out every new dependency and license in the pull request.

## Security issues

Do not open a public issue containing an exploit, credential, customer data, or
sensitive log. Follow [SECURITY.md](SECURITY.md) and use GitHub private
vulnerability reporting.

## Review

Maintainers review for correctness, project boundaries, security, compatibility,
test evidence, maintainability, and license compatibility. They may ask for a
smaller scope or decline a proposal that does not fit the public runtime.

The detailed maintainer workflow is in
[`docs/MAINTAINER_GUIDE.md`](docs/MAINTAINER_GUIDE.md).
