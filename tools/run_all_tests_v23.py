from __future__ import annotations

import logging

import run_all_tests_v22 as regression_suite


if __name__ == "__main__":
    try:
        raise SystemExit(regression_suite.main())
    finally:
        logging.shutdown()
        regression_suite.TEST_HOME.cleanup()
