"""Inline banner rendering for the bead_factory goal/loop subsystem.

Relocated from the former ``wiggum`` plugin's ``register_callbacks.py`` banner
helpers. The verifier vocabulary is renamed to "inspectors": the banner uses
the color key ``"bf_inspector"`` with the label ``"INSPECTOR"``, deliberately
chosen so it does not collide with wiggum's original banner key/label.
"""

from __future__ import annotations


def display_banner_message(
    label: str,
    message: str,
    *,
    banner_name: str,
    details: str | None = None,
    final: bool = False,
) -> None:
    """Display an inline banner followed by a message."""
    import time

    from rich.console import Console
    from rich.text import Text

    from code_puppy.config import get_banner_color
    from code_puppy.messaging.spinner import pause_all_spinners, resume_all_spinners

    console = Console()
    pause_all_spinners()
    time.sleep(0.1)

    console.print(" " * 50, end="\r")
    console.print()
    color = get_banner_color(banner_name)
    banner = Text.from_markup(
        f"[bold white on {color}] {label} [/bold white on {color}] "
    )
    console.print(banner, end="")
    # markup=False so brackets in the message (e.g. "[joe-brown]") aren't
    # eaten by Rich's markup parser. Same for `details`.
    console.print(message, markup=False, highlight=False)

    if details:
        console.print(details, markup=False, highlight=False)
    if final:
        console.print()

    resume_all_spinners()


def display_inspector(
    message: str, details: str | None = None, *, final: bool = False
) -> None:
    """Display goal-inspector output with an inline banner."""
    display_banner_message(
        "INSPECTOR",
        message,
        banner_name="bf_inspector",
        details=details,
        final=final,
    )
