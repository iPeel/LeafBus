import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'firmware'))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))

import mpshim  # noqa: E402

mpshim.install()
