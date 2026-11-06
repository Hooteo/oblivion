import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ps-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
