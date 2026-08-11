from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True)
class AccessPolicy:
    """IP allowlist parsed from the ALLOWED_IPS environment value."""

    networks: tuple[IPNetwork, ...] | None

    @classmethod
    def parse(cls, value: str) -> AccessPolicy:
        raw_value = value.strip()
        if raw_value == "*":
            return cls(networks=None)
        if not raw_value:
            raise ValueError("ALLOWED_IPS must be '*' or a comma-separated list of IPs and CIDRs")

        values = [item.strip() for item in raw_value.split(",")]
        if any(not item for item in values) or "*" in values:
            raise ValueError(
                "ALLOWED_IPS must be '*' alone or a comma-separated list of IPs and CIDRs"
            )

        networks: list[IPNetwork] = []
        for item in values:
            try:
                address = ip_address(item)
            except ValueError:
                try:
                    networks.append(ip_network(item, strict=False))
                except ValueError as error:
                    raise ValueError(
                        f"Invalid IP address or CIDR in ALLOWED_IPS: {item}"
                    ) from error
            else:
                networks.append(ip_network(f"{address}/{address.max_prefixlen}"))
        return cls(networks=tuple(networks))

    def allows(self, address: IPAddress) -> bool:
        return self.networks is None or any(address in network for network in self.networks)


def client_ip_from_x_forwarded_for(value: str | None) -> IPAddress:
    """Take Traefik's appended client address from X-Forwarded-For.

    The vikuget container is on a private network shared only with Traefik. On its
    public entrypoint Traefik appends the connected client's remote address to this
    header, so the rightmost address is the source to authorize and record.
    """

    if value is None:
        raise ValueError("X-Forwarded-For is missing")
    parts = [item.strip() for item in value.split(",")]
    if not parts or not parts[-1]:
        raise ValueError("X-Forwarded-For is invalid")
    try:
        return ip_address(parts[-1])
    except ValueError as error:
        raise ValueError("X-Forwarded-For is invalid") from error
