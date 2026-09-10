from __future__ import annotations

from opencode_manager.models import JobRecord, mint_job_id
from opencode_manager.review.findings import split_findings
from opencode_manager.review.format import format_success


def test_split_strips_opencoderman_findings_fence() -> None:
    text = """### Summary
C++ change. 1 Critical.

### Critical

#### 1. `src/buf.cpp:6` — overflow

**Code**
```cpp
strcpy(dest, src);
```

```opencoderman-findings
{
  "findings": [
    {
      "path": "src/buf.cpp",
      "start_line": 6,
      "end_line": 6,
      "side": "new",
      "severity": "critical",
      "title": "stack buffer overflow",
      "body": "Unbounded strcpy."
    }
  ]
}
```
"""
    markdown, findings = split_findings(text)
    assert "opencoderman-findings" not in markdown
    assert "Unbounded strcpy" not in markdown
    assert "### Summary" in markdown
    assert "```cpp" in markdown
    assert len(findings) == 1
    assert findings[0].path == "src/buf.cpp"
    assert findings[0].start_line == 6
    assert findings[0].end_line == 6
    assert findings[0].severity == "critical"
    assert findings[0].title == "stack buffer overflow"


def test_split_accepts_trailing_json_findings_object() -> None:
    text = """### Summary
ok

```json
{"findings": [{"path": "a.py", "start_line": 3, "title": "x", "body": "y"}]}
```
"""
    markdown, findings = split_findings(text)
    assert "findings" not in markdown
    assert len(findings) == 1
    assert findings[0].path == "a.py"
    assert findings[0].start_line == 3


def test_split_does_not_eat_unrelated_json_fence() -> None:
    text = """### Summary
example payload:

```json
{"name": "login", "ok": true}
```
"""
    markdown, findings = split_findings(text)
    assert findings == []
    assert '{"name": "login"' in markdown


def test_split_drops_broken_findings_block() -> None:
    text = """### Summary
ok

```opencoderman-findings
not-json
```
"""
    markdown, findings = split_findings(text)
    assert findings == []
    assert "opencoderman-findings" not in markdown
    assert "not-json" not in markdown
    assert "### Summary" in markdown


def test_success_note_hides_findings_json() -> None:
    job = JobRecord(
        job_id=mint_job_id(),
        mr_key="1-2",
        project_id=1,
        mr_iid=2,
        trigger="open",
        model="opencode/x",
        text="""### Summary
1 Critical.

```opencoderman-findings
{"findings":[{"path":"x.cpp","start_line":30,"end_line":40,"title":"leak","body":"free it"}]}
```
""",
    )
    body = format_success(job)
    assert "### Summary" in body
    assert "opencoderman-findings" not in body
    assert '"path"' not in body
    assert "1 Critical." in body


def test_markdown_titles_become_findings_when_json_missing() -> None:
    text = """### Summary
2 Critical.

### Critical

#### 1. `src/buf.cpp:5-6` — stack buffer overflow

**Code**
```cpp
strcpy(dest, src);
```

**Why it is an issue and where**
`src/buf.cpp:6` — unbounded strcpy.

**Suggested fix**
Use std::string.

#### 2. `src/dangle.cpp:7` — dangling view

**Why it is an issue and where**
Returns a view into a local.

**Suggested fix**
Return std::string.
"""
    markdown, findings = split_findings(text)
    assert "### Summary" in markdown
    assert len(findings) == 2
    assert findings[0].path == "src/buf.cpp"
    assert findings[0].start_line == 5
    assert findings[0].end_line == 6
    assert findings[0].severity == "critical"
    assert findings[0].title == "stack buffer overflow"
    assert "unbounded strcpy" in findings[0].body
    assert findings[1].path == "src/dangle.cpp"
    assert findings[1].start_line == 7
    assert findings[1].end_line == 7


def test_turkish_group_and_field_labels_parse() -> None:
    text = """### Özet
1 Kritik.

### Kritik

#### 1. `src/upload.py:18` — dest üzerinde path traversal

**Kod**
```python
dest = os.path.join(OUT, filename)
```

**Sorun**
`filename` istemci adıdır.

**Öneri**
basename alın.
"""
    _markdown, findings = split_findings(text)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].path == "src/upload.py"
    assert "istemci" in findings[0].body
    assert "basename" in findings[0].body


def test_markdown_title_uses_first_line_range() -> None:
    text = """### Critical

#### 3. `src/poly.cpp:3-6,17-18` — no virtual destructor

**Why it is an issue and where**
delete through Base*.

#### 4. `src/leak.cpp:3-4,8` — raw new[] never freed

**Why it is an issue and where**
main never delete[].
"""
    _markdown, findings = split_findings(text)
    assert [item.path for item in findings] == ["src/poly.cpp", "src/leak.cpp"]
    assert (findings[0].start_line, findings[0].end_line) == (3, 6)
    assert (findings[1].start_line, findings[1].end_line) == (3, 4)
