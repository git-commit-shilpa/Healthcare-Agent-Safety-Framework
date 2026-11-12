"""Technician portal backend.

Importing any submodule loads ``app.main`` first so the routers are mounted
exactly once and without a circular-import ordering problem.
"""

import app.main  # noqa: F401  (ensures main is initialised before submodules)
