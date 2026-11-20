"""
Can this address receive e-mail? Checked before an account is created.

A mailbox can't be reliably verified without writing to it, so this checks
what can be: the syntax, then that the domain exists and accepts mail (MX
records, or an A/AAAA record as RFC 5321's implicit MX, and no "null MX").
A mailbox unknown to an existing domain is caught when the e-mail is sent
(mail_service.RecipientRefused).
"""

import logging
import socket

from core.forms.validators import EMAIL_RE
from core.services.errors import ServiceError

try:
    import dns.exception
    import dns.resolver
except ImportError:          # dnspython missing: fall back to an A lookup
    dns = None

log = logging.getLogger(__name__)

_LIFETIME_S = 6


class EmailCheckUnavailable(ServiceError):
    """DNS could not be queried: the answer is unknown."""


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().rstrip(".").encode("idna").decode("ascii")


def _has_address(domain: str) -> bool:
    try:
        return bool(socket.getaddrinfo(domain, None))
    except socket.gaierror:
        return False


def can_receive_mail(address: str) -> bool:
    """False for an invalid address or a domain that can't receive mail.
    Raises EmailCheckUnavailable if DNS can't be reached."""
    address = (address or "").strip()
    if not EMAIL_RE.match(address) or len(address) > 254:
        return False
    try:
        domain = _domain(address)
    except UnicodeError:
        return False

    if dns is None:
        return _has_address(domain)

    resolver = dns.resolver.Resolver()
    resolver.lifetime = _LIFETIME_S
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = [str(r.exchange).rstrip(".") for r in answers]
        return any(hosts)                      # "0 ." is a null MX: no mail
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        for rtype in ("A", "AAAA"):
            try:
                resolver.resolve(domain, rtype)
                return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except dns.exception.DNSException:
                break
        return False
    except dns.exception.DNSException as exc:
        log.warning("E-mail domain check failed for %s: %s", domain, exc)
        raise EmailCheckUnavailable("Impossible de vérifier l'adresse e-mail pour "
                                    "le moment. Réessayez dans un instant.") from exc
