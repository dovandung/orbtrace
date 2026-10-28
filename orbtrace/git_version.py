import functools
import subprocess
import re

@functools.cache
def get_version():
    return subprocess.check_output('git describe --always --long --dirty', shell = True).decode('utf-8').strip()

def get_version_bcd():
    m = re.match(r'v(\d{1,2})\.(\d)(?:\.(\d+))?-', get_version())

    if not m:
        # Happens in clones/forks without the upstream release tags.
        # Fetch them with: git fetch --tags https://github.com/orbcode/orbtrace
        print(f'WARNING: git version {get_version()!r} has no vX.Y tag, using bcdDevice 0.00')
        return 0.0

    major, minor, patch = m.groups()

    return int(major) + int(minor) * 0.1 + int(patch or 0) * 0.01
