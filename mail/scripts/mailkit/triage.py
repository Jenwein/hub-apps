"""Sorting new mail with the reader, a batch at a time, and keeping every verdict.

`triage` takes mail by verdict key and UID. Verdicts already in the store are reused; the other
mails go in batches of BATCH mails of one mailbox and folder: one `fetch_raw` per batch (one login;
it never marks a mail as read), one reader call, up to WORKERS batches at once. A batch's verdicts
are stored as soon as it is done, by the calling thread only, so a run that ends early keeps what
it sorted. A batch that has not started when the budget is spent is left for the next run.

One mail must not keep a mailbox from being sorted (ruling R23). A batch the reader fails on is
sorted again mail by mail, each mail alone in its own call, and how a mail fails alone decides:

- The reader answered, but not with a verdict that fits the mail (a mail number missing or
  repeated, a tier that is not one of TIERS, no answer in the schema): that is about the mail,
  which gets the fallback verdict (tier daily, category other, reason FALLBACK_REASON), stored like
  any other, and reused like any other: the mail is not sent to the reader again. This
  holds when every mail of a batch fails so, too: what a mail makes the reader answer is up to
  its sender, and ten crafted mails must not hold a mailbox's alerts back for good.
- The reader did not answer (`ReaderDown`: claude missing, not logged in, an API error, no answer
  in time): that is about the reader, so it fails for every mail. The error ends the batch at once
  and `triage` raises it (normally at the first mail alone; when a batch timed out but its first
  mail alone gets an answer, the batch was the trouble and the others are tried as well). So a
  reader that fails for every mail of the batch never turns them into fallbacks: the caller keeps
  its old mark and tries again later.

A batch of one mail is its own retry. A retry starts only within the budget: past it, the batch's
mails are left, unless the batch failed with ReaderDown and no mail of it got an answer, which
raises. A mail the server no longer has (moved or deleted since) gets no verdict and is not left.

Any error (ReaderDown, or a HimalayaError of a fetch) stops new batches from starting; the
running ones finish and are stored, then the first error is raised. Call `triage` per mailbox to
keep one mailbox's failure from the others. Do not hold the state lock while calling it: storing
takes it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import mailtext, rules
from .context import Ctx
from .himalaya import Himalaya
from .mailtext import Parsed
from .reader import Reader, ReaderDown, ReaderError, Verdict, from_headers
from .settings import MailError
from .verdicts import VerdictStore

BATCH = 10
WORKERS = 3
FALLBACK_REASON = "could not be sorted"  # English: the report and the alerts say it in theirs

Item = tuple[str, str, str, int]  # verdict key, mailbox, folder, UID
Mail = tuple[str, str, Parsed]  # verdict key, mailbox, parsed: what the reader takes


@dataclass
class TriageResult:
    verdicts: list[Verdict]  # in the order of the items: reused, new and fallback verdicts
    left: list[str]  # keys of the mail not sorted in time, in the order of the items


@dataclass
class _Done:
    """What became of one batch."""

    verdicts: list[Verdict] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    error: MailError | None = None


def fallback(key: str, mailbox: str, parsed: Parsed) -> Verdict:
    """The verdict of a mail the reader could not sort alone: for the daily report, with the
    sender, subject and date from its headers and nothing from the reader."""
    sender, subject, when = from_headers(parsed)
    return Verdict(
        key=key, mailbox=mailbox, sender=sender, subject=subject, date=when, summary="",
        code=None, amount=None, deadline=None, tier="daily", category="other",
        reason=FALLBACK_REASON, phishing=False)


def _batches(items: list[Item]) -> list[list[Item]]:
    """The items in batches of BATCH of one mailbox and folder, in the order they came."""
    groups: dict[tuple[str, str], list[Item]] = {}
    for item in items:
        groups.setdefault((item[1], item[2]), []).append(item)
    return [
        group[at:at + BATCH] for group in groups.values() for at in range(0, len(group), BATCH)]


class _Sorter:
    """Sorts one batch at a time; runs on the pool's threads."""

    def __init__(self, hima: Himalaya, reader: Reader, rules_text: str, language: str,
                 out_of_time: Callable[[], bool]):
        self.hima = hima
        self.reader = reader
        self.rules_text = rules_text
        self.language = language
        self.out_of_time = out_of_time
        self.stop = threading.Event()  # set after an error: no new batch starts

    def _over(self) -> bool:
        return self.stop.is_set() or self.out_of_time()

    def batch(self, batch: list[Item]) -> _Done:
        """Fetch and sort `batch`. Errors of himalaya and the reader end up in `error`, with the
        verdicts found before them."""
        done = _Done()
        if self._over():
            done.left = [key for key, _, _, _ in batch]
            return done
        mailbox, folder = batch[0][1], batch[0][2]
        try:
            raws = self.hima.fetch_raw(mailbox, folder, [uid for _, _, _, uid in batch])
            mails = [
                (key, mailbox, mailtext.parse(raws[uid]))
                for key, _, _, uid in batch if uid in raws]
            self._sort(mails, done)
        except MailError as err:
            done.error = err
        return done

    def _sort(self, mails: list[Mail], done: _Done) -> None:
        if not mails:
            return
        try:
            done.verdicts += self.reader.triage(mails, self.rules_text, self.language)
            return
        except ReaderError as err:
            failed = err
        if len(mails) == 1:  # the call was the mail alone
            if isinstance(failed, ReaderDown):
                raise failed
            done.verdicts.append(fallback(*mails[0]))
            return
        for at, mail in enumerate(mails):
            if self._over():
                if at == 0 and isinstance(failed, ReaderDown):
                    raise failed  # no mail got an answer: nothing says the reader works
                done.left += [key for key, _, _ in mails[at:]]
                return
            try:
                done.verdicts += self.reader.triage([mail], self.rules_text, self.language)
            except ReaderDown:
                raise  # the reader's failure, not this mail's: it would fail for every mail
            except ReaderError:
                done.verdicts.append(fallback(*mail))


def triage(ctx: Ctx, hima: Himalaya, reader: Reader, items: list[Item], *, budget: float,
           monotonic: Callable[[], float] = time.monotonic) -> TriageResult:
    """The verdicts of `items` (verdict key, mailbox, folder, UID), sorted with the rules of
    `rules.combined` in the user's language; see the module's docstring."""
    start = monotonic()
    unique: dict[str, Item] = {}
    for item in items:
        unique.setdefault(item[0], item)
    store = VerdictStore(ctx)
    found = store.get_many(unique)
    todo = [item for key, item in unique.items() if key not in found]
    left: set[str] = set()
    if todo:
        sorter = _Sorter(
            hima, reader, rules.combined(ctx), ctx.settings.language,
            lambda: monotonic() - start >= budget)
        error: MailError | None = None
        with ThreadPoolExecutor(WORKERS) as pool:
            try:
                futures = [pool.submit(sorter.batch, batch) for batch in _batches(todo)]
                for future in as_completed(futures):
                    done = future.result()
                    store.put_many(done.verdicts)
                    found.update((v.key, v) for v in done.verdicts)
                    left.update(done.left)
                    if done.error is not None and error is None:
                        error = done.error
                        sorter.stop.set()
            finally:
                sorter.stop.set()  # whatever ends the loop, the batches still queued do not run
        if error is not None:
            raise error
    return TriageResult(
        [found[key] for key in unique if key in found], [key for key in unique if key in left])
