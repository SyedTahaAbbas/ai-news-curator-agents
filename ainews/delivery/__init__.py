"""
Delivery: rendering the digest, then getting it somewhere.

Every piece here is an independent subscriber. `render` turns
CommentaryWritten into DigestRendered; `files` and `email` each subscribe to
DigestRendered and know nothing about each other; `commit` watches what they
report and decides when the run has actually delivered; `seen` acts on that
decision. Adding a channel - Slack, a webhook - means writing a handler,
subscribing it to DigestRendered, and telling the commit coordinator to expect
it. Nothing existing changes.
"""

from ainews.delivery.commit import register_commit
from ainews.delivery.email import register_email
from ainews.delivery.files import register_files
from ainews.delivery.journal import RunJournal, register_journal
from ainews.delivery.render import register_render
from ainews.delivery.seen import register_seen

__all__ = [
    "register_commit",
    "register_email",
    "register_files",
    "register_journal",
    "register_render",
    "register_seen",
    "RunJournal",
]
