"""Boot-time maintenance that needs the stores already configured."""

from __future__ import annotations

from app.services.run_recovery import restart_interrupted_runs as restart_interrupted_runs
