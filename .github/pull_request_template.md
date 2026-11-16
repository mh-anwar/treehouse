<!-- One logical change per PR. See CONTRIBUTING.md. -->

What this changes and why:


How I tested it (which distro, and which of the tests in the README):

- [ ] `python3 -m unittest discover -s tests` and `python3 tests/e2e_gate.py .` pass
- [ ] Re-running `install.sh` on an existing box is still safe (no restarts of sshd, networking or the firewall)
- [ ] Docs updated if a command, setting or page changed
