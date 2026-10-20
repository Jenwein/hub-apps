"""The texts that scripts send to the user: chat messages and report sections.

Each text is in English and in Chinese. Chinese is used when the language tag (`[chat] language`
of config.toml for chat messages, such as `zh-CN`; CC_HUB_LANGUAGE for report sections) starts
with `zh`, English otherwise: cc-hub's rule. Every key exists in both languages. Texts for the
agent are not here: they are English files.
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
        "alert_backlog": "{count} alerts that could not be sent when they came:",
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
        # the report sections
        "urgent": "Urgent:",
        "awaiting_reply": "Waiting for your reply:",
        "due_soon": "Due soon:",
        "orders": "Orders and deliveries:",
        "bills": "Bills and receipts:",
        "other": "Other:",
        "unsorted": "Could not be sorted, so have a look yourself:",
        "phishing": "may be phishing",
        "daily_nothing": "No new mail needs your attention.",
        "daily_nothing_read": (
            "No new mail needs your attention in the mailboxes that could be read."),
        "more_mail": (
            "Not shown, to keep the report short: {n} more. Ask in the chat to list your mail."),
        "unfetched": (
            "Mail in {mailbox} that could not be fetched: {n}. The next report tries again."),
        "unfetched_dropped": (
            "Mail in {mailbox} that took too long to fetch, three reports in a row: {n}. It is "
            "left out from now on: look for it in the mailbox itself."),
        "not_sorted": "Mail not sorted in time: {n}. The next report lists it.",
        "reader_failed": (
            "Mail not sorted because the reader did not answer ({why}): {n}. The next report "
            "tries again. To test the reader, type this in the server's terminal, in the hub's "
            "folder: uv run apps/mail/scripts/mx.py reader-check"),
        "login_problem": (
            "{mailbox} cannot sign in (the server refused the login), so its mail is not in this "
            "report. To sign in again, type this in the server's terminal, in the hub's folder: "
            "{fix}"),
        "cannot_read": (
            "{mailbox} could not be read ({why}); its mail is listed once it can be read again."),
        "alerts_ran": "Alerts ran the whole time since the last report.",
        "alerts_down": "Alerts were down for {duration} since the last report.",
        "alerts_stopped": (
            "Alerts are on, but the alert watcher is not running here, so urgent mail is not "
            "alerted. To see why, type this in the server's terminal, in the hub's folder: "
            "uv run apps/mail/scripts/mx.py alerts-status"),
        "alerts_late": (
            "Urgent mails whose alert went out late because the chat could not be reached: {n}."),
        "alerts_queued": "Urgent mails whose alert still waits for the chat: {n}.",
        "duration_days": "{n} d",
        "duration_hours": "{n} h",
        "duration_minutes": "{n} min",
        "new_version": "{tool} {latest} is out; the mail app still uses {pinned}.",
        "versions_current": "himalaya, carillon and ortie are at their latest releases.",
        "versions_unknown": (
            "The latest releases of some tools could not be checked: GitHub did not answer."),
        "alerts_down_month": "Alerts were down for {duration} this month.",
        "alerts_ran_month": "Alerts ran the whole month.",
        "login_problem_month": "{mailbox}: {kind}, {count} this month.",
        "times_one": "once",
        "times": "{n} times",
        "kind_auth": "the server refused the login",
        "kind_connect": "it could not be checked",
        "kind_reader": "its new mail could not be sorted",
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
        "alert_backlog": "{count} 条当时未能发出的提醒：",
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
            "邮件：{mailbox} 的 {count} 封新邮件都无法分类，所以都没有提醒；日报里会列出它们。"
            "要测试读信程序，请在服务器的终端里、总站的目录下输入："
            "uv run apps/mail/scripts/mx.py reader-check"),
        "carillon_down": (
            "邮件：监听新邮件的程序（carillon）已经超过 {minutes} 分钟没有运行，所以现在每 "
            "{every} 分钟才检查一次新邮件，提醒可能会晚到。它的日志：{log}"),
        # the report sections
        "urgent": "紧急：",
        "awaiting_reply": "等你回复：",
        "due_soon": "快到期：",
        "orders": "订单和快递：",
        "bills": "账单和收据：",
        "other": "其他：",
        "unsorted": "无法分类，请自己看一下：",
        "phishing": "可能是钓鱼邮件",
        "daily_nothing": "没有需要你关注的新邮件。",
        "daily_nothing_read": "能读取的邮箱里没有需要你关注的新邮件。",
        "more_mail": "为了让日报不太长，另有 {n} 封没有列出。可以在聊天里要求列出你的邮件。",
        "unfetched": "{mailbox} 有 {n} 封邮件无法取回，下次日报会再试。",
        "unfetched_dropped": (
            "{mailbox} 有 {n} 封邮件连续三份日报都取回超时，以后不再列出，请直接到邮箱里找它。"),
        "not_sorted": "有 {n} 封邮件没能及时分类，下次日报会列出。",
        "reader_failed": (
            "有 {n} 封邮件没有分类，因为读信程序没有回应（{why}），下次日报会再试。要测试读信程序，"
            "请在服务器的终端里、总站的目录下输入：uv run apps/mail/scripts/mx.py reader-check"),
        "login_problem": (
            "{mailbox} 无法登录（服务器拒绝了登录），所以这份日报里没有它的邮件。要重新登录，请在"
            "服务器的终端里、总站的目录下输入：{fix}"),
        "cannot_read": "{mailbox} 无法读取（{why}），等能读取时再列出它的邮件。",
        "alerts_ran": "自上次日报以来，实时提醒一直在运行。",
        "alerts_down": "自上次日报以来，实时提醒有 {duration}没有运行。",
        "alerts_stopped": (
            "实时提醒已打开，但提醒程序没有在这台机器上运行，所以紧急邮件不会提醒。要查看原因，"
            "请在服务器的终端里、总站的目录下输入：uv run apps/mail/scripts/mx.py alerts-status"),
        "alerts_late": "因为聊天连不上，有 {n} 封紧急邮件的提醒晚发了。",
        "alerts_queued": "还有 {n} 封紧急邮件的提醒在等聊天恢复。",
        "duration_days": "{n} 天",
        "duration_hours": "{n} 小时",
        "duration_minutes": "{n} 分钟",
        "new_version": "{tool} 出了 {latest}，邮件应用用的还是 {pinned}。",
        "versions_current": "himalaya、carillon 和 ortie 都是最新版本。",
        "versions_unknown": "有的工具无法检查最新版本：GitHub 没有回应。",
        "alerts_down_month": "这个月实时提醒有 {duration}没有运行。",
        "alerts_ran_month": "这个月实时提醒一直在运行。",
        "login_problem_month": "{mailbox}：{kind}，本月 {count}。",
        "times_one": "1 次",
        "times": "{n} 次",
        "kind_auth": "服务器拒绝了登录",
        "kind_connect": "无法检查",
        "kind_reader": "新邮件无法分类",
    },
}


def lang(tag: str) -> str:
    """`zh` for a language tag that starts with `zh` (zh, zh-CN, zh_TW), else `en`."""
    return "zh" if tag.strip().lower().startswith("zh") else "en"


def t(key: str, language: str, **values: object) -> str:
    """The text `key` in the language of the tag `language` (`en` and `zh` are tags too), with
    `{name}` filled in from `values`. An unknown key is a KeyError: it is a bug, not an input."""
    return STRINGS[lang(language)][key].format(**values)
