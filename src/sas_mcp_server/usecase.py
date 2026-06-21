# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Use-case scoping for the SAS MCP server.

A "use case" restricts the assistant to a curated subset of the SAS Viya
environment — specific CAS tables, reports, models, and decisions — instead of
exposing everything. The scope is defined entirely through environment
variables, so a non-developer can configure a per-use-case chatbot (for
example, from the SAS Retrieval Agent Manager tool-server Environment Variables
tab) without touching code.

Environment variables
----------------------
``USE_CASE_NAME``         Human-readable name of the use case.
``USE_CASE_DESCRIPTION``  What the assistant is for.
``ALLOWED_TABLES``        Comma/newline-separated CAS tables. Each entry may be
                          ``table``, ``caslib.table``, or
                          ``server.caslib.table``.
``ALLOWED_REPORTS``       Comma/newline-separated report IDs or names.
``ALLOWED_MODELS``        Comma/newline-separated model IDs or names.
``ALLOWED_DECISIONS``     Comma/newline-separated decision/MAS-module IDs or names.
``SCOPE_ENFORCE``         ``true`` (default) blocks access to out-of-scope
                          resources; ``false`` only hides them from listings.

If none of the ``ALLOWED_*`` variables are set, the scope is inactive and the
server behaves exactly as before — full access to the environment.
"""

import os
from typing import Optional


def _parse_list(raw: Optional[str]) -> list:
    """Split a comma/newline-separated env value into a clean list."""
    if not raw:
        return []
    out = []
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _norm(value) -> str:
    return str(value).strip().lower()


class UseCaseScope:
    """An allowlist of the resources a scoped assistant may use."""

    def __init__(self, name="", description="", tables=None, reports=None,
                 models=None, decisions=None, enforce=True):
        self.name = name
        self.description = description
        self.tables = list(tables or [])
        self.reports = list(reports or [])
        self.models = list(models or [])
        self.decisions = list(decisions or [])
        self.enforce = enforce
        self._tables = {_norm(t) for t in self.tables}
        self._reports = {_norm(r) for r in self.reports}
        self._models = {_norm(m) for m in self.models}
        self._decisions = {_norm(d) for d in self.decisions}

    @property
    def active(self) -> bool:
        """True when at least one allowlist is defined."""
        return bool(self._tables or self._reports or self._models
                    or self._decisions)

    @property
    def enforced(self) -> bool:
        """True when out-of-scope access should be blocked (not just hidden)."""
        return self.active and self.enforce

    @staticmethod
    def _match(allowed: set, *candidates) -> bool:
        return any(c is not None and _norm(c) in allowed for c in candidates)

    # -- membership checks (an empty allowlist for a kind permits everything) --

    def allows_report(self, *candidates) -> bool:
        return not self._reports or self._match(self._reports, *candidates)

    def allows_model(self, *candidates) -> bool:
        return not self._models or self._match(self._models, *candidates)

    def allows_decision(self, *candidates) -> bool:
        return not self._decisions or self._match(self._decisions, *candidates)

    def allows_table(self, name=None, caslib=None, server=None) -> bool:
        if not self._tables:
            return True
        candidates = [name]
        if caslib and name:
            candidates.append(f"{caslib}.{name}")
        if server and caslib and name:
            candidates.append(f"{server}.{caslib}.{name}")
        return self._match(self._tables, *candidates)

    def manifest(self) -> dict:
        """A description of the scope suitable for returning to the agent."""
        return {
            "useCaseName": self.name,
            "description": self.description,
            "scoped": self.active,
            "enforced": self.enforced,
            "allowedTables": self.tables,
            "allowedReports": self.reports,
            "allowedModels": self.models,
            "allowedDecisions": self.decisions,
        }


def load_scope() -> UseCaseScope:
    """Build a :class:`UseCaseScope` from the current environment variables."""
    enforce = os.getenv("SCOPE_ENFORCE", "true").lower() not in ("false", "0", "no")
    return UseCaseScope(
        name=os.getenv("USE_CASE_NAME", ""),
        description=os.getenv("USE_CASE_DESCRIPTION", ""),
        tables=_parse_list(os.getenv("ALLOWED_TABLES", "")),
        reports=_parse_list(os.getenv("ALLOWED_REPORTS", "")),
        models=_parse_list(os.getenv("ALLOWED_MODELS", "")),
        decisions=_parse_list(os.getenv("ALLOWED_DECISIONS", "")),
        enforce=enforce,
    )
