**(Please remove this line only before submitting your PR. Ensure that all relevant items are checked before submission.)**

## Describe your changes

Briefly describe the changes you made and their purpose.

## Write your issue number after "Fixes "

Fixes #123

## Please ensure all items are checked off before requesting a review. "Checked off" means you need to add an "x" character between brackets so they turn into checkmarks.

- [ ] (Do not skip this or your PR will be closed) I tested and verified the application locally.
- [ ] (Do not skip this or your PR will be closed) I have performed a self-review and testing of my code.
- [ ] I have included the issue # in the PR.
- [ ] I have **not** included any files that are not related to my pull request, including `uv.lock` and `pyproject.toml` if dependencies have not changed.
- [ ] I didn't use any hardcoded values or secrets (all configurations are referenced from environment variables or settings).
- [ ] My PR is granular and targeted to one specific feature or bug fix.
- [ ] I ran `make format` (`uv run ruff format`), which automatically formats the code.
- [ ] I ran `make check` (lint, format check, typecheck, imports, and unit tests) and all checks passed.
- [ ] I ran integration tests (`make test-integration`) if my changes interact with backing services (PostgreSQL, Redis).
- [ ] I attached sample terminal output, logs, or traces to this PR if relevant.
