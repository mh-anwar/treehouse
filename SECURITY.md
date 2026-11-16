# Security policy

treehouse runs as root on machines shared with other people, so security reports are very welcome.

**Please don't open a public issue for a vulnerability.** Use GitHub's
[private vulnerability reporting](../../security/advisories/new) instead. You'll get a reply within a
few days.

## In scope

- Privilege escalation from a friend account.
- One friend reading or affecting another's files or processes beyond the documented limits.
- Getting past Cloudflare Access or the gate's token check, or past Tailscale identity on the `/join` and `/me`
  pages.
- Anything that lets a request make root act on attacker-chosen paths (the spool and drop folders, the
  provisioner, the janitor, `box request`).
- Cross-site issues in the dashboard that let one person act as another.

## Out of scope

- Friends seeing each other's process list (documented).
- Denial of service by the box owner, or by a friend within their documented limits.
- Problems that need root on the box already.

## How access works

Public access goes only through Cloudflare Tunnel + Access. The gate verifies the Access token (signature,
audience, expiry) on every request, and root verifies it again before any account or portal action. There are
no public login sessions or device links of treehouse's own. A conf that still names the removed Tailscale Funnel
mode leaves the gate off. The private dashboard and tailnet self-join use `tailscale whois`, which root checks
again; self-join is either open or closed, with no approval queue.
