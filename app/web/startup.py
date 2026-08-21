"""Boot-time and shutdown maintenance that needs the stores already configured."""

from __future__ import annotations

from app.services.run import end_tenures_on_shutdown as end_tenures_on_shutdown
from app.services.run_recovery import watch_for_interrupted_runs as watch_for_interrupted_runs
