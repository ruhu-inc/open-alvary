"""The local Law check server (scripts.law_check_local) and the page files it serves for its own address
(scripts.law_check_site). The browser tests in tests/browser run against this server."""

import json

from fastapi.testclient import TestClient

from scripts import law_check_local, law_check_site

ORIGIN = "http://127.0.0.1:8771"


def _client() -> TestClient:
    return TestClient(law_check_local.build_app(ORIGIN))


def _call(client: TestClient, name: str, arguments: dict) -> dict:
    response = client.post(
        "/agents/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_the_committed_agent_files_name_the_published_address_and_no_other_machine():
    files = law_check_site.addressed_files()
    names = {str(path.relative_to(law_check_site.AGENTS_DIR)) for path in files}
    # The two networks and the page's manifest carry the address the agents call.
    assert {"law_desk.network.json", "check/law_check.network.json", "agent-manifest.json"} <= names
    for path in law_check_site.AGENTS_DIR.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "127.0.0.1" not in text and "localhost" not in text, path


def test_a_copy_for_another_address_names_that_address_and_leaves_the_runtime_alone(tmp_path):
    changed = law_check_site.write_site(ORIGIN, tmp_path / "agents")
    assert changed
    network = (tmp_path / "agents" / "law_desk.network.json").read_text(encoding="utf-8")
    assert f"{ORIGIN}/agents/mcp" in network
    assert law_check_site.PUBLISHED_ORIGIN not in network
    manifest = json.loads((tmp_path / "agents" / "agent-manifest.json").read_text(encoding="utf-8"))
    assert manifest["allowed_peers"] == [ORIGIN]
    # The vendored runtime is copied byte for byte: its pinned fingerprint must keep holding.
    for path in (law_check_site.AGENTS_DIR / "runtime").rglob("*"):
        if path.is_file():
            copy = tmp_path / "agents" / path.relative_to(law_check_site.AGENTS_DIR)
            assert copy.read_bytes() == path.read_bytes(), path


def test_the_local_server_serves_the_page_under_the_real_content_policy():
    client = _client()
    page = client.get("/agents/")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]
    network = client.get("/agents/law_desk.network.json").text
    assert f"{ORIGIN}/agents/mcp" in network and law_check_site.PUBLISHED_ORIGIN not in network
    assert client.get("/agents/../../accounts/tiers.json").status_code == 404
    assert client.get("/agents/runtime/PIN.txt").status_code == 200


def test_the_local_server_checks_a_quote_with_the_real_verifier_and_the_committed_plan():
    client = _client()
    found = _call(
        client,
        "check_claim",
        {"citation": law_check_local.CITATION, "quote": law_check_local.TRUE_QUOTE, "jurisdiction": "ng"},
    )
    assert not found.get("isError")
    receipt = json.loads(found["content"][0]["text"])["receipt"]
    assert receipt["findings"]["quote"] in {"exact", "normalised"}
    assert receipt["part"]["source_id"] == law_check_local.SOURCE_ID

    altered = _call(
        client,
        "check_claim",
        {"citation": law_check_local.CITATION, "quote": law_check_local.ALTERED_QUOTE, "jurisdiction": "ng"},
    )
    assert json.loads(altered["content"][0]["text"])["receipt"]["status"] == "wording_differs"
    keys = client.get("/.well-known/open-alvary-receipt-keys.json").json()
    assert receipt["key"]["kid"] in {key["kid"] for key in keys["keys"]}


def test_the_scripted_model_asks_for_one_check_and_then_answers():
    client = _client()

    def chat(messages, tools):
        body = {"messages": messages, "tools": [{"type": "function", "function": tool} for tool in tools]}
        response = client.post("/test-llm/v1/chat/completions", json=body)
        assert response.status_code == 200
        return [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    question = [{"role": "user", "content": "What does section 1(1) say?"}]
    verifier = {
        "name": "verifier",
        "parameters": {"properties": {"inquiry": {"type": "string"}}, "required": ["inquiry"]},
    }
    check = {"name": "mcp_check_claim", "parameters": {}}

    front = chat(question, [verifier])
    call = front[0]["choices"][0]["delta"]["tool_calls"][0]["function"]
    assert (
        call["name"] == "verifier"
        and "Freedom of Information Act 2011" in json.loads(call["arguments"])["inquiry"]
    )

    asked = chat(question, [check])
    arguments = json.loads(asked[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
    assert arguments["quote"] == law_check_local.TRUE_QUOTE

    after = chat([*question, {"role": "tool", "content": "{}"}], [verifier])
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in after)
    assert "says:" in text and "tool_calls" not in json.dumps(after)

    plain = chat([{"role": "user", "content": "Tell me [plain]"}], [verifier])
    assert "tool_calls" not in json.dumps(plain)
    wrong = chat([{"role": "user", "content": "Tell me [altered]"}], [check])
    wrong_arguments = json.loads(wrong[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
    assert wrong_arguments["quote"] == law_check_local.ALTERED_QUOTE
