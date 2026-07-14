# Maintainer guide: community collaboration and pull requests

This guide explains how to bring collaborators into Kaidera OS while protecting
`main`, users, release credentials, and the public-source boundary.

## 1. Start with outside contributions

Most contributors do not need repository access. They fork the repository, create
a branch, and open a pull request. This is the safest default because they cannot
push to Kaidera branches or access organization secrets.

Make entry points visible:

- label small verified tasks as `good first issue`
- add acceptance criteria before inviting implementation
- answer scope questions in the issue so every contributor sees the decision
- link `CONTRIBUTING.md` from issues and review comments
- recognize accepted contributions in release notes

## 2. Grant collaborator access gradually

Invite a proven contributor only when they need ongoing triage or maintenance
access. Start with the least privilege that works:

- **Triage:** issue and pull-request management without code pushes
- **Write:** trusted maintainers who need repository branches
- **Maintain:** project settings without destructive administration
- **Admin:** repository owners only; keep this group very small

Prefer organization teams over individual grants. Review team membership, outside
collaborators, deploy keys, GitHub Apps, and webhooks regularly. Remove access when
the role ends.

## 3. Protect `main`

Create a GitHub ruleset for the default branch with these minimum controls:

1. Require changes through pull requests.
2. Require at least one approving review.
3. Require CODEOWNERS review for sensitive paths.
4. Dismiss approvals when reviewed code changes materially.
5. Require all review conversations to be resolved.
6. Require stable repository validation checks.
7. Block force pushes and branch deletion.
8. Limit bypass to a very small owner group and emergency use.

Do not make a newly added check mandatory until it has a stable name and has
passed on representative pull requests.

## 4. Triage issues before code is written

For each issue:

1. Confirm the problem belongs in the Kaidera OS community source.
2. Ask for version, installation method, environment, reproduction, and logs.
3. Remove credentials, customer data, and private paths from public discussion.
4. Classify it as a bug, documentation task, proposal, support request, or security
   report.
5. Define acceptance criteria and affected compatibility surfaces.
6. Route distribution-only changes to `Kaidera-AI/homebrew-kaidera`.

Useful labels include `bug`, `documentation`, `cortex`, `orchestration`, `harness`,
`provider`, `console`, `macos`, `linux`, `security`, `needs-reproduction`, and
`good-first-issue`.

## 5. Review a pull request locally

Start from a clean checkout:

```sh
gh pr list
gh pr view 42 --web
gh pr checkout 42
git fetch origin
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

Review in this order:

1. **Intent:** Does the change solve the stated problem and stay in scope?
2. **Security:** Can it expose secrets, weaken project isolation, or execute
   untrusted input with elevated authority?
3. **Correctness:** Do state transitions, failure modes, migrations, and recovery
   behavior work?
4. **Compatibility:** Does it preserve supported macOS/Linux and harness/Manifold
   contracts?
5. **Tests:** Is evidence proportional to risk and blast radius?
6. **Privacy:** Does the diff contain customer payloads, local state, or private
   paths?
7. **Licensing:** Are new code and dependencies compatible with `AGPL-3.0-only`?
8. **Documentation:** Are changed commands and user expectations documented?
9. **Maintainability:** Is the change smaller and clearer than its alternatives?

Run the checks from [CONTRIBUTING.md](../CONTRIBUTING.md). Test installers in a
temporary destination so contributor code cannot overwrite a working installation.

## 6. Treat fork code as untrusted

- Never paste a contributor's code into a shell without reviewing it first.
- Fork pull requests must not receive signing, npm, cloud, or production secrets.
- Do not use `pull_request_target` to check out and execute fork code.
- Inspect changes to workflows, installers, package scripts, migrations, and
  download URLs before running automated commands.
- Treat build artifacts as untrusted until checksums and provenance are verified.
- Never approve a workflow change and expose secrets to it in the same step.

## 7. Give actionable feedback

Separate blocking findings from optional suggestions. A blocking comment should
name the risk, point to the affected line, and state the acceptance condition.

```sh
gh pr review 42 \
  --request-changes \
  --body "The fallback crosses project scope. Keep it scoped and add a test."
```

Approve only the commit you reviewed and recheck after new commits arrive:

```sh
gh pr review 42 \
  --approve \
  --body "Project isolation and recovery tests pass on the reviewed commit."
```

## 8. Merge deliberately

Use squash merge for most community pull requests. It keeps `main` readable and
lets the final commit message explain the user-visible change.

```sh
gh pr merge 42 --squash --delete-branch
```

Use rebase merge only when every commit is independently useful and clean. Avoid
merge commits for small changes. Never rewrite or force-push `main`; use a revert
pull request when a merged change must be undone.

After merging:

1. Confirm required checks on `main` are green.
2. Close or update the originating issue.
3. Credit the contributor and explain follow-up work.
4. Add user-visible changes to the next release notes.
5. Record any required synchronization into release engineering.

## 9. Keep public source and releases aligned

The public `kaidera-os` repository is the community contribution surface. Release
engineering may use separate protected workspaces, but accepted community changes
must not disappear into an untracked private fork.

For every binary or package release:

1. Identify the exact reviewed public-source commit.
2. Run the full source, package-boundary, and distribution test suites.
3. Tag the corresponding source commit.
4. Publish equivalent access to corresponding source next to object artifacts.
5. Include the AGPL license and preserve third-party notices.
6. Verify release URLs and SHA-256 values anonymously.

Do not publish a package whose source cannot be reproduced or located. Keep
signing and registry credentials outside contributor-controlled workflows.

## 10. Promote trusted contributors

Do not grant write access based on one pull request. Look for a pattern of scoped
changes, responsive review follow-up, sound security judgement, license awareness,
and respectful collaboration.

A practical progression is contributor, repeat contributor, triager, component
owner, then maintainer. Promotion grants responsibility, not just convenience;
document the expected area and review duties when access changes.

## 11. Recommended repository settings

- Enable Issues and private vulnerability reporting.
- Enable automatic deletion of merged branches.
- Prefer squash merging and disable unused merge methods once agreed.
- Enable Discussions when support and ideas begin obscuring actionable issues.
- Enable secret scanning and dependency alerts where GitHub provides them.
- Keep release environments protected by owner approval.
- Review access and security settings on a regular schedule.

The operating principle is simple: make contribution easy, make review explicit,
and keep release authority narrow.
