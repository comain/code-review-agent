from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from jinja2 import Environment, PackageLoader, select_autoescape

from cr_agent.config import Settings
from cr_agent.models import AnalysisResult, TaskRecord


class ReportWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env = Environment(
            loader=PackageLoader("cr_agent", "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.env.globals["gitlab_blob_url"] = self.gitlab_blob_url

    def write(self, record: TaskRecord, result: AnalysisResult) -> Tuple[str, str]:
        target_dir = self.settings.report_dir / record.task_id
        target_dir.mkdir(parents=True, exist_ok=True)

        comments_path = target_dir / "comments.json"
        result_path = target_dir / "result.json"
        report_path = target_dir / "index.html"

        comments_path.write_text(
            json.dumps([item.model_dump(mode="json") for item in result.findings], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

        template = self.env.get_template("report.html.j2")
        report_path.write_text(
            template.render(record=record, result=result),
            encoding="utf-8",
        )

        report_url = f"{self.settings.report_base_url.rstrip('/')}/{record.task_id}/index.html"
        return report_url, str(report_path.resolve())

    @staticmethod
    def gitlab_blob_url(repo_url: str, commit_id: Optional[str], file_path: str, line: Optional[int] = None) -> Optional[str]:
        if not repo_url or not commit_id or not file_path:
            return None

        match = re.match(r"git@(?P<host>[^:]+):(?P<path>.+?)(?:\.git)?$", repo_url)
        if not match:
            https_match = re.match(r"https?://(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?$", repo_url)
            if not https_match:
                return None
            host = https_match.group("host")
            repo_path = https_match.group("path")
        else:
            host = match.group("host")
            repo_path = match.group("path")

        anchor = f"#L{line}" if line else ""
        return f"https://{host}/{repo_path}/blob/{commit_id}/{file_path}{anchor}"
