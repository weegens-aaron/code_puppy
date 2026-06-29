"""bead_factory: a beads-driven goal loop with an inspector pane.

This package fuses two formerly separate Code Puppy subsystems into one
self-contained plugin:

* the **goal loop** — the goal-continuation policy and the goal-completion
  *inspector* orchestration (the ``/inspectors`` pane, formerly "judges"), and
* the **bead-chain driver** — a ``bd ready`` queue driver that chains beads
  through the goal loop one at a time.

Both subsystems now live directly in this package's flat namespace
(``code_puppy.plugins.bead_factory.*``) — the bead-chain code is no longer a
separate plugin. The unrelated standalone ``wiggum`` plugin happens to remain
installed as independent legacy code, but bead_factory neither imports it nor
shares its command/config keys.

The only user-facing surface is ``/bead-factory`` (the chain driver) plus the
``/inspectors`` pane. The standalone loop commands and the old wiggum-alone
loop mode have been retired; bead_factory drives the goal loop in goal-only
mode. ``register_callbacks.py`` is the single entry point the plugin loader
imports to wire everything up.
"""

# Single source of truth for the plugin version.
#
# Everything that needs a version number MUST derive it from here -- there are
# deliberately no hardcoded duplicates anywhere else (release zip name, git
# release tag, runtime introspection via ``bead_factory.__version__``).
#
# A shell build script can read it without importing Python by grepping for the
# assignment below and slicing out the quoted value. The format below (a plain,
# single-line string literal that is the ONLY such assignment in this file) is
# part of the contract -- keep it greppable and keep it unique.
#
# Derived from the merged sources: wiggum (unversioned) and bead-chain 0.2.1.
__version__ = "0.2.1"

__all__ = ["__version__"]
