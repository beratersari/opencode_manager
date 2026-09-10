from __future__ import annotations

from opencode_manager.azure.threads import azure_thread_context, parse_azure_thread, parse_azure_threads
from opencode_manager.review.findings import Finding
from opencode_manager.review.position import CREASY_FINDING_MARK
from opencode_manager.review.threads import match_creasy_thread
from opencode_manager.workspace.diffmap import parse_unified_diff


def _finding(**kwargs) -> Finding:
    data = dict(
        path="src/buf.cpp",
        start_line=6,
        end_line=6,
        side="new",
        severity="critical",
        title="overflow",
        body="strcpy overflows",
    )
    data.update(kwargs)
    return Finding(**data)


def test_azure_thread_context_uses_right_file():
    diff = parse_unified_diff(
        """diff --git a/src/buf.cpp b/src/buf.cpp
new file mode 100644
--- /dev/null
+++ b/src/buf.cpp
@@ -0,0 +1,8 @@
+int main() {
+  char dest[8];
+  strcpy(dest, src);
+}
"""
    )
    ctx = azure_thread_context(_finding(), diff)
    assert ctx is not None
    assert ctx["filePath"] == "/src/buf.cpp"
    assert ctx["rightFileStart"]["line"] == 6
    assert ctx["rightFileEnd"]["line"] == 6


def test_parse_and_match_azure_thread():
    raw = {
        "id": 148,
        "status": "active",
        "comments": [
            {
                "id": 7,
                "content": f"{CREASY_FINDING_MARK}\n**Critical** · overflow",
                "author": {"id": "bot"},
            }
        ],
        "threadContext": {
            "filePath": "/src/buf.cpp",
            "rightFileStart": {"line": 6, "offset": 1},
            "rightFileEnd": {"line": 6, "offset": 1},
        },
    }
    thread = parse_azure_thread(raw)
    assert thread is not None
    assert thread.path == "src/buf.cpp"
    assert thread.start_line == 6
    assert thread.resolved is False
    matched = match_creasy_thread(_finding(), parse_azure_threads([raw]), set())
    assert matched is not None
    assert matched.discussion_id == "148"
    assert matched.root_comment_id == 7


def test_old_job_json_defaults_to_gitlab():
    from opencode_manager.models import JobRecord

    raw = (
        '{"job_id":"job_ab","mr_key":"1-2","project_id":1,"mr_iid":2,'
        '"trigger":"review","status":"success"}'
    )
    job = JobRecord.model_validate_json(raw)
    assert job.provider == "gitlab"
    assert job.azure_project == ""
    assert job.azure_repo == ""