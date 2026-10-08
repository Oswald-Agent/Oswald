"""Core VCS package.

Responsibility:
    GitProvider Protocol and implementations; the only place allowed to run git.

Forbidden imports:
    Must not import from `oswald.api`, `oswald.workers`, or `oswald.db`.
"""
