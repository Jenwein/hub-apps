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
        "alert_head": "Urgent mail in {mailbox}",
        "alert_reason": "Why: {reason}",
        "alert_amount": "Amount: {amount}",
        "alert_deadline": "Deadline: {deadline}",
        "alert_phishing": (
            "Warning: this mail may be phishing. Do not open its links or attachments, and do "
            "not reply to it."),
        "alert_phishing_short": "may be phishing",
        "alert_many": "{count} urgent mails:",
        "login_failed": (
            "Mail: {mailbox} cannot sign in any more (the server refused the login), so its mail "
            "is not checked and urgent mail there is not alerted. To sign in again, type this in "
            "the server's terminal, in the hub's folder: {fix}"),
        "cannot_connect": (
            "Mail: {mailbox} could not be checked for {minutes} minutes ({why}). Urgent mail "
            "there is alerted once it can be checked again."),
        "reader_down": (
            "Mail: the new mail in {mailbox} could not be sorted because the reader did not "
            "answer ({why}), so urgent mail there is not alerted yet. It is tried again at every "
            "check. To test the reader, type this in the server's terminal, in the hub's folder: "
            "uv run apps/mail/scripts/mx.py reader-check"),
        "reader_unsorted": (
            "Mail: none of the {count} new mails in {mailbox} could be sorted, so none of them was "
            "alerted; the daily report lists them. To test the reader, type this in the server's "
            "terminal, in the hub's folder: uv run apps/mail/scripts/mx.py reader-check"),
        "carillon_down": (
            "Mail: the listener for new mail (carillon) has not been running for more than "
            "{minutes} minutes, so new mail is only checked every {every} minutes and alerts can "
            "come late. Its log: {log}"),
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
        "alert_head": "{mailbox} 有一封紧急邮件",
        "alert_reason": "原因：{reason}",
        "alert_amount": "金额：{amount}",
        "alert_deadline": "截止日期：{deadline}",
        "alert_phishing": "警告：这封邮件可能是钓鱼邮件。不要打开其中的链接或附件，也不要回复。",
        "alert_phishing_short": "可能是钓鱼邮件",
        "alert_many": "{count} 封紧急邮件：",
        "login_failed": (
            "邮件：{mailbox} 无法再登录（服务器拒绝了登录），所以不再检查它的邮件，其中的紧急邮件"
            "也不会提醒。要重新登录，请在服务器的终端里、总站的目录下输入：{fix}"),
        "cannot_connect": (
            "邮件：{mailbox} 已经 {minutes} 分钟无法检查（{why}）。恢复检查后，会提醒其中的紧急"
            "邮件。"),
        "reader_down": (
            "邮件：{mailbox} 的新邮件无法分类，因为读信程序没有回应（{why}），所以其中的紧急邮件"
            "暂时不会提醒。每次检查时都会重试。要测试读信程序，请在服务器的终端里、总站的目录下"
            "输入：uv run apps/mail/scripts/mx.py reader-check"),
        "reader_unsorted": (
            "邮件：{mailbox} 的 {count} 封新邮件都无法分类，所以都没有提醒；每日汇报里会列出它们。"
            "要测试读信程序，请在服务器的终端里、总站的目录下输入："
            "uv run apps/mail/scripts/mx.py reader-check"),
        "carillon_down": (
            "邮件：监听新邮件的程序（carillon）已经超过 {minutes} 分钟没有运行，所以现在每 "
            "{every} 分钟才检查一次新邮件，提醒可能会晚到。它的日志：{log}"),
    },
}


def lang(tag: str) -> str:
    """`zh` for a language tag that starts with `zh` (zh, zh-CN, zh_TW), else `en`."""
    return "zh" if tag.strip().lower().startswith("zh") else "en"


def t(key: str, language: str, **values: object) -> str:
    """The text `key` in the language of the tag `language` (`en` and `zh` are tags too), with
    `{name}` filled in from `values`. An unknown key is a KeyError: it is a bug, not an input."""
    return STRINGS[lang(language)][key].format(**values)
