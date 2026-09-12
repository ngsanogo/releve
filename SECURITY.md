# Security

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub security advisories](https://github.com/ngsanogo/releve/security/advisories/new)
rather than in a public issue. Include the version (`releve version`), what an
attacker could do, and how to reproduce it — with tokens redacted. Fixes ship in
a release together with the advisory.

## Running it safely

- **The gateway token** reads your meter's data: keep the configuration file
  private (`releve init` creates it `0600`) or pass it as `RELEVE_GATEWAY__TOKEN`.
- **The database** holds your consumption history. It is created `0600` in a
  `0700` directory.
- **The web interface** is read-only and listens on `127.0.0.1` by default. To
  reach it from elsewhere, set `web.auth_token` and put it behind a reverse proxy
  with TLS, a VPN or a tailnet.
- **Exporters** only talk to the destinations you enable; use `mqtt.tls`,
  `wss://` and `https://` across untrusted networks.

releve talks to the MyElectricalData gateway and to the exporters you enable,
nothing else — no telemetry.

## Supported versions

Security fixes go into the latest release.
