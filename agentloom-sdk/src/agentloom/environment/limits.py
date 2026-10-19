"""Byte limits shared by local file I/O and its exposed tool schema."""

from ..filesystem.posix import MAX_FILE_BYTES as MAX_FILE_BYTES

MAX_FILE_READ_BYTES = 64 * 1024
DEFAULT_FILE_READ_BYTES = 16 * 1024
