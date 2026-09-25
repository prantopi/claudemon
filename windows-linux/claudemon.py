import os
import sys

if sys.version_info < (3, 9):
    sys.exit("claudemon requires Python 3.9 or newer (found %s)" % sys.version.split()[0])

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claudemon_lib import system

system.enable_dpi_awareness()  # before any Tk import side effects

from claudemon_lib.app import main

if __name__ == "__main__":
    main()
