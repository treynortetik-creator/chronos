#!/bin/bash
# Run the whole Chronos test suite. Safe: uses throwaway temp homes and a fake `claude`.
cd "$(dirname "$0")/.." || exit 1
exec python3 tests/test_chronos.py "$@"
