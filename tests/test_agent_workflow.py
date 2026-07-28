from bughound_agent import BugHoundAgent
from llm_client import MockClient


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])


class DangerousFixClient:
    """Fake offline client: valid analyzer JSON, but the "fix" introduces os.system()."""

    def complete(self, system_prompt, user_prompt):
        if "Return ONLY valid JSON" in system_prompt:
            return (
                '[{"type": "Reliability", "severity": "Low", '
                '"msg": "minor cleanup suggested"}]'
            )
        return (
            "def f(cmd):\n"
            "    try:\n"
            "        return os.system(cmd)\n"
            "    except Exception as e:\n"
            "        return None\n"
        )


def test_dangerous_fix_blocks_autofix_and_raises_risk():
    # LLM output here is well-formed JSON + valid Python, so it passes the
    # agent workflow's format checks. Only the risk assessor's dangerous-call
    # guardrail should catch that os.system() wasn't in the original code.
    agent = BugHoundAgent(client=DangerousFixClient())
    code = (
        "def f(cmd):\n"
        "    try:\n"
        "        return run(cmd)\n"
        "    except:\n"
        "        return None\n"
    )
    result = agent.run(code)

    risk = result["risk"]
    assert risk["should_autofix"] is False
    assert risk["level"] != "low"
    assert any("dangerous" in r.lower() for r in risk["reasons"])
