"""Single source of truth for plugin tier-collision precedence (E2.2).

Code Puppy discovers plugins from three tiers and loads them in this order::

    builtin  ->  user  ->  project

When the **same plugin name** appears in more than one tier we have a
*collision*. Exactly ONE copy may load and register callbacks; the rest are
FULLY SUPPRESSED — they are never imported, so they never register. This is
the E2.1 rule ("an owned copy fully suppresses the same-named builtin"),
generalized here to every collision pair so there is one — and only one —
place that decides who wins.

Precedence order (lowest to highest)::

    builtin  <  user  <  project

The highest-precedence tier that owns a name is the sole registrant. An
*ejected* plugin is a builtin that has been copied out (externalized) into the
user or project tier; from the loader's point of view it is simply an *owned*
copy, so the very same rule applies.

Collision matrix — every name-clash pair
-----------------------------------------

==========================  ========  =====================================
Collision pair              Winner    Loser (fully suppressed)
==========================  ========  =====================================
builtin vs user             user      builtin
builtin vs ejected/project  project   builtin
user vs project             project   user
builtin vs user vs project  project   builtin **and** user
==========================  ========  =====================================

The first three rows are the three pairwise collisions the bead enumerates;
the fourth is the transitive three-way case (it falls straight out of the
same precedence order, no special-casing required).

Why this lives in one module
----------------------------
Previously the skip-set arithmetic was scattered through
``load_plugin_callbacks`` as inline set unions. Centralizing it here means:

* the policy is documented and testable in isolation (unit tests target
  :func:`resolve_tier_skips` / :func:`resolve_tiers` directly), and
* the loader merely *applies* the resolution — it never re-derives precedence,
  so the two can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TIER_PRECEDENCE", "TierResolution", "resolve_tier_skips", "resolve_tiers"]


# Precedence order, lowest to highest. The LAST tier to claim a name wins the
# collision. Keep this tuple as the literal encoding of the policy — every
# helper below derives its behavior from this ordering rather than hardcoding
# tier names, so the order is changeable in exactly one spot.
TIER_PRECEDENCE: tuple[str, ...] = ("builtin", "user", "project")


@dataclass(frozen=True)
class TierResolution:
    """The fully resolved load/skip decision for all three tiers.

    Attributes:
        builtin_load / user_load / project_load:
            The names that WILL load in each tier after precedence is applied.
            For any name that collides, it appears in exactly one ``*_load``
            set — the winning tier's.
        builtin_skip / user_skip:
            The names each lower-precedence tier must NOT load because a
            higher-precedence tier owns them. (The project tier never skips —
            it is the top of the precedence order, so it has no ``project_skip``.)
    """

    builtin_load: frozenset[str]
    user_load: frozenset[str]
    project_load: frozenset[str]
    builtin_skip: frozenset[str]
    user_skip: frozenset[str]


def resolve_tier_skips(
    user_names: set[str],
    project_names: set[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Compute the skip sets the loader hands to the lower-precedence tiers.

    This is the minimal interface the loader needs: given the *owned* tier
    names (user + project), return ``(builtin_skip, user_skip)``.

    * ``builtin_skip`` — any name owned by the user OR project tier suppresses
      the same-named builtin (E2.1: an owned copy wins). This covers both the
      *builtin vs user* and the *builtin vs ejected/project* collisions.
    * ``user_skip`` — any name the project tier owns supersedes the user copy
      (*user vs project*: project wins).

    Sets are returned ``frozenset`` so callers cannot mutate the policy result.
    """
    user = set(user_names)
    project = set(project_names)

    # An owned copy from EITHER owned tier suppresses the builtin.
    builtin_skip = frozenset(user | project)
    # Project supersedes user on a name collision.
    user_skip = frozenset(project)
    return builtin_skip, user_skip


def resolve_tiers(
    builtin_names: set[str],
    user_names: set[str],
    project_names: set[str],
) -> TierResolution:
    """Resolve every collision and report exactly which names load per tier.

    Unlike :func:`resolve_tier_skips` (which the loader uses and which does not
    need the builtin name set), this is the full picture used by the policy
    unit tests: pass the discovered names for all three tiers and get back the
    deterministic winner for every collision.

    The invariant the tests assert: the three ``*_load`` sets are pairwise
    disjoint (a colliding name lands in exactly one winning tier) and their
    union equals the union of all input names (nothing is lost).
    """
    builtin = set(builtin_names)
    user = set(user_names)
    project = set(project_names)

    builtin_skip, user_skip = resolve_tier_skips(user, project)

    builtin_load = frozenset(builtin - builtin_skip)
    user_load = frozenset(user - user_skip)
    project_load = frozenset(project)

    return TierResolution(
        builtin_load=builtin_load,
        user_load=user_load,
        project_load=project_load,
        builtin_skip=frozenset(builtin & builtin_skip),
        user_skip=frozenset(user & user_skip),
    )
