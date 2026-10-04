"""``fleet-policy watch``: generic new-item detection over one MCP tool call.

The tool call is injected (``call=``) so these tests need neither Hermes nor the ``mcp`` SDK;
``test_real_streamable_http_server`` additionally drives a real MCP server when the SDK is there.
"""
import json
import os

import pytest

from fleet_policy import watch


def _config(tmp_path, **extra):
    cfg = {"server_url": "http://mcp.invalid/mcp", "tool": "LIST_ITEMS", "arguments": {"q": "x"},
           "id_field": "messageId", "show_fields": ["from", "subject"], "max_new": 20,
           "state": str(tmp_path / "state.json")}
    cfg.update(extra)
    path = tmp_path / "watch.json"
    path.write_text(json.dumps(cfg))
    return path


def _items(*ids, **fields):
    return {"successful": True, "error": None, "data": {"messages": [
        {"messageId": i, "from": fields.get("sender", f"sender-{i}"), "subject": f"subject {i}",
         "volatile": os.urandom(4).hex()} for i in ids]}}


class Fake:
    """Stand-in for the MCP call: returns queued payloads, records the calls."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, cfg):
        self.calls.append((cfg.server_url, cfg.tool, cfg.arguments))
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(path, fake, capsys, **kw):
    code = watch.run(str(path), call=fake, **kw)
    out, err = capsys.readouterr()
    return code, out, err


def last_line(out):
    return [ln for ln in out.splitlines() if ln.strip()][-1]


def test_first_run_records_baseline_without_waking(tmp_path, capsys):
    path = _config(tmp_path)
    code, out, err = run(path, Fake(_items("a", "b")), capsys)
    assert code == 0
    assert json.loads(last_line(out)) == {"wakeAgent": False}
    assert "baseline" in out
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["seen"] == ["a", "b"]


def test_nothing_new_sleeps_even_if_raw_response_changes(tmp_path, capsys):
    path = _config(tmp_path)
    fake = Fake(_items("a", "b"), _items("b", "a"), _items("a"))  # reorder, then a deletion
    for _ in range(3):
        code, out, _ = run(path, fake, capsys)
        assert code == 0 and json.loads(last_line(out)) == {"wakeAgent": False}
    assert fake.calls[0] == ("http://mcp.invalid/mcp", "LIST_ITEMS", {"q": "x"})


def test_new_items_wake_with_short_lines(tmp_path, capsys):
    path = _config(tmp_path)
    run(path, Fake(_items("a")), capsys)
    code, out, _ = run(path, Fake(_items("a", "b", "c", sender="x" * 500)), capsys)
    assert code == 0
    assert '"wakeAgent": false' not in out
    lines = out.strip().splitlines()
    assert lines[0].startswith("2 new item(s)")
    item_lines = [ln for ln in lines if ln.startswith("- ")]
    assert len(item_lines) == 2
    assert "id=b" in item_lines[0] and "subject: subject b" in item_lines[0]
    assert "volatile" not in out  # only show_fields are printed
    assert max(len(ln) for ln in item_lines) < 400  # values truncated
    assert json.loads((tmp_path / "state.json").read_text())["seen"] == ["a", "b", "c"]
    code, out, _ = run(path, Fake(_items("a", "b", "c")), capsys)
    assert json.loads(last_line(out)) == {"wakeAgent": False}


def test_max_new_caps_lines_but_marks_all_seen(tmp_path, capsys):
    path = _config(tmp_path, max_new=2)
    run(path, Fake(_items("a")), capsys)
    code, out, _ = run(path, Fake(_items("a", "b", "c", "d", "e")), capsys)
    assert out.startswith("4 new item(s)")
    assert len([ln for ln in out.splitlines() if ln.startswith("- ")]) == 2
    assert "2 more" in out
    assert set(json.loads((tmp_path / "state.json").read_text())["seen"]) == set("abcde")


def test_id_inside_nested_json_string(tmp_path, capsys):
    # Composio-style envelope: the provider's payload arrives as a JSON string in a text field.
    inner = json.dumps({"messages": [{"messageId": "m1", "subject": "hello"}]})
    payload = {"successful": True, "data": {"results": [{"response": {"successful": True,
                                                                      "data": inner}}]}}
    assert watch.extract_items(payload, "messageId")[0]["messageId"] == "m1"
    path = _config(tmp_path)
    run(path, Fake({"successful": True, "data": {"results": []}}), capsys)
    code, out, _ = run(path, Fake(payload), capsys)
    assert "id=m1" in out and "subject: hello" in out


def test_outermost_dict_is_the_item(tmp_path):
    payload = {"items": [{"id": "ev1", "creator": {"id": "person-9"}, "summary": "s"}]}
    items = watch.extract_items(payload, "id")
    assert [watch.item_id(i, "id") for i in items] == ["ev1"]


@pytest.mark.parametrize("payload", [
    {"successful": False, "error": "quota", "data": {}},
    {"successful": True, "data": {"results": [{"response": {"successful": False,
                                                            "error": "no auth"}}]}},
    {"error": "Tool not found", "data": None},
])
def test_tool_error_exits_nonzero_and_keeps_state(tmp_path, capsys, payload):
    path = _config(tmp_path)
    run(path, Fake(_items("a")), capsys)
    before = (tmp_path / "state.json").read_text()
    code, out, err = run(path, Fake(payload), capsys)
    assert code == 1 and err.strip() and "wakeAgent" not in out
    assert (tmp_path / "state.json").read_text() == before


def test_transport_error_exits_nonzero(tmp_path, capsys):
    path = _config(tmp_path)
    code, out, err = run(path, Fake(ConnectionError("refused")), capsys)
    assert code == 1 and "refused" in err and out == ""
    assert not (tmp_path / "state.json").exists()


def test_error_inside_an_item_is_data_not_failure(tmp_path, capsys):
    path = _config(tmp_path)
    payload = {"successful": True, "data": [{"messageId": "a", "error": "bounced", "subject": "s"}]}
    code, out, _ = run(path, Fake(payload), capsys)
    assert code == 0


def test_dry_run_reports_without_writing(tmp_path, capsys):
    path = _config(tmp_path)
    code, out, _ = run(path, Fake(_items("a", "b")), capsys, dry_run=True)
    assert code == 0 and not (tmp_path / "state.json").exists()
    assert "2 id(s)" in out and "id=a" in out
    run(path, Fake(_items("a")), capsys)
    before = (tmp_path / "state.json").read_text()
    code, out, _ = run(path, Fake(_items("a", "z")), capsys, dry_run=True)
    assert "id=z" in out and (tmp_path / "state.json").read_text() == before


def test_state_is_bounded_and_keeps_current_ids(tmp_path, capsys):
    path = _config(tmp_path, max_seen=5)
    run(path, Fake(_items("keep")), capsys)
    for n in range(10):
        run(path, Fake(_items("keep", f"n{n}")), capsys)
    seen = json.loads((tmp_path / "state.json").read_text())["seen"]
    assert len(seen) == 5 and "keep" in seen and seen[-1] == "n9"


def test_state_write_is_atomic(tmp_path, capsys, monkeypatch):
    path = _config(tmp_path)
    run(path, Fake(_items("a")), capsys)
    before = (tmp_path / "state.json").read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(watch.os, "replace", boom)
    code, _, err = run(path, Fake(_items("a", "b")), capsys)
    assert code == 1 and "disk full" in err
    assert (tmp_path / "state.json").read_text() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_corrupt_state_is_an_error_not_a_new_baseline(tmp_path, capsys):
    path = _config(tmp_path)
    (tmp_path / "state.json").write_text("{oops")
    code, out, err = run(path, Fake(_items("a")), capsys)
    assert code == 1 and "state" in err and out == ""


@pytest.mark.parametrize("bad", [{"tool": ""}, {"server_url": "ftp://x"}, {"id_field": ""},
                                 {"arguments": []}, {"max_new": 0}])
def test_bad_config(tmp_path, capsys, bad):
    path = _config(tmp_path, **bad)
    code, out, err = run(path, Fake(), capsys)
    assert code == 2 and err.strip() and out == ""


def test_relative_state_and_default(tmp_path):
    cfg = watch.load_config(_config(tmp_path, state="sub/s.json"))
    assert cfg.state == tmp_path / "sub" / "s.json"
    data = json.loads((tmp_path / "watch.json").read_text())
    del data["state"]
    (tmp_path / "watch.json").write_text(json.dumps(data))
    assert watch.load_config(tmp_path / "watch.json").state == tmp_path / "watch.state.json"


def test_headers_expand_env_without_storing_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_TOKEN", "t0k")
    cfg = watch.load_config(_config(tmp_path, headers={"Authorization": "Bearer ${WATCH_TOKEN}"}))
    assert cfg.headers == {"Authorization": "Bearer t0k"}


def test_script_line(tmp_path):
    line = watch.script_line(tmp_path / "w.json", python="/opt/hermes/.venv/bin/python",
                             plugin_dir="/opt/data/plugins/fleet-policy")
    assert line == ("exec /opt/hermes/.venv/bin/python /opt/data/plugins/fleet-policy watch "
                    f"--config {tmp_path / 'w.json'} \"$@\"")


def test_parse_mcp_result():
    class Block:
        def __init__(self, text):
            self.text = text

    class Result:
        is_error = False
        structured_content = None
        content = [Block('{"a": '), Block('1}')]

    assert watch.parse_tool_result(Result()) == {"a": 1}
    Result.structured_content = {"b": 2}
    assert watch.parse_tool_result(Result()) == {"b": 2}
    Result.is_error = True
    with pytest.raises(watch.WatchError):
        watch.parse_tool_result(Result())


def _mcp_available():
    try:
        import mcp.server.mcpserver  # noqa: F401  (mcp >= 2)
        import uvicorn  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _mcp_available(), reason="mcp>=2 SDK + uvicorn not installed")
def test_real_streamable_http_server(tmp_path, capsys):
    from mcp_stub import serve

    with serve(tmp_path / "items.json") as url:
        (tmp_path / "items.json").write_text(json.dumps(["a"]))
        path = _config(tmp_path, server_url=url, tool="COMPOSIO_MULTI_EXECUTE_TOOL",
                       arguments={"tools": [{"tool_slug": "FAKE_FETCH", "arguments": {}}]})
        assert watch.run(str(path)) == 0
        assert json.loads(last_line(capsys.readouterr().out)) == {"wakeAgent": False}
        (tmp_path / "items.json").write_text(json.dumps(["a", "b"]))
        assert watch.run(str(path)) == 0
        out = capsys.readouterr().out
        assert out.startswith("1 new item(s)") and "id=b" in out


def test_standalone_entry_point_routes_watch(tmp_path):
    """``python <plugin-dir> watch --config ...`` (the form a cron .sh uses) reaches watch.run."""
    import subprocess
    import sys
    from pathlib import Path

    plugin_dir = Path(watch.__file__).resolve().parent
    bad = _config(tmp_path, tool="")
    proc = subprocess.run([sys.executable, str(plugin_dir), "watch", "--config", str(bad)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "invalid config" in proc.stderr and proc.stdout == ""
