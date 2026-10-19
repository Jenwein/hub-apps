"""The texts that scripts send to the user: chat messages and, later, report sections.

Each text is in English and in Chinese. Chinese is used when the language tag (`[chat] language`
of config.toml, such as `zh-CN`) starts with `zh`, English otherwise: cc-hub's rule. Every key
exists in both languages. Texts for the agent are not here: they are English files.
"""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "chat_hello": (
            "Mail is now connected to this chat. From now on, drafts, confirmation codes and "
            "alerts for your mailboxes arrive here."),
        "answer_about": 'About "{subject}":',
        "show_from": "From: {value}",
        "show_to": "To: {value}",
        "show_cc": "Cc: {value}",
        "show_date": "Date: {value}",
        "show_subject": "Subject: {value}",
        "show_attachments": "Attachments: {names}",
        "show_bytes": "{size} bytes",
        "show_more": "and {count} more",
        "show_no_text": "(the mail has no text)",
        "show_cut": "[the body goes on: cut at {limit} characters]",
        "unknown": "unknown",
        "show_bcc": "Bcc: {value}",
        "draft_forwarded": "the forwarded mail",
        "code_line": "To send draft {id}, reply with this code (valid for 24 hours): {code}",
        "quote_header": "On {date}, {sender} wrote:",
    },
    "zh": {
        "chat_hello": "邮件已连接到这个聊天。以后你邮箱的草稿、确认码和提醒都会发到这里。",
        "answer_about": "关于“{subject}”：",
        "show_from": "发件人：{value}",
        "show_to": "收件人：{value}",
        "show_cc": "抄送：{value}",
        "show_date": "日期：{value}",
        "show_subject": "主题：{value}",
        "show_attachments": "附件：{names}",
        "show_bytes": "{size} 字节",
        "show_more": "另有 {count} 个",
        "show_no_text": "（这封邮件没有正文）",
        "show_cut": "[正文未完：已在 {limit} 个字符处截断]",
        "unknown": "未知",
        "show_bcc": "密送：{value}",
        "draft_forwarded": "转发的原邮件",
        "code_line": "要发送草稿 {id}，请回复这个确认码（24 小时内有效）：{code}",
        "quote_header": "{date}，{sender} 写道：",
    },
}


def lang(tag: str) -> str:
    """`zh` for a language tag that starts with `zh` (zh, zh-CN, zh_TW), else `en`."""
    return "zh" if tag.strip().lower().startswith("zh") else "en"


def t(key: str, language: str, **values: object) -> str:
    """The text `key` in the language of the tag `language` (`en` and `zh` are tags too), with
    `{name}` filled in from `values`. An unknown key is a KeyError: it is a bug, not an input."""
    return STRINGS[lang(language)][key].format(**values)
