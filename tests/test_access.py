from __future__ import annotations

from ipaddress import ip_address

import pytest

from vikuget.access import AccessPolicy, client_ip_from_x_forwarded_for


def test_allowlist_accepts_wildcard_addresses_and_cidrs() -> None:
    policy = AccessPolicy.parse("192.0.2.44, 198.51.100.0/24, 2001:db8:12::/48")

    assert policy.allows(ip_address("192.0.2.44"))
    assert policy.allows(ip_address("198.51.100.99"))
    assert policy.allows(ip_address("2001:db8:12::5"))
    assert not policy.allows(ip_address("192.0.2.45"))
    assert AccessPolicy.parse("*").allows(ip_address("203.0.113.1"))


def test_allowlist_rejects_invalid_wildcard_usage() -> None:
    with pytest.raises(ValueError, match=r"must be '\*' alone"):
        AccessPolicy.parse("*,192.0.2.44")


def test_client_ip_is_the_rightmost_traefik_forwarded_address() -> None:
    assert client_ip_from_x_forwarded_for("198.51.100.10, 203.0.113.44") == ip_address(
        "203.0.113.44"
    )
