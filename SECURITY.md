# Security policy

sharedbox runs as root on machines shared with other people, so security reports are very welcome.

**Please don't open a public issue for a vulnerability.** Use GitHub's
[private vulnerability reporting](../../security/advisories/new) instead. You'll get a reply within a
few days.

In scope: privilege escalation from a friend account, one friend reading or affecting another's files
or processes beyond the documented limits, bypassing Tailscale identity on the join or `/me` pages,
and anything that lets a request make root act on attacker-chosen paths.

Out of scope: friends seeing each other's process list (documented), and denial of service by the box
owner.
