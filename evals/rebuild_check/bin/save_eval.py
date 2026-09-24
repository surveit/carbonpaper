"""Save the committed Rebuild Check eval into a project: save_eval.py <project_id>."""
import json
import sys
from pathlib import Path

from app.core.store_config import configure_default_stores, refuse_renamed_env_vars
from app.services.workspace import configure_projects_dir_from_env

refuse_renamed_env_vars()
configure_projects_dir_from_env()
configure_default_stores()

from app.models.records.eval_config import EvalConfig  # noqa: E402
from app.services.project import write_eval_config  # noqa: E402

project_id = sys.argv[1]
config_path = Path(__file__).resolve().parents[1] / "eval_config.json"
config = EvalConfig.model_validate(
    {**json.loads(config_path.read_text(encoding="utf-8")), "project": project_id})
write_eval_config(project_id, config)
print("SAVED eval", config.eval_id, "on", project_id)
