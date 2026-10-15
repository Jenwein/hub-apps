import dataclasses

import dns.exception
import dns.resolver
import mailkit.providers as mp
import pytest
from mailkit.config import HOST_PORT, MailError
from mailkit.providers import FEISHU_MX, PROVIDERS, Provider, detect, lookup_mx


def no_lookup(domain):
    pytest.fail(f"no MX lookup expected, but {domain} was looked up")


@pytest.mark.parametrize("address, key", [
    ("a@163.com", "163"), ("a@126.com", "126"), ("a@yeah.net", "yeah"),
    ("a@qq.com", "qq"), ("a@foxmail.com", "qq"), ("A@Gmail.com", "gmail"),
    ("a@googlemail.com", "gmail"),
])
def test_known_domains(address, key):
    got = detect(address, mx=lambda d: pytest.fail("no MX lookup for known domains"))
    assert got["provider"] == key


def test_known_domains_ignore_case_spaces_tags_and_a_trailing_dot():
    for address in ("  Me+news@QQ.COM ", "x@163.com.", "a.b+c+d@Gmail.Com"):
        assert detect(address, mx=no_lookup)["supported"] is True
    assert detect("me+shop@qq.com", mx=no_lookup)["provider"] == "qq"
    assert detect("x@163.com.", mx=no_lookup)["provider"] == "163"


def test_supported_result_has_exactly_four_keys():
    r = detect("a@qq.com", mx=no_lookup)
    assert set(r) == {"provider", "supported", "status", "reason"}
    assert (r["supported"], r["status"]) == (True, "supported")
    assert "qq.com" in r["reason"]


def test_custom_domain_by_mx():
    assert detect("me@corp.example", mx=lambda d: [FEISHU_MX])["provider"] == "feishu"
    assert detect("me@corp.example", mx=lambda d: ["aspmx.l.google.com."])["provider"] == "gmail"


def test_the_domain_is_what_the_lookup_gets():
    seen = []
    detect("Me+x@Corp.Example", mx=lambda d: seen.append(d) or [])
    assert seen == ["corp.example"]


def test_unsupported():
    r = detect("me@outlook.com", mx=lambda d: [])
    assert r == {"provider": None, "supported": False, "status": "unsupported",
                 "reason": r["reason"]}
    assert "not supported" in r["reason"]
    # Tencent enterprise mail
    r = detect("me@corp.example", mx=lambda d: ["mxbiz1.qq.com."])
    assert (r["supported"], r["status"]) == (False, "unsupported")


@pytest.mark.parametrize("hosts, service", [
    (["mxbiz1.qq.com.", "mxbiz2.qq.com."], "Tencent enterprise mail"),
    (["corp-example.mail.protection.outlook.com."], "Outlook"),
    (["mx-sg.larksuite.com."], "Lark"),
    (["mx01.mail.icloud.com."], "iCloud"),
    (["qiye163mx01.mxmail.netease.com."], "NetEase enterprise mail"),
])
def test_services_that_are_named_but_not_supported_yet(hosts, service):
    r = detect("me@corp.example", mx=lambda d: hosts)
    assert (r["provider"], r["supported"], r["status"]) == (None, False, "unsupported")
    assert service in r["reason"] and "not supported yet" in r["reason"]


@pytest.mark.parametrize(
    "address", ["me@outlook.com", "me@hotmail.com", "me@live.com", "me@icloud.com"])
def test_big_unsupported_domains_need_no_lookup(address):
    r = detect(address, mx=no_lookup)
    assert (r["provider"], r["supported"], r["status"]) == (None, False, "unsupported")
    assert "not supported yet" in r["reason"]


def ask_the_user(r):
    """An `unknown` result: no guess, and the reason tells the agent to ask the user which of
    the supported providers holds the mailbox and to pass it as --provider."""
    assert (r["provider"], r["supported"], r["status"]) == (None, False, "unknown")
    assert "not supported" not in r["reason"]
    assert "ask the user" in r["reason"] and "--provider" in r["reason"]
    assert all(key in r["reason"] for key in PROVIDERS)


def test_a_domain_without_mx_records_is_unknown_not_unsupported():
    # no network, a resolver that failed, or a domain whose mail a gateway takes
    r = detect("me@corp.example", mx=lambda d: [])
    ask_the_user(r)
    assert "corp.example" in r["reason"] and "MX" in r["reason"]


