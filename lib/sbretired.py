"""Remove files owned by retired features; never remove an unrecorded or changed decoy."""

import hashlib
import json
import os
import stat
import sys


DECOYS = {".env", "aws-credentials", "id_ed25519_deploy", "id_ed25519_deploy.pub",
          "db_dump_users.sql", "README.txt"}


def cleanup_canary(conf="/etc/sharedbox/sharedbox.conf", state="/var/lib/sharedbox"):
    # Read the retired setting directly: load_config deliberately ignores obsolete keys.
    directory = "/srv/backups/prod"
    try:
        with open(conf) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and key == "CANARY_DIR":
                    directory = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    manifest = os.path.join(state, "canary-planted.json")
    try:
        with open(manifest) as f:
            planted = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    if not isinstance(planted, dict):
        return
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        for name, digest in planted.items():
            if name not in DECOYS:
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            except FileNotFoundError:
                continue
            except OSError:
                continue  # a symlink is user data, not a planted regular file
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
                    continue
                if hashlib.sha256(f.read()).hexdigest() != digest:
                    continue
            current = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


def cleanup_janitor(state="/var/lib/sharedbox"):
    path = os.path.join(state, "janitor-state.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    if not isinstance(data, dict):
        return
    changed = False
    for rec in data.values():
        if isinstance(rec, dict):
            for key in list(rec):
                if key.startswith("t3_"):
                    del rec[key]
                    changed = True
    if changed:
        with open(path, "w") as f:
            json.dump(data, f)
            f.write("\n")


if __name__ == "__main__":
    cleanup_canary(*sys.argv[1:])
    cleanup_janitor(sys.argv[2] if len(sys.argv) > 2 else "/var/lib/sharedbox")
