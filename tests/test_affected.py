from cryptobot import affected


def test_docs_tests_and_research_restart_nothing_that_trades():
    assert affected.affected(["CLAUDE.md", "tests/test_social.py", "reports/r.md"]) == []
    assert "crypto-bot.service" not in affected.affected(["src/cryptobot/screen.py"])


def test_a_collector_fix_does_not_restart_the_paper_trader():
    assert affected.affected(["src/cryptobot/tgweb.py"]) == ["crypto-bot-social.service"]
    units = affected.affected(["src/cryptobot/xscrape.py"])
    assert "crypto-bot.service" not in units and "crypto-bot-x.service" in units


def test_shared_code_units_and_the_lock_restart_their_services():
    assert "crypto-bot.service" in affected.affected(["src/cryptobot/curve.py"])
    assert "crypto-bot.service" in affected.affected(["src/cryptobot/social_state.py"])
    assert affected.affected(["deploy/crypto-bot-x.service"]) == ["crypto-bot-x.service"]
    assert len(affected.affected(["uv.lock"])) == 3


def test_closure_follows_imports_inside_functions(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    from cryptobot import b\n")
    (tmp_path / "b.py").write_text("import cryptobot.c\n")
    (tmp_path / "c.py").write_text("x = 1\n")
    assert affected.closure("a", tmp_path) == {"a", "b", "c"}
