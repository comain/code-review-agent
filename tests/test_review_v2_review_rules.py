from cr_agent.review_v2.review_rules import classify_review_file, match_review_rules


def test_review_file_policy_uses_default_test_excludes() -> None:
    policy = classify_review_file("provider/src/test/java/com/demo/FooTest.java")

    assert policy.skipped is True
    assert policy.test is True
    assert policy.skip_reason == "test file"


def test_review_file_policy_skips_unsupported_and_bloat_files() -> None:
    assert classify_review_file("web/static/app.min.js").skip_reason == "bloat file type"
    assert classify_review_file("assets/logo.svg").skip_reason == "unsupported file type"


def test_match_review_rules_adds_default_and_path_specific_rule() -> None:
    matches = match_review_rules(
        [
            "provider/src/main/java/com/demo/Foo.java",
            "provider/src/main/resources/mapper/FooMapper.xml",
            "src/app.py",
        ]
    )

    by_path = {item.path: item for item in matches}
    assert by_path["provider/src/main/java/com/demo/Foo.java"].rule_names == ["default", "java"]
    assert by_path["provider/src/main/resources/mapper/FooMapper.xml"].rule_names == [
        "default",
        "mapper_dao_xml",
    ]
    assert by_path["src/app.py"].rule_names == ["default"]
