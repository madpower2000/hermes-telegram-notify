"""Hermes native plugin entry point for telegram-notify."""

# Hermes loads standalone plugin roots through an importlib file spec, while
# pytest may import this root ``__init__.py`` as a top-level test package.
# Support both loading modes without requiring the hyphenated plugin name to
# be importable as a normal Python identifier.
try:
    from .hermes_telegram_notify.plugin import register
except ImportError:
    from hermes_telegram_notify.plugin import register

__all__ = ["register"]