def test_mx_hosts_no_rule_knows_are_unknown_and_shown_to_the_agent():
    # a company's own mail gateway in front of Feishu or Google Workspace
    r = detect("me@corp.example", mx=lambda d: ["mail.corp.example.", "mx2.corp.example."])
    ask_the_user(r)
    assert "mail.corp.example" in r["reason"]


@pytest.mark.parametrize("host", [
    "mx1.feishu.cn.", "mx1.feishu.cn", "MX1.Feishu.CN.", "feishu.cn.", "a.b.feishu.cn.",
])
def test_mx_host_matches_on_the_suffix(host):
    assert detect("me@corp.example", mx=lambda d: [host])["provider"] == "feishu"


@pytest.mark.parametrize("host", [
    "evilfeishu.cn.", "feishu.cn.evil.example.", "mx1.feishu.cn.example.", "feishu.com.",
    "notgoogle.com.", "aspmx.l.google.com.evil.example.", "feishu.cn.x.",
])
def test_mx_host_must_match_at_a_label_boundary(host):
    r = detect("me@corp.example", mx=lambda d: [host])
    assert (r["provider"], r["supported"], r["status"]) == (None, False, "unknown")


@pytest.mark.parametrize("hosts", [
    ["aspmx.l.google.com.", "alt1.aspmx.l.google.com.", "alt2.aspmx.l.google.com."],
    ["aspmx.l.google.com.", "aspmx2.googlemail.com."],
    ["smtp.google.com."],
])
def test_google_workspace_mx_hosts(hosts):
    assert detect("me@corp.example", mx=lambda d: hosts)["provider"] == "gmail"


def test_a_backup_mx_of_another_service_does_not_hide_the_provider():
    # a real Feishu domain also lists an Alibaba DirectMail host
    hosts = ["mx1.feishu.cn.", "mx2.feishu.cn.", "mx01.dm.aliyun.com.", "mx3.feishu.cn."]
    assert detect("me@corp.example", mx=lambda d: hosts)["provider"] == "feishu"


def test_mx_hosts_of_two_services_are_not_guessed_at():
    r = detect("me@corp.example", mx=lambda d: ["mx1.feishu.cn.", "aspmx.l.google.com."])
    ask_the_user(r)
    assert "Feishu" in r["reason"] and "Google" in r["reason"]
    r = detect("me@corp.example", mx=lambda d: ["mx1.feishu.cn.", "mxbiz1.qq.com."])
    ask_the_user(r)  # it may be the Feishu mailbox
    assert "Tencent enterprise mail" in r["reason"]
    # two services, neither supported: nothing to ask
    r = detect("me@corp.example", mx=lambda d: ["mxbiz1.qq.com.", "x.mail.protection.outlook.com."])
    assert (r["status"], r["supported"]) == ("unsupported", False)
    assert "not supported yet" in r["reason"]


@pytest.mark.parametrize(
    "address", ["", "plain", "@qq.com", "a@", "a b@qq.com", "a@b@qq.com", "a@qq com"])
def test_not_an_address_is_an_error(address):
    with pytest.raises(MailError, match="not an email address"):
        detect(address, mx=no_lookup)


def test_table():
    assert PROVIDERS["feishu"].imap == "imap.feishu.cn:993"
    assert PROVIDERS["feishu"].smtp == "smtp.feishu.cn:465"
    assert PROVIDERS["feishu"].max_message_mb == 36 and PROVIDERS["gmail"].max_message_mb == 25
    assert PROVIDERS["163"].send_id and PROVIDERS["126"].send_id and not PROVIDERS["qq"].send_id
    assert {p.guide for p in PROVIDERS.values()} == {"netease.md", "qq.md", "gmail.md", "feishu.md"}


def test_table_servers():
    expected = {
        "163": ("imap.163.com:993", "smtp.163.com:465"),
        "126": ("imap.126.com:993", "smtp.126.com:465"),
        "yeah": ("imap.yeah.net:993", "smtp.yeah.net:465"),
        "qq": ("imap.qq.com:993", "smtp.qq.com:465"),
        "gmail": ("imap.gmail.com:993", "smtp.gmail.com:465"),
        "feishu": ("imap.feishu.cn:993", "smtp.feishu.cn:465"),
    }
    assert set(PROVIDERS) == set(expected)
    for key, (imap, smtp) in expected.items():
        assert (PROVIDERS[key].imap, PROVIDERS[key].smtp) == (imap, smtp), key


