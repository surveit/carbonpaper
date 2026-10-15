"""Run a project's working copy."""
import sys

from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.services.workspace import configure_projects_dir_from_env

refuse_renamed_env_vars()
configure_projects_dir_from_env()
configure_default_stores()

from app.services.run import execute  # noqa: E402

result = execute(sys.argv[1])
print("RUN", result.get("run_id"), result.get("status"))
