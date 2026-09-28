from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from critic.evaluation import EvalResult

RESULTS_FILE = "results.json"
REPORT_FILE = "report.html"
_TEMPLATES = Path(__file__).with_name("templates")


def write_eval_outputs(result: EvalResult, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / RESULTS_FILE
    results_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    report_path = out_dir / REPORT_FILE
    report_path.write_text(render_report(result), encoding="utf-8")
    return [results_path, report_path]


def render_report(result: EvalResult) -> str:
    return render_template(REPORT_FILE, result=result)


def render_template(name: str, **context: object) -> str:
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES), autoescape=True, undefined=StrictUndefined
    )
    environment.filters["percent"] = format_percent
    environment.filters["usd"] = format_usd
    return environment.get_template(name).render(**context)


def format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def format_usd(value: float) -> str:
    return f"${value:.2f}"