def test_table_is_consistent():
    for key, p in PROVIDERS.items():
        assert isinstance(p, Provider) and p.key == key
        assert p.name and p.guide.endswith(".md")
        assert HOST_PORT.match(p.imap) and HOST_PORT.match(p.smtp), key  # what config.toml accepts
        assert isinstance(p.send_id, bool) and p.max_message_mb > 0
        assert p.saves_sent in (True, False, None)
        for folders in (p.trash, p.archive, p.sent):
            assert isinstance(folders, tuple) and all(isinstance(f, str) and f for f in folders)
    assert PROVIDERS["163"].guide == PROVIDERS["126"].guide == PROVIDERS["yeah"].guide
    with pytest.raises(dataclasses.FrozenInstanceError):
        PROVIDERS["qq"].imap = "x:1"


def test_gmail_folders_are_the_documented_ones():
    p = PROVIDERS["gmail"]
    assert p.saves_sent is True  # Google: mail sent over SMTP is copied to Sent automatically
    assert p.trash == ("[Gmail]/Trash",) and p.sent == ("[Gmail]/Sent Mail",)
    assert p.archive == ("[Gmail]/All Mail",)


def test_feishu_mx_is_only_a_suffix():
    assert FEISHU_MX == "feishu.cn."


# -- lookup_mx --------------------------------------------------------------------------------


class FakeAnswer:
    def __init__(self, preference, exchange):
        self.preference, self.exchange = preference, exchange


def fake_resolver(monkeypatch, answers=None, error=None):
    """Replace dns.resolver.Resolver; returns what the code did to it."""
    seen = {}

    class FakeResolver:
        def __init__(self, *args, **kwargs):
            seen["resolver"] = self

        def resolve(self, name, rdtype, *args, **kwargs):
            seen["query"] = (name, rdtype)
            if error:
                raise error
            return answers

    monkeypatch.setattr(dns.resolver, "Resolver", FakeResolver)
    return seen


def test_lookup_mx_returns_hosts_by_preference(monkeypatch):
    seen = fake_resolver(monkeypatch, [
        FakeAnswer(20, "MX2.Corp.Example."), FakeAnswer(10, "mx1.corp.example."),
    ])
    assert lookup_mx("corp.example") == ["mx1.corp.example.", "mx2.corp.example."]
    assert seen["query"] == ("corp.example", "MX")


def test_lookup_mx_has_a_short_timeout(monkeypatch):
    seen = fake_resolver(monkeypatch, [])
    lookup_mx("corp.example")
    resolver = seen["resolver"]
    assert 0 < resolver.lifetime <= 5 and 0 < resolver.timeout <= resolver.lifetime


@pytest.mark.parametrize("error", [
    dns.resolver.NXDOMAIN(), dns.resolver.NoAnswer(), dns.resolver.NoNameservers(),
    dns.resolver.NoResolverConfiguration(), dns.exception.Timeout(),
    dns.exception.DNSException("anything else"), OSError("Network is unreachable"),
    UnicodeError("label empty or too long"), ValueError("bad name"),
])
def test_lookup_mx_never_raises(monkeypatch, error):
    fake_resolver(monkeypatch, error=error)
    assert lookup_mx("corp.example") == []


def test_lookup_mx_survives_a_resolver_that_cannot_start(monkeypatch):
    def broken(*args, **kwargs):
        raise dns.resolver.NoResolverConfiguration("no resolv.conf")

    monkeypatch.setattr(dns.resolver, "Resolver", broken)
    assert lookup_mx("corp.example") == []


@pytest.mark.parametrize("domain", ["a" * 300 + ".example", "x..y"])
def test_lookup_mx_refused_names_do_not_raise(domain):
    # the resolver itself refuses these names, before it sends anything
    assert lookup_mx(domain) == []


def test_the_default_lookup_is_lookup_mx():
    import inspect

    assert inspect.signature(detect).parameters["mx"].default is mp.lookup_mx
