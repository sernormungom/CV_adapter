from pathlib import Path
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_cv(cv_content: dict, consultant_id: str) -> str:
    template = _env.get_template("cv.html.jinja2")
    generated_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return template.render(cv=cv_content, generated_date=generated_date)
