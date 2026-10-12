"""Git credential helper: reads the username and token from a private file.

Declared through the `GIT_CONFIG_*` variables (see `credentials.git_environment`). Git calls it
with the action as last argument: `get` (answer), `store` or `erase` (ignored). No executable
file is created: `/tmp` is mounted `noexec` in the Docker container.

The token is only given to the repository host, over HTTPS. Git describes its request on
standard input (`protocol=…`, `host=…`) and asks again for each new host: a redirect from the
forge to another server would therefore receive nothing.

Usage (by Git): credential_helper.py <file> <host[:port]> get
"""

import json
import sys


def request(text: str) -> dict[str, str]:
    """`key=value` fields of a Git request (one per line, up to the empty line)."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            fields.setdefault(key, value)
    return fields


def main() -> int:
    path, host, action = sys.argv[1], sys.argv[2], sys.argv[-1]
    asked = request(sys.stdin.read())
    if action != "get":
        return 0
    if asked.get("protocol") != "https" or asked.get("host", "").lower() != host.lower():
        return 0  # other host or protocol: no answer, Git fails without the token

    with open(path, encoding="utf-8") as stream:
        values = json.load(stream)
    print(f"username={values['username']}")
    print(f"password={values['token']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
