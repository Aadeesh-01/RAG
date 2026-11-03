"""
Build the indexes for the demo repositories listed in demos.yaml.

    python scripts/build_demos.py                    # all demos -> data/demos/
    python scripts/build_demos.py --only requests    # one demo
    python scripts/build_demos.py --data-dir /srv/data --keep-clones

Remote demos are shallow-fetched at their pinned commit, indexed, and the
clone is deleted afterwards (the server only needs the saved index).
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from coderag.web.registry import build_demos  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", default=os.path.join(ROOT, "demos.yaml"))
    parser.add_argument("--data-dir", default=os.environ.get("CODERAG_DATA_DIR",
                                                             os.path.join(ROOT, "data")))
    parser.add_argument("--only", nargs="+", help="demo ids to build")
    parser.add_argument("--keep-clones", action="store_true")
    args = parser.parse_args(argv)

    build_demos(args.spec, args.data_dir, only=args.only, keep_clones=args.keep_clones)
    print(f"Demos built in {os.path.join(args.data_dir, 'demos')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
