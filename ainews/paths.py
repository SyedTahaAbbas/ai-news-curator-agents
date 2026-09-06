#!/usr/bin/env python3
"""
Where the data lives.

The code is a package and may be installed anywhere - site-packages, a wheel,
a container layer. The data it reads and writes (sources.yaml, PREFERENCES.md,
`see news/`, the state files) belongs to the *user*, not to the installation,
so it is resolved from the working directory rather than from __file__.

That keeps every context working the same way: the repo root when you run it
locally, /app in the container, the checkout in CI. AINEWS_HOME overrides it
when you want to run from somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """The directory holding sources.yaml and the digest output."""
    return Path(os.environ.get("AINEWS_HOME") or Path.cwd())
