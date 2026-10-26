"""The decision layer: pure Python, no Home Assistant, no I/O.

Kept in its own subpackage so it can be imported and tested without Home
Assistant installed. The integration's own ``__init__.py`` imports Home
Assistant at module level, as every integration does; putting the core one
level down means ``core.planner`` can be imported with nothing but the
standard library, which is what keeps the test suite fast and the dependency
boundary enforced rather than merely intended.
"""
