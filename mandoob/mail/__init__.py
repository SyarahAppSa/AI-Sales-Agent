from __future__ import annotations

from ..models import MailAccount

_overrides: dict = {}


def get_provider(account: MailAccount):
    """يعيد مزود البريد للحساب المرتبط. يمكن للاختبارات استبداله عبر set_provider_override."""
    if account.id in _overrides:
        return _overrides[account.id]
    if account.provider == "microsoft365":
        from .graph import GraphProvider
        return GraphProvider(account.sender_address, account.reply_mailbox or None)
    if account.provider == "google":
        from .gmail import GmailProvider
        return GmailProvider(account.sender_address)
    if account.provider == "smtp":
        from .smtp import SMTPProvider
        return SMTPProvider(account.sender_address, account)
    from .sandbox import SandboxProvider
    return SandboxProvider(account.sender_address)


def set_provider_override(account_id: int, provider) -> None:
    _overrides[account_id] = provider
