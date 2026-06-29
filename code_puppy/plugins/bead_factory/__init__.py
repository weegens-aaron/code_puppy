"""bead_factory: a beads-driven build loop with an inspector pane.

This self-contained plugin has two cooperating subsystems:

* the **build loop** — the build-continuation policy and the build-completion
  *inspector* orchestration (the ``/inspectors`` pane), and
* the **chain driver** — a ``bd ready`` queue driver that chains beads
  through the build loop one at a time.

Both subsystems live directly in this package's flat namespace
(``code_puppy.plugins.bead_factory.*``) and depend on Code Puppy core only —
bead_factory imports no other plugin.

The only user-facing surface is ``/bead-factory`` (the chain driver) plus the
``/inspectors`` pane. bead_factory drives the build loop in build mode.
``register_callbacks.py`` is the single entry point the plugin loader imports
to wire everything up.
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
__version__ = "0.2.1"

__all__ = ["__version__"]
