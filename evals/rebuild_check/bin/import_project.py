"""Import a workflow file as a new project."""
import sys
from pathlib import Path

from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.services.workspace import configure_projects_dir_from_env

refuse_renamed_env_vars()
configure_projects_dir_from_env()
configure_default_stores()

from app.services.project import WorkflowFile, import_project  # noqa: E402

workflow = WorkflowFile.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("PROJECT_ID", import_project(workflow, name=sys.argv[2]))
