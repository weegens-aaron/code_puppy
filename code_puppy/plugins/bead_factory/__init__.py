"""bead_factory: a fused looping + beads-driven goal plugin.

This package combines two previously separate Code Puppy plugins into one:

* the **wiggum** loop subsystem — the goal-continuation policy and the
  goal-completion *inspector* orchestration (formerly "judges"), and
* the **bead-chain** subsystem — a ``bd ready`` queue driver that chains beads
  through the goal loop one at a time.

bead_factory is a deliberate *clean break*: it coexists with the original
``wiggum`` and ``bead-chain`` plugins (both stay loaded) and exposes its own,
distinct slash commands and config keys so nothing collides. Behavior is
identical to the originals — only the command/config names and the
"judges" -> "inspectors" vocabulary differ.

The wiggum and bead-chain runtime modules land directly in this package's flat
namespace (``code_puppy.plugins.bead_factory.*``); ``register_callbacks.py`` is
the single entry point the plugin loader imports to wire everything up.
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
