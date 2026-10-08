#!/usr/bin/env python3
"""Research CLI adapter to the existing product image pipeline; no input fallback."""
import argparse
import sys

from pipelines.image.inject_reviewed_textures import main as product_main


def main() -> int:
    argv = sys.argv[1:]
    if "--help" not in argv and "-h" not in argv:
        if not any(arg == '--install-manifest' or arg.startswith('--install-manifest=') for arg in argv):
            argparse.ArgumentParser(description=__doc__).error('--install-manifest must be an explicit reviewed input; research adapters do not use private defaults')
        if not any(arg == '--original-root' or arg.startswith('--original-root=') for arg in argv):
            argparse.ArgumentParser(description=__doc__).error('--original-root must be an explicit reviewed input; research adapters do not use private defaults')
    return product_main()


if __name__ == "__main__":
    raise SystemExit(main())
