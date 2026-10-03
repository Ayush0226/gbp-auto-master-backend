import json
from pathlib import Path


CONTRACT_PATH = Path(__file__).parents[1] / "docs" / "gbp-master-mcp-tools-v1.json"


def load_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_v1_tool_surface_is_stable_and_unique():
    contract = load_contract()
    names = [tool["name"] for tool in contract["tools"]]

    assert names == [
        "list_locations",
        "get_credit_balance",
        "list_reviews",
        "draft_review_reply",
        "publish_review_reply",
        "list_scheduled_posts",
        "schedule_post",
        "cancel_scheduled_post",
    ]
    assert len(names) == len(set(names))


def test_model_inputs_never_accept_identity_or_provider_secrets():
    contract = load_contract()
    forbidden = set(contract["authentication"]["forbiddenModelInputs"])

    for tool in contract["tools"]:
        inputs = set(tool["inputSchema"].get("properties", {}))
        assert inputs.isdisjoint(forbidden), tool["name"]


def test_oauth_uses_only_supabase_supported_identity_scopes():
    contract = load_contract()

    assert contract["authentication"]["oauthScopes"] == ["openid", "email", "profile"]
    assert set(contract["scopes"]) == {"openid", "email", "profile"}


def test_annotations_and_confirmation_match_side_effects():
    contract = load_contract()

    for tool in contract["tools"]:
        annotations = tool["annotations"]
        assert set(annotations) == {
            "readOnlyHint",
            "destructiveHint",
            "openWorldHint",
        }
        if annotations["readOnlyHint"]:
            assert tool["confirmation"] == "never"
            assert tool["creditCost"] == 0
        else:
            assert tool["confirmation"] == "always"


def test_credit_costs_match_current_billing_rules():
    tools = {tool["name"]: tool for tool in load_contract()["tools"]}

    assert tools["publish_review_reply"]["creditCost"] == 2.5
    assert tools["schedule_post"]["creditCost"] == 5
    assert tools["cancel_scheduled_post"]["creditCost"] == 0
