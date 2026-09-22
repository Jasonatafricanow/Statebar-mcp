"""Backward-compatible import wrapper for state transition rules.

New code should import `state_rules`.
"""

from .state_rules import *  # noqa: F401,F403
