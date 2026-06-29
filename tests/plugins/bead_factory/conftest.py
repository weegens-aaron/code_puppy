"""Pytest fixtures for the migrated bead_factory plugin tests.

These tests were brought over from the standalone *bead-chain* plugin and the
internal *wiggum* plugin and repointed at ``code_puppy.plugins.bead_factory``
(epic bead-factory-zfk). Unlike the old bead-chain conftest, bead_factory is a
*real* installed sub-package, so we never have to synthesize the package from a
loose ``__init__.py`` -- importing it normally is enough.

We still do three things the migrated tests rely on:

  * Put the package directory on ``sys.path`` so the dual-context ``beads`` /
    ``beads_reads`` / ``beads_writes`` modules import *flat* (``import beads``).
    Several pure-stdlib tests exercise the bare-module seam exactly as they did
    in bead-chain.
  * Snapshot + restore the monkeypatchable ``beads`` module globals (both the
    flat module and the package facade) so a stub left by an alphabetically
    earlier module can't leak into a later one (bead_chain-221).
  * Default the *package* ``beads._run_bd`` seam to raise, so any test that
    misses a stub fails loudly instead of shelling out to a real ``bd`` and
    mutating live issue state.
"""

from __future__ import annotations

import os
import sys

import pytest

import code_puppy.plugins.bead_factory as _bf

# The flat ``beads`` / ``beads_reads`` / ``beads_writes`` modules live right
# here; putting the package dir on sys.path lets ``import beads`` resolve to
# bead_factory's copy (a distinct module object from the package submodule, by
# design -- see the dual-context import guards in beads.py).
_PKG_DIR = os.path.dirname(os.path.abspath(_bf.__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)


@pytest.fixture(autouse=True)
def _restore_beads_module_globals():
    """Snapshot/restore the monkeypatchable ``beads`` globals, both copies.

    Many test modules stub ``beads._run_bd`` / ``beads._parse_json_list`` /
    ``beads.show`` by direct attribute assignment and never restore them.
    Without this guard a leftover stub from an alphabetically-earlier module
    leaks into later tests that call the real implementation -- a test that
    passes in isolation then fails in the full suite.

    We guard BOTH the flat ``beads`` module (what the pure-stdlib tests patch)
    and the package facade ``code_puppy.plugins.bead_factory.beads`` (what the
    lifecycle/chain tests reach through), since they are separate module
    objects under the dual-context import seam.
    """
    import beads  # flat module (resolved via _PKG_DIR on sys.path)

    from code_puppy.plugins.bead_factory import beads as pkg_beads

    saved = {
        "flat_run_bd": beads._run_bd,
        "flat_parse": beads._parse_json_list,
        "flat_show": beads.show,
        "pkg_run_bd": pkg_beads._run_bd,
        "pkg_parse": pkg_beads._parse_json_list,
        "pkg_show": pkg_beads.show,
    }

    def _no_real_bd(*_a, **_k):
        raise pkg_beads.BeadsError(
            "bd subprocess disabled in tests: a package-facade call reached "
            "the real _run_bd without a stub. Patch the seam your code path "
            "actually uses (e.g. lifecycle_close.check_gates, fan_out_gate."
            "show, or beads._run_bd) instead of shelling out to live bd."
        )

    # Safety net: an un-stubbed real bd call through the package facade raises
    # instead of mutating live state. The flat module is left untouched so the
    # genuine end-to-end tests (which spin up a throwaway bd db) still run.
    pkg_beads._run_bd = _no_real_bd
    try:
        yield
    finally:
        beads._run_bd = saved["flat_run_bd"]
        beads._parse_json_list = saved["flat_parse"]
        beads.show = saved["flat_show"]
        pkg_beads._run_bd = saved["pkg_run_bd"]
        pkg_beads._parse_json_list = saved["pkg_parse"]
        pkg_beads.show = saved["pkg_show"]
