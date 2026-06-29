"""Tests for the single-source-of-truth plugin version (bead_chain-u2o).

Migrated to bead_factory. ``__version__`` lives in
``code_puppy/plugins/bead_factory/__init__.py`` and is the *only* place the
version is defined; runtime introspection and a non-Python build script's grep
both derive from it. These tests lock the contract two ways:

  1. Runtime introspection: ``bead_factory.__version__`` exposes a PEP 440-ish
     string. We assert its *shape*, never a hardcoded literal — a duplicated
     literal is a second source of truth that drifts (bead_chain-wn7).
  2. Greppability: the literal is readable by a non-Python build script via a
     simple ``grep`` over the file — the format is part of the contract.

bead_factory is a real installed sub-package, so (unlike the standalone
bead-chain version of this test) we import it normally rather than loading
``__init__.py`` by file path.
"""

from __future__ import annotations

import os
import re

import code_puppy.plugins.bead_factory as bead_factory

# ``__file__`` of the package object IS its ``__init__.py`` — the single source
# of truth the greppability test slices the literal out of.
_INIT_PATH = os.path.abspath(bead_factory.__file__)

# PEP 440-ish: a dotted release segment (e.g. "0.2.1") optionally followed by
# pre/post/dev or local-version suffixes. We intentionally do NOT hardcode the
# concrete value: __init__.py is the single source of truth, and a second
# literal here is just a duplicate that drifts (it did — see bead_chain-wn7).
_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+)*(?:[._-]?(?:a|b|rc|alpha|beta|post|dev)\d*)*(?:\+[a-zA-Z0-9.]+)?$"
)


def test_version_is_defined_and_well_formed():
    """The single source of truth defines a PEP 440-ish ``__version__``.

    We assert the *shape* of the version rather than a hardcoded literal so the
    test never drifts from ``__init__.py`` on a release bump.
    """
    assert hasattr(bead_factory, "__version__"), (
        "__version__ missing from bead_factory/__init__.py"
    )
    assert _VERSION_RE.match(bead_factory.__version__), bead_factory.__version__


def test_version_is_a_plain_string():
    """Runtime introspection gets a non-empty str (not a tuple/bytes)."""
    version = bead_factory.__version__
    assert isinstance(version, str)
    assert version.strip() == version
    assert version


def test_version_is_greppable_from_source():
    """A non-Python build script can extract the version with a simple grep.

    Mirrors the documented one-liner:
        grep -oE '__version__ = "[^"]+"' __init__.py | cut -d'"' -f2
    """
    with open(_INIT_PATH, encoding="utf-8") as fh:
        source = fh.read()
    matches = re.findall(r'__version__ = "([^"]+)"', source)
    assert matches == [bead_factory.__version__], matches
