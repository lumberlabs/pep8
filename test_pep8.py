#!/usr/bin/env python

import fnmatch
import os
import sys
import unittest

# Make sure we get our local pep8.py
pep8_path = os.path.abspath(os.path.dirname(__file__)) + os.sep
sys.path.insert(0, pep8_path)
from pep8 import Checker, readlines
sys.path.remove(pep8_path)
del(pep8_path)


def matches_any_pattern(full_filename, patterns):
    if not isinstance(patterns, (tuple, list)):
        patterns = [patterns]
    basename = os.path.basename(full_filename)
    for pattern in patterns:
        if fnmatch.fnmatch(basename, pattern):
            return True


def matching_filenames(directory, include_patterns=None, exclude_patterns=None):
    for root, dirs, files in os.walk(directory):

        dirs.sort()
        for subdir in dirs:
            if matches_any_pattern(subdir, exclude_patterns):
                dirs.remove(subdir)

        files.sort()
        for filename in files:
            whitelist_everything = not include_patterns
            whitelisted = True if whitelist_everything else matches_any_pattern(filename, include_patterns)
            if whitelisted:
                yield os.path.join(root, filename)


class TestSuite(unittest.TestCase):

    def setUp(self):
        pass


    def run_suite(self, filename):
        """
        Run all the tests from a file.

        A test file can provide many tests. Each test starts with a declaration.
        This declaration is a single line starting with '#:'.
        It declares codes of expected failures, separated by spaces or 'Okay'
        if no failure is expected.
        If the file does not contain such declaration, it should pass all tests.
        If the declaration is empty, following lines are not checked, until next
        declaration.

        Examples:

         * Only E224 and W701 are expected:         #: E224 W701
         * Following example is conform:            #: Okay
         * Don't check these lines:                 #:
        """
        lines = readlines(filename) + ['#:\n']
        line_offset = 0
        codes = ['Okay']
        testcase = []
        for index, line in enumerate(lines):
            if not line.startswith('#:'):
                if codes:
                    # Collect the lines of the test case
                    testcase.append(line)
                continue
            if codes and index > 0:
                label = '%s:%s:1' % (filename, line_offset + 1)
                codes = [c for c in codes if c != 'Okay']
                # Run the checker
                results = Checker(filename, lines=testcase).check_all()
                # Check if the expected errors were found
                for code in codes:
                    self.assertTrue(results.contains_error_with_code(code), 'Expected %s in %s at line %s' % (code, filename, line_offset + 1))
                extra_errors = results.errors_ignoring(frozenset(codes))
                if extra_errors:
                    for error in extra_errors:
                        self.fail("Unexpected %s in %s at line %s column %s" % (error.code, filename, error.location()[0], error.location()[1]))
            # output the real line numbers
            line_offset = index
            # configure the expected errors
            codes = line.split()[1:]
            # empty the test case buffer
            del testcase[:]

    def test_all_files(self):
        """
        Run all tests in the test suite.
        """
        test_suite_dir = "testsuite"
        full_test_suite_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), test_suite_dir)
        for matching_filename in matching_filenames(full_test_suite_dir, include_patterns="*.py"):
            self.run_suite(matching_filename)


if __name__ == '__main__':
    unittest.main()
