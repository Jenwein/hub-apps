"""Settings of the ledger app: config.toml in the ledger directory, and the bill sources."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class LedgerError(Exception):
    """An expected failure. The command line prints `lk: <message>` and exits with 1."""


@dataclass(frozen=True)
class Source:
    name: str
    kind: str  # "platform" | "bank"
    provider: str | None  # double-entry-generator provider; None = the agent reads the file
    extra_args: tuple[str, ...] = ()


SOURCES: dict[str, Source] = {
    "alipay": Source("alipay", "platform", "alipay"),
    "wechat": Source("wechat", "platform", "wechat", ("--ignore-invalid-tx-types",)),
    "meituan": Source("meituan", "platform", "mt"),
    "jd": Source("jd", "platform", "jd"),
    "ccb": Source("ccb", "bank", "ccb"),
    "icbc": Source("icbc", "bank", "icbc"),
    "bosc": Source("bosc", "bank", None),
    "nbcb": Source("nbcb", "bank", None),
}

KINDS = ("platform", "bank")

# The values of the seed config.toml.
DEFAULT_CHANNEL_KEYWORDS = ("支付宝", "财付通", "美团", "京东", "抖音", "拼多多")
DEFAULT_INVEST_KEYWORDS = ("基金", "申购", "赎回", "证券", "理财", "股票")


@dataclass(frozen=True)
class Settings:
    currency: str
    start: str | None
    chat_default: str | None
    chat_channels: dict[str, str]
    sources: tuple[str, ...]
    window_days: int
    channel_keywords: tuple[str, ...]
    invest_keywords: tuple[str, ...]
    extra_sources: dict[str, Source]
    fava_port: int
    tailscale: bool


def _table(parent: dict, key: str, where: str) -> dict:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise LedgerError(f"config.toml: {where} must be a table")
    return value


def _str(table: dict, key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise LedgerError(f"config.toml: {where} must be a string")
    return value


def _int(table: dict, key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LedgerError(f"config.toml: {where} must be an integer")
    return value


def _bool(table: dict, key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise LedgerError(f"config.toml: {where} must be true or false")
    return value


def _strings(table: dict, key: str, default: tuple[str, ...], where: str) -> tuple[str, ...]:
    value = table.get(key, default)
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise LedgerError(f"config.toml: {where} must be a list of strings")
    return tuple(value)


def _start(value: str) -> str | None:
    if value == "":
        return None
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise LedgerError(f"config.toml: ledger.start must be YYYY-MM-DD, got {value!r}")


def _extra_sources(data: dict) -> dict[str, Source]:
    found = {}
    for name, entry in _table(data, "sources", "[sources]").items():
        where = f"[sources.{name}]"
        if not isinstance(entry, dict):
            raise LedgerError(f"config.toml: {where} must be a table")
        kind = _str(entry, "kind", "", f"{where} kind")
        if kind not in KINDS:
            raise LedgerError(f"config.toml: {where} kind must be one of {', '.join(KINDS)}")
        provider = _str(entry, "provider", "", f"{where} provider")
        found[name] = Source(name, kind, provider or None)
    return found


def parse_settings(data: dict) -> Settings:
    """Settings from parsed config.toml data. Missing keys take the seed defaults."""
    ledger = _table(data, "ledger", "[ledger]")
    chat = _table(data, "chat", "[chat]")
    imports = _table(data, "import", "[import]")
    fava = _table(data, "fava", "[fava]")
    channels = _table(chat, "channels", "[chat.channels]")
    for channel, account in channels.items():
        if not isinstance(account, str):
            raise LedgerError(f"config.toml: chat.channels.{channel} must be a string")
    return Settings(
        currency=_str(ledger, "currency", "CNY", "ledger.currency"),
        start=_start(_str(ledger, "start", "", "ledger.start")),
        chat_default=_str(chat, "default", "", "chat.default") or None,
        chat_channels=dict(channels),
        sources=_strings(imports, "sources", (), "import.sources"),
        window_days=_int(imports, "window_days", 3, "import.window_days"),
        channel_keywords=_strings(
            imports, "channel_keywords", DEFAULT_CHANNEL_KEYWORDS, "import.channel_keywords"),
        invest_keywords=_strings(
            imports, "invest_keywords", DEFAULT_INVEST_KEYWORDS, "import.invest_keywords"),
        extra_sources=_extra_sources(data),
        fava_port=_int(fava, "port", 5000, "fava.port"),
        tailscale=_bool(fava, "tailscale", False, "fava.tailscale"),
    )


def load_settings(ledger_dir: Path) -> Settings:
    path = Path(ledger_dir) / "config.toml"
    if not path.is_file():
        raise LedgerError(f"no config.toml in {ledger_dir}")
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise LedgerError(f"cannot read {path}: {err}") from err
    return parse_settings(data)


def source(settings: Settings, name: str) -> Source:
    """A built-in bill source, or one the user declared under [sources.<name>]."""
    found = SOURCES.get(name) or settings.extra_sources.get(name)
    if found is None:
        raise LedgerError(f"unknown source {name}")
    return found
