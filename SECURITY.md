# Security

Report vulnerabilities privately through a
[security advisory on this repository](https://github.com/Nitjsefnie-Actions/pr-gate/security/advisories/new).
Include the action commit SHA, calling workflow, PR body and base template,
and the observed behavior needed to reproduce the problem. Use a repository
you control and keep sensitive details out of public issues.

The action and this repository's workflows are in scope, especially unauthorized
PR comments, closes, or reopens. The action runs with privileged PR-write
permissions; the PR body is untrusted data and must never become executable
shell or Python source.

Policy comes from the consumer template at the PR snapshot's immutable base
commit SHA, never a moving branch or PR-head template. Executable code comes
only from the pinned action's `github.action_path`; the action must not check
out PR-head code or execute consumer scripts.

Automatic reopening is limited to a close the gate owns. State and closer
revalidation must preserve maintainer closures, and interrupted close/reopen
operations must retain retryable ownership without granting ownership of
someone else's close.
