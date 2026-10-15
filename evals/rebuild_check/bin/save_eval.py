"""Save the committed Rebuild Check eval into a project: save_eval.py <project_id>."""
import json
import sys
from pathlib import Path

from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.services.workspace import configure_projects_dir_from_env

refuse_renamed_env_vars()
configure_projects_dir_from_env()
configure_default_stores()

from app.evals.compatibility import validate_eval_compatibility  # noqa: E402
from app.evals.store import latest_version_id  # noqa: E402
from app.models import Workflow  # noqa: E402
from app.models.records.eval_config import EvalConfig  # noqa: E402
from app.services.project import write_eval_config  # noqa: E402
from app.services.versioning import load_version_stages  # noqa: E402

project_id = sys.argv[1]
config_path = Path(__file__).resolve().parents[1] / "eval_config.json"
config = EvalConfig.model_validate(
    {**json.loads(config_path.read_text(encoding="utf-8")), "project": project_id})

version = latest_version_id(project_id)
if version is None:
    raise SystemExit(f"project '{project_id}' has no workflow version to run the eval against")
workflow = Workflow(stages=load_version_stages(project_id, version))
report = validate_eval_compatibility(config, workflow)
if not report.ok:
    for problem in report.problems:
        print(problem)
    raise SystemExit(
        f"eval '{config.eval_id}' does not fit '{project_id}' version '{version}'")

write_eval_config(project_id, config)
print("SAVED eval", config.eval_id, "on", project_id)
