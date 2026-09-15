"""Sample Python fixture for repo-map tests."""


def greet(name):
    """Return a greeting."""
    return f"Hello, {name}!"


class Calculator:
    """Simple calculator."""

    def add(self, a, b):
        return a + b

    def multiply(self, a, b):
        return a * b


def farewell():
    """Return a goodbye."""
    return "Goodbye!"
