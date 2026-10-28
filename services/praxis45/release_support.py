"""Generic release wrapper and host CLI resolution; no installer."""
import hashlib
import os
from pathlib import Path
import shutil
import sys

def sha(raw):return hashlib.sha256(raw).hexdigest()

def wrapper(release,module):
    return ('import sys\nsys.dont_write_bytecode=True\nsys.path.insert(0,'+repr(str(release/'code/services/praxis45'))+')\nfrom '+module+' import *\nif __name__=="__main__":\n    sys.exit(main())\n').encode()

def orca_cli(value=None):
    """Resolve the host CLI once; children use the recorded absolute executable."""
    name=(value or {}).get('orca_cli',{}).get('path') or os.environ.get('ORCA_CLI_COMMAND') or ('orca-dev' if os.environ.get('ORCA_DEV_REPO_ROOT') else 'orca-ide' if sys.platform.startswith('linux') else 'orca')
    selected=Path(name) if Path(name).is_absolute() else Path(shutil.which(name) or '')
    if not selected.is_absolute():raise ValueError('host Orca CLI not found')
    selected=selected.resolve()
    if not selected.is_file() or not os.access(selected,os.X_OK):raise ValueError('host Orca CLI is not executable')
    recorded=(value or {}).get('orca_cli',{})
    if recorded and sha(selected.read_bytes())!=recorded.get('sha256'):raise ValueError('host Orca CLI bytes drifted')
    return {'path':str(selected),'sha256':sha(selected.read_bytes())}
