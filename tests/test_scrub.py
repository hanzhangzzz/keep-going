from keep_going.corpus.scrub import scrub


def test_replaces_user_path():
    assert "/Users/USER/foo" in scrub(
        "/Users/sample/foo", user_replacement="USER", real_user="sample"
    )


def test_masks_email():
    out = scrub("contact me at sample.user@example.com please")
    assert "sample.user@example.com" not in out
    assert "example.com" not in out
    assert "s***@***" in out


def test_masks_prefixed_token():
    out = scrub("export OPENAI_API_KEY=" + "sk-" + "proj-AAAABBBBCCCC1234567890DDDD")
    assert "<REDACTED_TOKEN>" in out


def test_keeps_short_strings():
    assert scrub("hello world") == "hello world"


def test_masks_long_high_entropy_string():
    s = "abc123" * 6  # 36 chars mixed
    out = scrub(s)
    assert "<REDACTED_" in out


def test_empty_safe():
    assert scrub("") == ""


def test_masks_extra_hosts_from_env(monkeypatch):
    monkeypatch.setenv("KEEP_GOING_SCRUB_EXTRA_HOSTS", "internal-corp.com, other.cn")
    out = scrub("see https://gitlab.internal-corp.com/team/repo and api.other.cn/v1")
    assert "internal-corp.com" not in out
    assert "other.cn" not in out
    assert out.count("<REDACTED_HOST>") == 2
