"""AI News Curator Agents.

Three agents - gatherer, analyst, writer - plus delivery, wired together by an
event bus rather than by calling each other. See ainews/bus.py for the bus,
ainews/events.py for the vocabulary they speak, and ainews/cli.py for the
composition root that registers everyone and publishes the first event.
"""

__version__ = "1.0.0"
