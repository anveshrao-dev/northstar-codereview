"""OFFLINE SCRIPTED TEST DOUBLE: exercises plumbing, not LLM review accuracy.

The fake reads actual observations to choose the next tool/arguments. Production
CodeQualityAgent never imports or falls back to this demonstration model.
"""

import asyncio
import json
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from northstar_codereview.agents.code_quality import CodeQualityAgent
from northstar_codereview.local_source import collect_local_snapshot
from northstar_codereview.models import ReviewRequest


class ObservationDrivenDemo:
    """Scripted knowledge of the fictional refund defect, explicitly not an LLM."""
    def bind_tools(self, tools):
        self.names = {tool.name for tool in tools}
        return self

    async def ainvoke(self, messages, **kwargs):
        if not isinstance(messages[-1], ToolMessage):
            return self.call("list_snapshot_files", {}, "discover")
        observation = json.loads(messages[-1].content)
        if observation["tool_name"] == "list_snapshot_files":
            path = observation["data"]["files"][0]["file_path"]
            return self.call("get_source_metadata", {"file_path": path}, "measure")
        if observation["tool_name"] == "get_source_metadata":
            metadata = observation["data"]
            return self.call("read_source_lines", {"file_path": metadata["file_path"], "start_line": 1,
                                                    "end_line": metadata["line_count"]}, "inspect")
        return AIMessage(content=json.dumps({"findings": [{
            "title": "Refund subtracts a fee despite the full-amount contract", "category": "correctness",
            "severity": "medium", "confidence": 0.9,
            "explanation": "refund(amount) is documented to return the full amount with no fee, but returns amount - 1. Every numeric input is reduced by one; zero becomes negative. The separate refund_without_fee function returns amount and is the negative control.",
            "suggested_remediation": "Return amount in refund and add tests for zero and positive amounts.",
            "assumptions": ["The fictional no-fee docstring is the intended contract; source was not executed."],
            "evidence": observation["evidence"],
        }]}))

    @staticmethod
    def call(name, arguments, identity):
        return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identity}])


async def demonstration():
    root = Path(__file__).resolve().parents[1]
    labels = json.loads((root / "tests/fixtures/quality_expected.json").read_text())
    request = ReviewRequest(mode="local", local_path=".")
    snapshot = collect_local_snapshot(request, source_root=root / "tests/fixtures/quality")
    agent = CodeQualityAgent(snapshot, model=ObservationDrivenDemo(), approved_synthetic=True)
    result = await agent.run(review_id=request.review_id)
    return {"mode": "OFFLINE SCRIPTED TEST DOUBLE; not a live model or an accuracy evaluation",
            "expected_labels_defined_before_run": labels, "run": result.model_dump(mode="json")}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(demonstration()), indent=2))
