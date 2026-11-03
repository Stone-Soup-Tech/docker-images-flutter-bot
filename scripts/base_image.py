#!/usr/bin/env python3
"""Record the current Flutter base digest; publishing bumps versions on changes."""
import json
import os
from pathlib import Path
import re
import sys


def record_digest(digest, path=Path('versions.yml')):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Expected a SHA256 image digest')
    configuration = json.loads(path.read_text())
    previous = configuration.get('flutter_base_digest')
    changed = previous != digest
    if changed:
        configuration['flutter_base_digest'] = digest
        path.write_text(json.dumps(configuration, indent=2) + '\n')
    return changed and previous is not None


if __name__ == '__main__':
    changed = record_digest(sys.argv[1])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'changed={str(changed).lower()}\n')
