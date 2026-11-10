"""Deterministic tool execution over a fictional fixture; no model or network."""

from pathlib import Path

from langsmith import tracing_context

from northstar_codereview.evidence_tools import build_snapshot_tools
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    snapshot = collect_local_snapshot(
        ReviewRequest(mode="local", local_path="refund.py"),
        source_root=project / "tests" / "fixtures" / "northstar",
    )
    with tracing_context(enabled=False):
        tools = {tool.name: tool for tool in build_snapshot_tools(snapshot, approved_synthetic=True)}
        print("Snapshot:", snapshot.manifest.snapshot_id)
        for tool in tools.values():
            print(tool.name, tool.args_schema.model_json_schema()["properties"])
        print(tools["list_snapshot_files"].invoke({"limit": 10}))
        print(tools["read_source_lines"].invoke({"file_path": "refund.py", "start_line": 4, "end_line": 5}))
        print(tools["get_evidence"].invoke({"file_path": "refund.py", "start_line": 5, "end_line": 5}))
        print(tools["get_source_metadata"].invoke({"file_path": "refund.py"}))


if __name__ == "__main__":
    main()
