"""The mail providers the app supports, and which one an address belongs to.

Each provider is a row in `PROVIDERS`: its servers (host:port, SSL on both) and what differs
from the standard behaviour of an IMAP/SMTP server. Adding a provider is a row, a guide file and
a rule in `_MX_RULES` or `_KNOWN_DOMAINS`; nothing else changes.

`detect` names the provider of an address: from the domain when the domain is a well-known one,
else from the MX records of the domain (a company's own domain on Feishu or Google Workspace).
It never guesses. An address of a service that is known and not supported yet (Outlook,
iCloud, Lark, Tencent or NetEase enterprise mail) is `unsupported`. An address it cannot place
(MX records no rule knows, such as a company's own mail gateway in front of Feishu, or no MX
records at all because DNS failed) is `unknown`: the agent asks the user which supported
provider holds it, and the login check of `account add --provider` confirms the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ADDRESS, MailError


@dataclass(frozen=True)
class Provider:
    key: str  # the `provider` value in config.toml
    name: str  # as the user knows it
    imap: str  # host:port, SSL
    smtp: str  # host:port, SSL
    send_id: bool  # the server wants an IMAP ID command after login (NetEase refuses without it)
    max_message_mb: int  # the largest message the provider takes; warned about in the draft
    saves_sent: bool | None  # does a message sent over SMTP land in Sent by itself? None: unknown
    # Folder names to fall back on when the server does not mark the folder with its SPECIAL-USE
    # attribute (\Trash, \Archive or \All, \Sent). The attribute comes first; an empty tuple
    # means no name is known to be right.
    trash: tuple[str, ...]
    archive: tuple[str, ...]
    sent: tuple[str, ...]
    guide: str  # the file in skills/mail/providers/ that tells the user where to get the password


# Sources (checked 2026-10-14; "to confirm" values are settled on real accounts at acceptance):
# - servers: each provider's own client-setup pages; NetEase and QQ also by the many guides that
#   agree on imap/smtp.<domain>:993/465.
# - send_id: NetEase answers "Unsafe Login" to a client that has not sent the IMAP ID command.
# - max_message_mb: Feishu 36 and Gmail 25 are their help pages (the spec has the Feishu one).
#   QQ 50 is "ordinary attachment 50M, body plus attachments 55M" on
#   service.mail.qq.com/detail/0/479 (the lower of the two).
#   NetEase 20 is what the 163 and 126 help pages give for sending with client software (a page
#   from 2007 says 15; pages for the web mail and the Mail Master app say 50), the cautious
#   middle; to confirm.
# - saves_sent: Gmail copies SMTP mail to Sent itself (Gmail Help, "Choose your IMAP email client
#   settings"). QQ has a setting "save to the server after SMTP sending", so it depends on the
#   account. NetEase and Feishu do not say; to confirm.
# - folders: Gmail's names are in Google's IMAP extensions page ([Gmail]/Trash, [Gmail]/All Mail,
#   [Gmail]/Sent Mail) but are translated to the account's language, so SPECIAL-USE comes first.
#   The others are left to SPECIAL-USE; to confirm.
PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider(
            "163", "NetEase 163 Mail", "imap.163.com:993", "smtp.163.com:465",
            send_id=True, max_message_mb=20, saves_sent=None,
            trash=(), archive=(), sent=(), guide="netease.md"),
        Provider(
            "126", "NetEase 126 Mail", "imap.126.com:993", "smtp.126.com:465",
            send_id=True, max_message_mb=20, saves_sent=None,
            trash=(), archive=(), sent=(), guide="netease.md"),
        Provider(
            "yeah", "NetEase yeah.net Mail", "imap.yeah.net:993", "smtp.yeah.net:465",
            send_id=True, max_message_mb=20, saves_sent=None,
            trash=(), archive=(), sent=(), guide="netease.md"),
        Provider(
            "qq", "QQ Mail", "imap.qq.com:993", "smtp.qq.com:465",
            send_id=False, max_message_mb=50, saves_sent=None,
            trash=(), archive=(), sent=(), guide="qq.md"),
        Provider(
            "gmail", "Gmail", "imap.gmail.com:993", "smtp.gmail.com:465",
            send_id=False, max_message_mb=25, saves_sent=True,
            trash=("[Gmail]/Trash",), archive=("[Gmail]/All Mail",),
            sent=("[Gmail]/Sent Mail",), guide="gmail.md"),
        Provider(
            "feishu", "Feishu Mail", "imap.feishu.cn:993", "smtp.feishu.cn:465",
            send_id=False, max_message_mb=36, saves_sent=None,
            trash=(), archive=(), sent=(), guide="feishu.md"),
    )
}

# -- detection --------------------------------------------------------------------------------

# Domains that belong to one provider, whoever the user is.
_KNOWN_DOMAINS = {
    "163.com": "163", "126.com": "126", "yeah.net": "yeah",
    "qq.com": "qq", "foxmail.com": "qq",
    "gmail.com": "gmail", "googlemail.com": "gmail",
}
# Domains of services that are named but not supported yet: no point in looking up their MX.
_UNSUPPORTED_DOMAINS = {
    "outlook.com": "Outlook", "hotmail.com": "Outlook", "live.com": "Outlook", "msn.com": "Outlook",
    "icloud.com": "iCloud Mail", "me.com": "iCloud Mail", "mac.com": "iCloud Mail",
}

# What the hosts in a domain's MX records end with when a provider hosts its mail. Feishu's own
# docs tell an admin to point a company domain at mx1/mx2/mx3.feishu.cn, and every Feishu-hosted
# public company domain looked up on 2026-10-14 (six of them) lists exactly these three; only
# the suffix is kept here, no company's domain.
FEISHU_MX = "feishu.cn."
_GOOGLE = "Google Workspace (Gmail)"

# (host suffix, provider key or None when not supported yet, name of the service)
# Matched at a label boundary, so "mx1.feishu.cn." and "feishu.cn." match "feishu.cn." and
# "evilfeishu.cn." does not. NetEase and Tencent enterprise mail are other services on other
# servers (imap.qiye.163.com, imap.exmail.qq.com), not 163 or QQ mail.
_MX_RULES = (
    (FEISHU_MX, "feishu", "Feishu Mail"),
    ("google.com.", "gmail", _GOOGLE),
    ("googlemail.com.", "gmail", _GOOGLE),
    ("mxbiz1.qq.com.", None, "Tencent enterprise mail"),
    ("mxbiz2.qq.com.", None, "Tencent enterprise mail"),
    ("mxmail.netease.com.", None, "NetEase enterprise mail"),
    ("outlook.com.", None, "Outlook"),
    ("outlook.cn.", None, "Outlook"),
    ("icloud.com.", None, "iCloud Mail"),
    ("larksuite.com.", None, "Lark"),
)

MX_TIMEOUT = 4.0  # seconds, for one whole MX lookup; adding a mailbox does not wait longer


def lookup_mx(domain: str) -> list[str]:
    """The mail exchangers of `domain`, lower case with the final dot, most preferred first.
    Never raises on a DNS problem (no such domain, no MX, no answer in time, no network, a name
    the resolver refuses): the answer is then []."""
    import dns.resolver  # (not at the top: only adding a mailbox needs it)

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = MX_TIMEOUT / 2  # per server
        resolver.lifetime = MX_TIMEOUT
        answers = resolver.resolve(domain, "MX")
        return [str(a.exchange).lower() for a in sorted(answers, key=lambda a: a.preference)]
    except Exception:  # noqa: BLE001
        # dnspython's own errors, OSError for a network that is not there, UnicodeError for a
        # name the resolver cannot use
        return []


def _service(host: str) -> tuple[str | None, str] | None:
    """(provider key or None, service name) for an MX host, or None when no rule knows it."""
    host = host.strip().lower().rstrip(".") + "."
    for suffix, key, name in _MX_RULES:
        if host == suffix or host.endswith("." + suffix):
            return key, name
    return None


def _result(provider: str | None, status: str, reason: str) -> dict:
    return {"provider": provider, "supported": provider is not None, "status": status,
            "reason": reason}


def _unknown(why: str) -> dict:
    """A domain the app cannot place: the agent asks the user, nothing is guessed."""
    keys = ", ".join(PROVIDERS)
    return _result(None, "unknown", (
        f"{why}; ask the user which of the supported providers holds the mailbox ({keys}) and "
        "pass it to account add as --provider: the login check confirms it. Only when it is "
        "none of them, the mailbox cannot be added yet"))


def detect(address: str, *, mx=lookup_mx) -> dict:
    """Which supported provider `address` belongs to: {"provider": key or None, "supported":
    bool, "status": "supported" | "unsupported" | "unknown", "reason": text for the agent}.

    `unsupported`: the address belongs to a service that is known and not supported yet.
    `unknown`: nothing places it (MX records no rule knows, records of two services, or no MX
    records at all); the reason tells the agent to ask the user which supported provider holds
    it. A well-known domain is placed without any lookup; any other domain by its MX records
    (`mx` returns them for a domain, [] when there are none or DNS fails; the default is
    `lookup_mx`). Raises MailError when `address` is not an address at all."""
    text = address.strip()
    domain = text.rpartition("@")[2].lower().rstrip(".")
    if not ADDRESS.match(text) or not domain:
        raise MailError(f"not an email address: {address!r}")
    if domain in _KNOWN_DOMAINS:
        provider = PROVIDERS[_KNOWN_DOMAINS[domain]]
        return _result(provider.key, "supported", f"{domain} is a {provider.name} domain")
    if domain in _UNSUPPORTED_DOMAINS:
        return _result(None, "unsupported",
                       f"{_UNSUPPORTED_DOMAINS[domain]} is not supported yet ({domain})")

    hosts = mx(domain)
    # (a domain may list a backup host of another kind, such as a mail relay: only the hosts a
    # rule knows count)
    services = list(dict.fromkeys(s for s in map(_service, hosts) if s))
    names = " and ".join(name for _, name in services)
    if len(services) == 1 and services[0][0]:
        key, name = services[0]
        return _result(key, "supported", f"the MX records of {domain} point at {name}")
    if services and not any(key for key, _ in services):
        verb = "is" if len(services) == 1 else "are"
        return _result(None, "unsupported", f"{names} {verb} not supported yet ({domain})")
    if services:
        return _unknown(f"cannot tell which service holds {domain}: its MX records name {names}")
    if hosts:
        shown = ", ".join(hosts[:3])
        return _unknown(f"cannot tell which service holds {domain}: its MX records ({shown}) "
                        "name no provider this app knows, as with a company's own mail gateway")
    return _unknown(f"cannot tell which service holds {domain}: it is not a well-known mail "
                    "domain and no MX records could be looked up for it (no network, a DNS "
                    "failure, or a domain that takes no mail)")
