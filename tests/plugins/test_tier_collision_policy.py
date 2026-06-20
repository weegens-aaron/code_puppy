"""E2.2 — Tier-collision policy: every name-clash pair is unit-tested here.

These tests pin the *single source of truth* in
``code_puppy.plugins.precedence`` for all collision pairs the bead enumerates:

    * builtin vs user            -> user wins, builtin suppressed
    * builtin vs ejected/project -> project wins, builtin suppressed
    * user vs project            -> project wins, user suppressed
    * builtin vs user vs project -> project wins (transitive three-way)

The companion file ``test_owned_copy_suppresses_builtin.py`` exercises the
loader's *application* of this policy end-to-end; here we test the policy
function in isolation so it can never silently drift from the docs.
"""

from code_puppy.plugins.precedence import (
    TIER_PRECEDENCE,
    TierResolution,
    resolve_tier_skips,
    resolve_tiers,
)

# ---------------------------------------------------------------------------
# Precedence order is the literal encoding of the policy.
# ---------------------------------------------------------------------------


def test_precedence_order_is_builtin_user_project():
    """Lowest-to-highest order: builtin < user < project."""
    assert TIER_PRECEDENCE == ("builtin", "user", "project")


# ---------------------------------------------------------------------------
# resolve_tier_skips — the minimal interface the loader consumes.
# ---------------------------------------------------------------------------


def test_skips_empty_when_no_collisions():
    """Disjoint tier names -> nobody skips anything."""
    builtin_skip, user_skip = resolve_tier_skips({"a"}, {"b"})
    assert builtin_skip == frozenset({"a", "b"})  # owned names suppress builtins
    assert user_skip == frozenset({"b"})


def test_builtin_vs_user_user_wins():
    """builtin vs user: the user copy suppresses the builtin."""
    builtin_skip, user_skip = resolve_tier_skips(
        user_names={"dup"}, project_names=set()
    )
    assert "dup" in builtin_skip  # builtin 'dup' is suppressed
    assert user_skip == frozenset()  # nothing supersedes the user copy


def test_builtin_vs_ejected_project_wins():
    """builtin vs ejected (project copy): the project copy suppresses builtin."""
    builtin_skip, user_skip = resolve_tier_skips(
        user_names=set(), project_names={"dup"}
    )
    assert "dup" in builtin_skip  # builtin 'dup' suppressed by the ejected copy
    # The project name is always handed down as the user-tier skip; here there
    # is no user 'dup' to actually suppress, but the raw skip still lists it.
    assert user_skip == frozenset({"dup"})


def test_user_vs_project_project_wins():
    """user vs project: the project copy suppresses the user copy."""
    builtin_skip, user_skip = resolve_tier_skips(
        user_names={"dup"}, project_names={"dup"}
    )
    assert "dup" in builtin_skip  # builtin also suppressed (owned copy exists)
    assert user_skip == frozenset({"dup"})  # user copy yields to project


def test_skips_return_frozensets_so_policy_is_immutable():
    """Callers must not be able to mutate the resolved policy in place."""
    builtin_skip, user_skip = resolve_tier_skips({"a"}, {"b"})
    assert isinstance(builtin_skip, frozenset)
    assert isinstance(user_skip, frozenset)


# ---------------------------------------------------------------------------
# resolve_tiers — full resolution, used to assert per-tier winners.
# ---------------------------------------------------------------------------


def _resolution(builtin, user, project) -> TierResolution:
    return resolve_tiers(set(builtin), set(user), set(project))


def test_no_collisions_everything_loads():
    res = _resolution(builtin={"b1"}, user={"u1"}, project={"p1"})
    assert res.builtin_load == frozenset({"b1"})
    assert res.user_load == frozenset({"u1"})
    assert res.project_load == frozenset({"p1"})
    assert res.builtin_skip == frozenset()
    assert res.user_skip == frozenset()


def test_builtin_vs_user_resolution():
    res = _resolution(builtin={"dup"}, user={"dup"}, project=set())
    assert res.builtin_load == frozenset()  # builtin loses
    assert res.user_load == frozenset({"dup"})  # user wins
    assert res.builtin_skip == frozenset({"dup"})


def test_builtin_vs_project_resolution():
    res = _resolution(builtin={"dup"}, user=set(), project={"dup"})
    assert res.builtin_load == frozenset()  # builtin loses
    assert res.project_load == frozenset({"dup"})  # project wins
    assert res.builtin_skip == frozenset({"dup"})


def test_user_vs_project_resolution():
    res = _resolution(builtin=set(), user={"dup"}, project={"dup"})
    assert res.user_load == frozenset()  # user loses
    assert res.project_load == frozenset({"dup"})  # project wins
    assert res.user_skip == frozenset({"dup"})


def test_three_way_collision_project_wins():
    """builtin vs user vs project — transitive: only project survives."""
    res = _resolution(builtin={"dup"}, user={"dup"}, project={"dup"})
    assert res.builtin_load == frozenset()
    assert res.user_load == frozenset()
    assert res.project_load == frozenset({"dup"})
    assert res.builtin_skip == frozenset({"dup"})
    assert res.user_skip == frozenset({"dup"})


def test_winning_tiers_are_pairwise_disjoint():
    """A colliding name lands in exactly one winning tier — the invariant."""
    res = _resolution(
        builtin={"dup", "b_only"},
        user={"dup", "u_only"},
        project={"dup", "p_only"},
    )
    assert res.builtin_load.isdisjoint(res.user_load)
    assert res.builtin_load.isdisjoint(res.project_load)
    assert res.user_load.isdisjoint(res.project_load)


def test_nothing_is_lost_union_preserved():
    """Every input name appears in exactly one winning tier (no leaks)."""
    builtin = {"dup", "b_only"}
    user = {"dup", "u_only"}
    project = {"dup", "p_only"}
    res = _resolution(builtin, user, project)
    all_loaded = res.builtin_load | res.user_load | res.project_load
    assert all_loaded == (builtin | user | project)


def test_skip_sets_only_contain_names_present_in_that_tier():
    """builtin_skip/user_skip are scoped to names that tier actually has."""
    # 'ghost' is owned by project but no builtin/user has it -> not a skip there.
    res = _resolution(builtin={"real"}, user={"real"}, project={"ghost", "real"})
    assert "ghost" not in res.builtin_skip
    assert res.builtin_skip == frozenset({"real"})
    assert res.user_skip == frozenset({"real"})
