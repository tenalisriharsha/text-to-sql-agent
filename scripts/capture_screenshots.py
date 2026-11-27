"""Run real CLI commands and screenshot their output as terminal windows.

Every command listed below is actually executed; its captured stdout/stderr
is rendered verbatim into a terminal-style HTML page and screenshotted with
Playwright. Nothing here hand-writes or edits command output.
"""

from __future__ import annotations

import datetime
import html
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = REPO_ROOT / "docs" / "screenshots"
VENV_BIN = REPO_ROOT / ".venv" / "bin"

TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ margin: 0; background: #0d1117; }}
  .window {{
    width: 900px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 10px 40px rgba(0,0,0,0.5);
    border: 1px solid #30363d;
  }}
  .titlebar {{
    background: #161b22;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    border-bottom: 1px solid #30363d;
  }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .dot.red {{ background: #ff5f56; }}
  .dot.yellow {{ background: #ffbd2e; }}
  .dot.green {{ background: #27c93f; }}
  .titletext {{
    color: #8b949e;
    font-size: 13px;
    margin-left: 8px;
  }}
  .body {{
    background: #0d1117;
    color: #c9d1d9;
    padding: 18px 20px;
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }}
  .prompt {{ color: #58a6ff; }}
  .cmd {{ color: #e6edf3; font-weight: 600; }}
  .stderr {{ color: #ff7b72; }}
</style>
</head>
<body>
<div class="window">
  <div class="titlebar">
    <div class="dot red"></div><div class="dot yellow"></div><div class="dot green"></div>
    <div class="titletext">{title}</div>
  </div>
  <div class="body">{body}</div>
</div>
</body>
</html>
"""


@dataclass
class Step:
    command: str
    stdout: str
    stderr: str
    returncode: int


@dataclass
class Scene:
    filename: str
    title: str
    commands: list[str]


SCENES = [
    Scene(
        filename="01-ask-simple.png",
        title="text-to-sql-agent — ask a question",
        commands=['text-to-sql-agent ask "how many customers" --yes'],
    ),
    Scene(
        filename="02-ask-join.png",
        title="text-to-sql-agent — ask a question involving a join",
        commands=['text-to-sql-agent ask "order details" --yes --row-limit 5'],
    ),
    Scene(
        filename="03-validate-safety.png",
        title="text-to-sql-agent — validate: the safety contract",
        commands=[
            'text-to-sql-agent validate "SELECT 1; DROP TABLE orders"',
            'text-to-sql-agent validate "SELECT * FROM secret_table"',
            'text-to-sql-agent validate "SELECT * FROM orders"',
        ],
    ),
    Scene(
        filename="04-inspect-schema.png",
        title="text-to-sql-agent — inspect-schema",
        commands=["text-to-sql-agent inspect-schema"],
    ),
    Scene(
        filename="05-dialect-translate.png",
        title="text-to-sql-agent — ask --target-dialect",
        commands=[
            'text-to-sql-agent ask "total revenue" --target-dialect bigquery',
            'text-to-sql-agent ask "total revenue" --target-dialect athena',
        ],
    ),
    Scene(
        filename="06-audit-trace.png",
        title="text-to-sql-agent — audit trace",
        commands=[
            'text-to-sql-agent ask "how many customers" --yes --audit-log trace.jsonl',
            "grep -v '\"step_type\": \"explain\"' trace.jsonl",
        ],
    ),
    Scene(
        filename="07-retrieve.png",
        title="text-to-sql-agent — retrieve: schema retrieval for a question",
        commands=[
            'text-to-sql-agent retrieve "events per customer" --top-k-tables 2 --top-k-columns 3',
        ],
    ),
    Scene(
        filename="08-advanced-queries.png",
        title="text-to-sql-agent — window functions and time filters",
        commands=[
            'text-to-sql-agent ask "running total of revenue" --yes --row-limit 6',
            'text-to-sql-agent ask "recent orders" --yes --row-limit 5',
        ],
    ),
    Scene(
        filename="10-benchmark.png",
        title="text-to-sql-agent — benchmark: the evaluation harness",
        commands=["text-to-sql-agent benchmark"],
    ),
    Scene(
        filename="11-ambiguous-question.png",
        title="text-to-sql-agent — ask: declining to guess",
        commands=[
            'text-to-sql-agent ask "who are our top performers"',
            'text-to-sql-agent ask "what is our customer churn rate"',
        ],
    ),
]


def _cost_guard_scene() -> Scene:
    """Built at capture time: `events`' partitions are named after today's date."""
    today = datetime.date.today().isoformat()
    return Scene(
        filename="09-cost-guard.png",
        title="text-to-sql-agent — estimate-cost and the scan-cost guard",
        commands=[
            'text-to-sql-agent estimate-cost "SELECT * FROM events"',
            'text-to-sql-agent estimate-cost "SELECT event_type FROM events WHERE event_date = '
            f"'{today}'\"",
            'text-to-sql-agent ask "how many customers" --yes --max-bytes-scanned 10',
        ],
    )


def run_command(command: str, cwd: Path, env: dict[str, str]) -> Step:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        executable="/bin/bash",
    )
    return Step(command=command, stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def render_body(steps: list[Step]) -> str:
    lines = []
    for step in steps:
        lines.append(f'<span class="prompt">$</span> <span class="cmd">{html.escape(step.command)}</span>')
        if step.stdout:
            lines.append(html.escape(step.stdout.rstrip("\n")))
        if step.stderr:
            lines.append(f'<span class="stderr">{html.escape(step.stderr.rstrip(chr(10)))}</span>')
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = REPO_ROOT

    env = os.environ.copy()
    env["PATH"] = f"{VENV_BIN}:{env['PATH']}"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "100"

    # Fresh, deterministic warehouse for every capture run.
    shutil.rmtree(work_dir / "data" / "warehouse", ignore_errors=True)
    run_command("text-to-sql-agent init-warehouse", cwd=work_dir, env=env)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 960, "height": 100})

        for scene in [*SCENES, _cost_guard_scene()]:
            steps = [run_command(cmd, cwd=work_dir, env=env) for cmd in scene.commands]
            body_html = render_body(steps)
            html_doc = TEMPLATE.format(title=html.escape(scene.title), body=body_html)
            html_path = SCREENSHOT_DIR / f"_{scene.filename}.html"
            html_path.write_text(html_doc)

            page.goto(f"file://{html_path}")
            window = page.locator(".window")
            window.screenshot(path=str(SCREENSHOT_DIR / scene.filename))
            html_path.unlink()
            print(f"captured {scene.filename}")
            for step in steps:
                if step.returncode not in (0, 1):
                    print(f"  WARNING: unexpected exit code {step.returncode} for: {step.command}")

        browser.close()

    (work_dir / "trace.jsonl").unlink(missing_ok=True)
    shutil.rmtree(work_dir / "data" / "warehouse", ignore_errors=True)


if __name__ == "__main__":
    main()
