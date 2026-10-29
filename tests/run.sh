#!/bin/bash
# Run the whole Chronos test suite. Safe: uses throwaway temp homes and a fake `claude`.
#   tests/run.sh            the 0.2 suite (test_chronos.py) and the 0.3 API suites (test_api_*.py)
#   tests/run.sh --golden   regenerate tests/golden/api/*.json from the real CLI (commit the diff)
cd "$(dirname "$0")/.." || exit 1
if [ "${1:-}" = "--golden" ]; then exec python3 tests/golden.py; fi
rc=0
for f in tests/test_chronos.py tests/test_api_core.py tests/test_api_writer.py tests/test_api_approval.py tests/test_api_session.py tests/test_api_deadlines.py tests/test_api_golden.py tests/test_api_runs.py tests/test_api_tick.py tests/test_review_c1.py tests/test_review_c2.py; do
  python3 "$f" "$@" || rc=1
done
exit $rc
