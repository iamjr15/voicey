"""Filesystem and secret-handling security primitives."""

from voicekit.security.files import ensure_private_directory, ensure_private_file

__all__ = ["ensure_private_directory", "ensure_private_file"]
