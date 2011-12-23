#!/usr/bin/env python

# pep8.py - Check and fix Python source code formatting, according to PEP 8
# Heavily modified by Josh Bleecher Snyder / Lumber Labs in 2011.
# Original license and header:

# pep8.py - Check Python source code formatting, according to PEP 8
# Copyright (C) 2006 Johann C. Rocholl <johann@rocholl.net>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Check Python source code formatting, according to PEP 8:
http://www.python.org/dev/peps/pep-0008/

For usage and a list of options, try this:
$ python pep8.py -h

This program and its regression test suite live here:
http://github.com/jcrocholl/pep8

Groups of errors and warnings:
E errors
W warnings
100 indentation
200 whitespace
300 blank lines
400 imports
500 line length
600 deprecation
700 statements

You can add checks to this program by writing plugins. Each plugin is
a simple function that is called for each line of source code, either
physical or logical.

Physical line:
- Raw line of text from the input file.

Logical line:
- Multi-line statements converted to a single line.
- Stripped left and right.
- Contents of strings replaced with 'xxx' of same length.
- Comments removed.

The check function requests physical or logical lines by the name of
the first argument:

def maximum_line_length(physical_line)
def extraneous_whitespace(logical_line)
def blank_lines(logical_line, blank_lines, indent_level, line_number)

The last example above demonstrates how check plugins can request
additional information with extra arguments. All attributes of the
Checker object are available. Some examples:

lines: a list of the raw lines from the input file
tokens: the tokens that contribute to this logical line
line_number: line number in the input file
blank_lines: blank lines before this one
indent_char: first indentation character in this file (' ' or '\t')
indent_level: indentation (with tabs expanded to multiples of 8)
previous_indent_level: indentation on previous line
previous_logical: previous logical line

The docstring of each check function shall be the relevant part of
text from PEP 8. It is printed if the user enables --show-pep8.
Several docstrings contain examples directly from the PEP 8 document.

Okay: spam(ham[1], {eggs: 2})
E201: spam( ham[1], {eggs: 2})

These examples are verified automatically when pep8.py is run with the
--doctest option. You can add examples for your own check functions.
The format is simple: "Okay" or error/warning code followed by colon
and space, the rest of the line is example source code. If you put 'r'
before the docstring, you can use \n for newline, \t for tab and \s
for space.

"""

__version__ = '0.5.1dev'

import inspect
import keyword
import os
import re
import sys
import textwrap
import time
import tokenize
from optparse import OptionParser
from fnmatch import fnmatch
try:
    frozenset
except NameError:
    from sets import ImmutableSet as frozenset

try:
    import cStringIO as StringIO
except ImportsError:
    import StringIO

DEFAULT_EXCLUDE = '.svn,CVS,.bzr,.hg,.git'
DEFAULT_IGNORE = 'E24'

INDENTATION_WHITESPACE = ' \t'
OPEN_PARENS = '([{'
CLOSE_PARENS = ')]}'

BINARY_OPERATORS = frozenset(['**=', '*=', '+=', '-=', '!=', '<>',
    '%=', '^=', '&=', '|=', '==', '/=', '//=', '<=', '>=', '<<=', '>>=',
    '%',  '^',  '&',  '|',  '=',  '/',  '//',  '<',  '>',  '<<'])
UNARY_OPERATORS = frozenset(['>>', '**', '*', '+', '-'])
OPERATORS = BINARY_OPERATORS | UNARY_OPERATORS
SKIP_TOKENS = frozenset([tokenize.COMMENT, tokenize.NL, tokenize.INDENT,
                         tokenize.DEDENT, tokenize.NEWLINE])

options = None
args = None


##############################################################################
# Metaclasses and registries for cleaners
##############################################################################

PHYSICAL_LINE_CHECKERS = set()
LOGICAL_LINE_CHECKERS = set()


class LineChecker(type):

    def __new__(metacls, name, bases, dictionary):
        # set up a generic __init__ that has kwargs
        if "__init__" not in dictionary:
            def default_init(self, **kwargs):
                pass
            dictionary["__init__"] = default_init

        return super(LineChecker, metacls).__new__(metacls, name, bases, dictionary)

    def __init__(cls, name, bases, dictionary):
        # registration
        cls.registry.add(cls)


class PhysicalLineChecker(LineChecker):
    registry = PHYSICAL_LINE_CHECKERS


class LogicalLineChecker(LineChecker):
    registry = LOGICAL_LINE_CHECKERS


##############################################################################
# Line data structure
##############################################################################

class PhysicalLine(object):

    def __init__(self, physical_line, line_number=None):
        self.physical_line = physical_line
        self.line_number = line_number

    def original_location_for_column(self, column):
        if column is None:
            return self.line_number, 0
        return self.line_number, column


class LogicalLine(object):

    def __init__(self, logical_line,
                 line_number=None,
                 tokens=None, autotokenize=None,
                 blank_lines=None, blank_lines_before_comment=None,
                 token_offset_mapping=None):
        """
        >>> line = LogicalLine("    abc")
        >>> line.dedented_line
        'abc'
        >>> line.indent_level
        4
        """
        self.logical_line = logical_line
        self.tokens = tokens
        self.blank_lines = blank_lines
        self.blank_lines_before_comment = blank_lines_before_comment
        indentation = leading_indentation(logical_line)
        self.indent_level = indentation_level(indentation)
        self.dedented_line = self.logical_line[len(indentation):]
        self.line_number = line_number
        self.token_offset_mapping = token_offset_mapping

        if autotokenize:
            line_io = StringIO.StringIO(self.logical_line)
            self.tokens = tokenize.generate_tokens(line_io.readline)

    def original_location_for_column(self, column):
        if column is None:
            return self.line_number, 0
        if isinstance(column, tuple):
            for token_offset, token in self.token_offset_mapping:
                if column >= token_offset:
                    token_start_row, token_start_col = token[2]
                    original_line_number = token_start_row
                    original_column = token_start_col + column[1] - token_offset
            return original_line_number, original_column
        if isinstance(column, int):
            return self.line_number, column
        raise TypeError()


def most_common_indent_char(list_of_strings, indent_chars=INDENTATION_WHITESPACE):
    r"""
    Determine which of a set of indentation characters occurs most in a list of lines.
    Behavior is undetermined if there is a tie.

    >>> most_common_indent_char([" a", " b", " c"])
    ' '
    >>> most_common_indent_char([" a", " b", "\tc"])
    ' '
    >>> most_common_indent_char([" a", "\tb", "\tc"])
    '\t'
    >>> most_common_indent_char([]) in INDENTATION_WHITESPACE  # tie
    True
    >>> most_common_indent_char(["  a", "\tb", "\tc"]) in INDENTATION_WHITESPACE  # tie
    True
    >>> most_common_indent_char([" a", " b", "cccc"], indent_chars=" c")
    'c'
    """
    # quick heuristic to determine whether the file is indented with spaces or tabs
    # not the most efficient, but simple: extract all indentation characters,
    # and pick the most commonly occurring one.
    all_indentation = "".join(leading_indentation(line, indent_chars=indent_chars) for line in list_of_strings)
    most_common_indent_char = max((all_indentation.count(indent_char), indent_char) for indent_char in indent_chars)[1]
    return most_common_indent_char


def most_common_line_ending(list_of_strings):
    r"""
    Determine which line ending occurs most in a list of lines.
    Behavior is undetermined if there is a tie.

    >>> most_common_line_ending(["a\n", "a\n", "a\n"])
    '\n'
    >>> most_common_line_ending(["a\n", "b\n", "c\r\n"])
    '\n'
    >>> most_common_line_ending(["a\n", "b\r\n", "c\r\n"])
    '\r\n'
    >>> most_common_line_ending([])  # tie
    >>> most_common_line_ending(["a\n", "b\r\n"]) in ("\r\n", "\n")  # tie
    True
    """
    if len(list_of_strings) == 0:
        return None
    all_endings = [line_ending(s) for s in list_of_strings]
    most_common_line_ending = max((all_endings.count(ending), ending) for ending in set(all_endings))[1]
    return most_common_line_ending


class Document(object):

    def __init__(self, lines):
        self.lines = lines
        self.num_lines = len(lines)
        self.indent_char = most_common_indent_char(self.lines)
        self.line_ending = most_common_line_ending(self.lines)
        self.line_number = 0

    def readline(self):
        try:
            line = self.lines[self.line_number]
        except IndexError:
            line = ""
        self.line_number += 1
        return line


##############################################################################
# Errors and warnings
##############################################################################


class CheckerError(object):
    "A class that encapsulates checker errors."

    ERROR_TEXT = {"E251": "no spaces around keyword / parameter equals",
                  "E262": "inline comment should start with '# '",
                  "E101": "indentation contains mixed spaces and tabs",
                  "W191": "indentation contains tabs",
                  "W291": "trailing whitespace",
                  "W293": "blank line contains whitespace",
                  "W391": "blank line at end of file",
                  "W292": "no newline at end of file",
                  "E261": "at least two spaces before inline comment",
                  "E401": "multiple imports on one line",
                  "E701": "multiple statements on one line (colon)",
                  "E702": "multiple statements on one line (semicolon)",
                  "W601": ".has_key() is deprecated, use 'in'",
                  "W602": "deprecated form of raising exception",
                  "W603": "'<>' is deprecated, use '!='",
                  "W604": "backticks are deprecated, use 'repr()'",
                  "E225": "missing whitespace around operator",
                  "E111": "indentation is not a multiple of four",
                  "E112": "expected an indented block",
                  "E113": "unexpected indentation",
                  "E304": "blank lines found after function decorator",
                  "E301": "expected 1 blank line, found 0",
                  "E501": "line too long (%(line_length)d characters)",
                  "E241": "multiple spaces after %(separator)r",
                  "E242": "tab after %(separator)r",
                  "E211": "whitespace before %(char)r",
                  "E231": "missing whitespace after %r",
                  "E201": "whitespace after %(char)r",
                  "E202": "whitespace before %(char)r",
                  "E203": "whitespace before %(char)r",
                  "E303": "too many blank lines (%(blank_lines)d)",
                  "E302": "expected 2 blank lines, found %(blank_lines)d",
                  "E223": "tab before operator",
                  "E221": "multiple spaces before operator",
                  "E224": "tab after operator",
                  "E222": "multiple spaces after operator",
                 }

    line = None

    def __init__(self, code, column=None, **context):
        self.code = code
        self.column = column
        self.context = context

    @property
    def description(self):
        return "%s %s" % (self.code, self.text)

    @property
    def text_format(self):
        return self.ERROR_TEXT[self.code]

    @property
    def text(self):
        return self.text_format % self.context

    def __repr__(self):
        # This is a lame __repr__ implementation, but it's kept simple and
        # minimal so that doctests are easy to write and read.
        if self.column is not None:
            return "%s: %s" % (self.code, self.column)
        else:
            return self.code

    def location(self):
        return self.line.original_location_for_column(self.column)


##############################################################################
# Plugins (checker classes) for physical lines
##############################################################################

class TabsOrSpaces(object):
    r"""
    Never mix tabs and spaces.

    The most popular way of indenting Python is with spaces only. The
    second-most popular way is with tabs only. Code indented with a mixture
    of tabs and spaces should be converted to using spaces exclusively. When
    invoking the Python command line interpreter with the -t option, it issues
    warnings about code that illegally mixes tabs and spaces. When using -tt
    these warnings become errors. These options are highly recommended!

    Okay: if a == 0:\n        a = 1\n        b = 1
    E101: if a == 0:\n        a = 1\n\tb = 1

    This checker does not provide autofixes, because it is not possible
    to confidently disambiguate some tab depths...and reindent.py
    already does a good job of handling the reasonable cases.
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = TabsOrSpaces()
        >>> space_indented_document = Document([" "])
        >>> tab_indented_document = Document(["\t"])
        >>> checker.find_error(PhysicalLine('if a == 0:'), document=space_indented_document)
        >>> checker.find_error(PhysicalLine('        a = 1'), document=space_indented_document)
        >>> checker.find_error(PhysicalLine('\ta = 1'), document=space_indented_document)
        E101: 0
        >>> checker.find_error(PhysicalLine('        \ta = 1'), document=space_indented_document)
        E101: 8
        >>> checker.find_error(PhysicalLine('\t        a = 1'), document=space_indented_document)
        E101: 0
        >>> checker.find_error(PhysicalLine('        a = 1'), document=tab_indented_document)
        E101: 0
        >>> checker.find_error(PhysicalLine('\ta = 1'), document=tab_indented_document)
        >>> checker.find_error(PhysicalLine('        \ta = 1'), document=tab_indented_document)
        E101: 0
        >>> checker.find_error(PhysicalLine('\t        a = 1'), document=tab_indented_document)
        E101: 1
        """
        indent = leading_indentation(line.physical_line)
        for offset, char in enumerate(indent):
            if char != document.indent_char:
                return CheckerError("E101", offset)


class TabsObsolete(object):
    r"""
    For new projects, spaces-only are strongly recommended over tabs. Most
    editors have features that make this easy to do.

    Okay: if True:\n    return
    W191: if True:\n\treturn

    Not autofixable right now. Reason: Unless the whole file uses tabs,
    ambiguities are possible. And reindent.py does a pretty good job of this already.
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = TabsObsolete()
        >>> checker.find_error(PhysicalLine('a == 0'))
        >>> checker.find_error(PhysicalLine(' a = 0'))
        >>> checker.find_error(PhysicalLine('  a = 0'))
        >>> checker.find_error(PhysicalLine('   a = 0'))
        >>> checker.find_error(PhysicalLine('\ta = 0'))
        W191: 0
        >>> checker.find_error(PhysicalLine('\t\ta = 0'))
        W191: 0
        >>> checker.find_error(PhysicalLine(' \t\ta = 0'))
        W191: 1
        >>> checker.find_error(PhysicalLine('    \t\ta = 0'))
        W191: 4
        """
        indent = leading_indentation(line.physical_line)
        try:
            column = indent.index('\t')
            return CheckerError("W191", column)
        except ValueError:
            pass


def rstrip_newlines(s):
    r"""
    Removes newline characters from the end of a line.

    Newline characters include:
        \\n: chr(10), newline
        \\r: chr(13), carriage return
        \\x0c: chr(12), form feed (^L)

    >>> rstrip_newlines("abc")
    'abc'
    >>> rstrip_newlines("")
    ''
    >>> rstrip_newlines("abc\n")
    'abc'
    >>> rstrip_newlines("abc\r")
    'abc'
    >>> rstrip_newlines("abc\x0c")
    'abc'
    >>> rstrip_newlines("abc\r\x0c\n")
    'abc'
    """
    return s.rstrip('\n\r\x0c')


class TrailingWhitespace(object):
    r"""
    JCR: Trailing whitespace is superfluous.
    FBM: Except when it occurs as part of a blank line (i.e. the line is
         nothing but whitespace). According to Python docs[1] a line with only
         whitespace is considered a blank line, and is to be ignored. However,
         matching a blank line to its indentation level avoids mistakenly
         terminating a multi-line statement (e.g. class declaration) when
         pasting code into the standard Python interpreter.

         [1] http://docs.python.org/reference/lexical_analysis.html#blank-lines

    The warning returned varies on whether the line itself is blank, for easier
    filtering for those who want to indent their blank lines.

    Okay: spam(1)
    W291: spam(1)\s
    W293: class Foo(object):\n    \n    bang = 12
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = TrailingWhitespace()
        >>> checker.find_error(PhysicalLine('spam(1)'))
        >>> checker.find_error(PhysicalLine('spam(1) '))
        W291: 7
        >>> checker.find_error(PhysicalLine('spam(1)  '))
        W291: 7
        >>> checker.find_error(PhysicalLine(' spam(1) '))
        W291: 8
        >>> checker.find_error(PhysicalLine('spam(1)\t'))
        W291: 7
        >>> checker.find_error(PhysicalLine('   '))
        W293
        >>> checker.find_error(PhysicalLine('\t '))
        W293
        >>> checker.find_error(PhysicalLine(' \t '))
        W293
        """
        without_newlines = rstrip_newlines(line.physical_line)
        without_spaces = without_newlines.rstrip()
        if without_newlines != without_spaces and not without_spaces:
            return CheckerError("W293")
        if without_newlines != without_spaces:
            column = len(without_spaces)
            return CheckerError("W291", column)

    def autofix(self, line, previous_line=None, document=None):
        """
        >>> checker = TrailingWhitespace()
        >>> checker.autofix(PhysicalLine('spam(1)'))
        'spam(1)'
        >>> checker.autofix(PhysicalLine('spam(1) '))
        'spam(1)'
        >>> checker.autofix(PhysicalLine('spam(1)  '))
        'spam(1)'
        >>> checker.autofix(PhysicalLine(' spam(1) '))
        ' spam(1)'
        >>> checker.autofix(PhysicalLine('spam(1)\t'))
        'spam(1)'
        >>> checker.autofix(PhysicalLine('   '))
        ''
        >>> checker.autofix(PhysicalLine('\t '))
        ''
        >>> checker.autofix(PhysicalLine(' \t '))
        ''
        """
        return line.physical_line.rstrip() + line_ending(line.physical_line)


class TrailingBlankLines(object):
    r"""
    JCR: Trailing blank lines are superfluous.

    Okay: spam(1)
    W391: spam(1)\n
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = TrailingBlankLines()
        >>> one_line_document = Document([""])
        >>> two_line_document = Document(["", ""])
        >>> checker.find_error(PhysicalLine('a == 0', line_number=1), document=one_line_document)
        >>> checker.find_error(PhysicalLine('', line_number=1), document=one_line_document)
        W391
        >>> checker.find_error(PhysicalLine('', line_number=1), document=two_line_document)
        >>> checker.find_error(PhysicalLine('a == 0', line_number=1), document=one_line_document)
        """
        if line.physical_line.strip() == '' and line.line_number == document.num_lines:
            return CheckerError("W391")


class MissingNewline(object):
    """
    JCR: The last line should have a newline.
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = MissingNewline()
        >>> checker.find_error(PhysicalLine(''))
        W292: 0
        >>> checker.find_error(PhysicalLine('\n'))
        >>> checker.find_error(PhysicalLine('abc'))
        W292: 3
        >>> checker.find_error(PhysicalLine('abc\n'))
        """
        if line.physical_line.rstrip() == line.physical_line:
            column = len(line.physical_line)
            return CheckerError("W292", column)

    def autofix(self, line, previous_line=None, document=None):
        error = self.find_error(line, previous_line=previous_line, document=document)
        if error is not None:
            return line.physical_line + document.line_ending
        else:
            return line.physical_line


class MaximumLineLength(object):
    """
    Limit all lines to a maximum of 79 characters.
    
    There are still many devices around that are limited to 80 character
    lines; plus, limiting windows to 80 characters makes it possible to have
    several windows side-by-side. The default wrapping on such devices looks
    ugly. Therefore, please limit all lines to a maximum of 79 characters.
    For flowing long blocks of text (docstrings or comments), limiting the
    length to 72 characters is recommended.
    """

    # TODO: Implement a check for the recommended line length limits
    # for "flowing long blocks of text".

    __metaclass__ = PhysicalLineChecker

    DEFAULT_MAX_LINE_LENGTH = 79

    def __init__(self, max_line_length=DEFAULT_MAX_LINE_LENGTH, **kwargs):
        self.max_line_length = max_line_length

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = MaximumLineLength()
        >>> checker.find_error(PhysicalLine('a' * 80))
        E501: 79
        >>> checker.find_error(PhysicalLine('a' * 79))
        >>> checker.find_error(PhysicalLine('a' * 200))
        E501: 79
        >>> checker.find_error(PhysicalLine(''))
        >>> checker3 = MaximumLineLength(max_line_length=3)
        >>> checker3.find_error(PhysicalLine("123"))
        >>> checker3.find_error(PhysicalLine("1234"))
        E501: 3
        """
        stripped = line.physical_line.rstrip()
        length = len(stripped)
        if length > self.max_line_length:
            try:
                # The line could contain multi-byte characters
                if not hasattr(stripped, 'decode'):   # Python 3
                    encoded = stripped.encode('latin-1')
                else:
                    encoded = stripped
                length = len(encoded.decode('utf-8'))
            except UnicodeDecodeError:
                pass
        if length > self.max_line_length:
            return CheckerError("E501", column=self.max_line_length, line_length=length)


##############################################################################
# Plugins (check functions) for logical lines
##############################################################################


class BlankLines(object):
    r"""
    Separate top-level function and class definitions with two blank lines.

    Method definitions inside a class are separated by a single blank line.

    Extra blank lines may be used (sparingly) to separate groups of related
    functions. Blank lines may be omitted between a bunch of related
    one-liners (e.g. a set of dummy implementations).

    Use blank lines in functions, sparingly, to indicate logical sections.

    Okay: def a():\n    pass\n\n\ndef b():\n    pass
    Okay: def a():\n    pass\n\n\n# Foo\n# Bar\n\ndef b():\n    pass

    E301: class Foo:\n    b = 0\n    def bar():\n        pass
    E302: def a():\n    pass\n\ndef b(n):\n    pass
    E303: def a():\n    pass\n\n\n\ndef b(n):\n    pass
    E303: def a():\n\n\n\n    pass
    E304: @decorator\n\ndef a():\n    pass
    """

    __metaclass__ = LogicalLineChecker

    DOCSTRING_REGEX = re.compile(r'u?r?["\']')

    def find_error(self, line, previous_line=None, document=None):
        r"""
        # >>> checker = BlankLines()
        TODO
        """
        if line.line_number == 1 or not previous_line:
            return  # Don't expect blank lines before the first line
        max_blank_lines = max(line.blank_lines, line.blank_lines_before_comment)
        if previous_line.dedented_line.startswith('@'):
            if max_blank_lines:
                return CheckerError("E304")
        elif max_blank_lines > 2 or (line.indent_level and max_blank_lines == 2):
            return CheckerError("E303", blank_lines=max_blank_lines)
        elif (line.dedented_line.startswith('def ') or
              line.dedented_line.startswith('class ') or
              line.dedented_line.startswith('@')):
            if line.indent_level:
                if not (max_blank_lines or previous_line.indent_level < line.indent_level or
                        self.DOCSTRING_REGEX.match(previous_line.logical_line)):
                    return CheckerError("E301")
            elif max_blank_lines != 2:
                return CheckerError("E302", blank_lines=max_blank_lines)


class ExtraneousWhitespace(object):
    """
    Avoid extraneous whitespace in the following situations:

    - Immediately inside parentheses, brackets or braces.

    - Immediately before a comma, semicolon, or colon.

    Okay: spam(ham[1], {eggs: 2})
    E201: spam( ham[1], {eggs: 2})
    E201: spam(ham[ 1], {eggs: 2})
    E201: spam(ham[1], { eggs: 2})
    E202: spam(ham[1], {eggs: 2} )
    E202: spam(ham[1 ], {eggs: 2})
    E202: spam(ham[1], {eggs: 2 })

    E203: if x == 4: print x, y; x, y = y , x
    E203: if x == 4: print x, y ; x, y = y, x
    E203: if x == 4 : print x, y; x, y = y, x
    """

    __metaclass__ = LogicalLineChecker

    EXTRANEOUS_WHITESPACE_REGEX = re.compile(r'[[({] | []}),;:]')

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = ExtraneousWhitespace()
        >>> checker.find_error(LogicalLine('spam(ham[1], {eggs: 2})'))
        >>> checker.find_error(LogicalLine('spam( ham[1], {eggs: 2})'))
        E201: 5
        >>> checker.find_error(LogicalLine('spam(ham[ 1], {eggs: 2})'))
        E201: 9
        >>> checker.find_error(LogicalLine('spam(ham[1], { eggs: 2})'))
        E201: 14
        >>> checker.find_error(LogicalLine('spam(ham[1], {eggs: 2} )'))
        E202: 22
        >>> checker.find_error(LogicalLine('spam(ham[1 ], {eggs: 2})'))
        E202: 10
        >>> checker.find_error(LogicalLine('spam(ham[1], {eggs: 2 })'))
        E202: 21
        >>> checker.find_error(LogicalLine('if x == 4: print x, y; x, y = y , x'))
        E203: 31
        >>> checker.find_error(LogicalLine('if x == 4: print x, y ; x, y = y, x'))
        E203: 21
        >>> checker.find_error(LogicalLine('if x == 4 : print x, y; x, y = y, x'))
        E203: 9
        """
        line = line.logical_line
        for match in self.EXTRANEOUS_WHITESPACE_REGEX.finditer(line):
            text = match.group()
            char = text.strip()
            found = match.start()
            if text == char + ' ' and char in OPEN_PARENS:
                return CheckerError("E201", column=found + 1, char=char)
            if text == ' ' + char and line[found - 1] != ',':
                if char in CLOSE_PARENS:
                    return CheckerError("E202", column=found, char=char)
                if char in ',;:':
                    return CheckerError("E203", column=found, char=char)


class MissingWhitespaceAfterSeparator(object):
    """
    JCR: Each comma, semicolon or colon should be followed by whitespace.

    Okay: [a, b]
    Okay: (3,)
    Okay: a[1:4]
    Okay: a[:4]
    Okay: a[1:]
    Okay: a[1:4:2]
    E231: ['a','b']
    E231: foo(bar,baz)
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = MissingWhitespaceAfterSeparator()
        >>> checker.find_error(LogicalLine('[a, b]'))
        >>> checker.find_error(LogicalLine('(3,)'))
        >>> checker.find_error(LogicalLine('a[1:4]'))
        >>> checker.find_error(LogicalLine('a[:4]'))
        >>> checker.find_error(LogicalLine('a[1:]'))
        >>> checker.find_error(LogicalLine('a[1:4:2]'))
        >>> checker.find_error(LogicalLine('["a","b"]'))
        E231: 4
        >>> checker.find_error(LogicalLine('foo(bar,baz)'))
        E231: 7
        """
        line = line.logical_line
        for index in range(len(line) - 1):
            char = line[index]
            if char in ',;:' and line[index + 1] not in INDENTATION_WHITESPACE:
                before = line[:index]
                if char == ':' and before.count('[') > before.count(']'):
                    continue  # Slice syntax, no space required
                if char == ',' and line[index + 1] == ')':
                    continue  # Allow tuple with only one element: (3,)
                return CheckerError("E231", column=index, char=char)


class Indentation(object):
    r"""
    Use 4 spaces per indentation level.

    For really old code that you don't want to mess up, you can continue to
    use 8-space tabs.

    Okay: a = 1
    Okay: if a == 0:\n    a = 1
    E111:   a = 1

    Okay: for item in items:\n    pass
    E112: for item in items:\npass

    Okay: a = 1\nb = 2
    E113: a = 1\n    b = 2
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = Indentation()
        >>> space_indented_document = Document([" "])
        >>> checker.find_error(LogicalLine('a = 1'), previous_line=LogicalLine(''), document=space_indented_document)
        >>> checker.find_error(LogicalLine('if a == 0:'), previous_line=LogicalLine('    a = 1'), document=space_indented_document)
        >>> checker.find_error(LogicalLine('  a = 1'), previous_line=LogicalLine(''), document=space_indented_document)
        E111
        >>> checker.find_error(LogicalLine('    pass'), previous_line=LogicalLine('for item in items:'), document=space_indented_document)
        >>> checker.find_error(LogicalLine('pass'), previous_line=LogicalLine('for item in items:'), document=space_indented_document)
        E112
        >>> checker.find_error(LogicalLine('b = 2'), previous_line=LogicalLine('a = 1'), document=space_indented_document)
        >>> checker.find_error(LogicalLine('    b = 2'), previous_line=LogicalLine('a = 1'), document=space_indented_document)
        E113
        """
        if document.indent_char == ' ' and line.indent_level % 4:
            return CheckerError("E111")
        if not previous_line:
            return
        indent_expect = previous_line.logical_line.endswith(':')
        if indent_expect and line.indent_level <= previous_line.indent_level:
            return CheckerError("E112")
        if line.indent_level > previous_line.indent_level and not indent_expect:
            return CheckerError("E113")


class WhitespaceBeforeParameters(object):
    """
    Avoid extraneous whitespace in the following situations:

    - Immediately before the open parenthesis that starts the argument
      list of a function call.

    - Immediately before the open parenthesis that starts an indexing or
      slicing.

    Okay: spam(1)
    E211: spam (1)

    Okay: dict['key'] = list[index]
    E211: dict ['key'] = list[index]
    E211: dict['key'] = list [index]
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = WhitespaceBeforeParameters()
        >>> checker.find_error(LogicalLine('spam(1)', autotokenize=True))
        >>> checker.find_error(LogicalLine('spam (1)', autotokenize=True))
        E211: (1, 4)
        >>> checker.find_error(LogicalLine('dict["key"] = list[index]', autotokenize=True))
        >>> checker.find_error(LogicalLine('dict ["key"] = list[index]', autotokenize=True))
        E211: (1, 4)
        >>> checker.find_error(LogicalLine('dict["key"] = list [index]', autotokenize=True))
        E211: (1, 18)
        """
        tokens = list(line.tokens)
        prev_type = tokens[0][0]
        prev_text = tokens[0][1]
        prev_end = tokens[0][3]
        for index in range(1, len(tokens)):
            token_type, text, start, end, line = tokens[index]
            if (token_type == tokenize.OP and
                text in '([' and
                start != prev_end and
                (prev_type == tokenize.NAME or prev_text in CLOSE_PARENS) and
                # Syntax "class A (B):" is allowed, but avoid it
                (index < 2 or tokens[index - 2][1] != 'class') and
                # Allow "return (a.foo for a in range(5))"
                (not keyword.iskeyword(prev_text))):
                return CheckerError("E211", column=prev_end, char=text)
            prev_type = token_type
            prev_text = text
            prev_end = end


class WhitespaceAroundOperator(object):
    """
    Avoid extraneous whitespace in the following situations:

    - More than one space around an assignment (or other) operator to
      align it with another.

    Okay: a = 12 + 3
    E221: a = 4  + 5
    E222: a = 4 +  5
    E223: a = 4\t+ 5
    E224: a = 4 +\t5
    """

    __metaclass__ = LogicalLineChecker

    WHITESPACE_AROUND_OPERATOR_REGEX = re.compile('([^\w\s]*)\s*(\t|  )\s*([^\w\s]*)')

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = WhitespaceAroundOperator()
        >>> checker.find_error(LogicalLine('a = 12 + 3'))
        >>> checker.find_error(LogicalLine('a = 4  + 5'))
        E221: 5
        >>> "multiple spaces" in checker.find_error(LogicalLine('a = 4  + 5')).text
        True
        >>> "before" in checker.find_error(LogicalLine('a = 4  + 5')).text
        True
        >>> checker.find_error(LogicalLine('a = 4 +  5'))
        E222: 7
        >>> "after" in checker.find_error(LogicalLine('a = 4 +  5')).text
        True
        >>> "multiple spaces" in checker.find_error(LogicalLine('a = 4 +  5')).text
        True
        >>> checker.find_error(LogicalLine('a = 4\t+ 5'))
        E223: 5
        >>> "before" in checker.find_error(LogicalLine('a = 4\t+ 5')).text
        True
        >>> "tab" in checker.find_error(LogicalLine('a = 4\t+ 5')).text
        True
        >>> checker.find_error(LogicalLine('a = 4 +\t5'))
        E224: 7
        >>> "tab" in checker.find_error(LogicalLine('a = 4 +\t5')).text
        True
        >>> "after" in checker.find_error(LogicalLine('a = 4 +\t5')).text
        True
        """
        for match in self.WHITESPACE_AROUND_OPERATOR_REGEX.finditer(line.logical_line):
            before, whitespace, after = match.groups()
            tab = "\t" in whitespace
            offset = match.start(2)
            if before in OPERATORS:
                code = "E224" if tab else "E222"
                return CheckerError(code, column=offset)
            elif after in OPERATORS:
                code = "E223" if tab else "E221"
                return CheckerError(code, column=offset)


class MissingWhitespaceAroundOperator(object):
    r"""
    - Always surround these binary operators with a single space on
      either side: assignment (=), augmented assignment (+=, -= etc.),
      comparisons (==, <, >, !=, <>, <=, >=, in, not in, is, is not),
      Booleans (and, or, not).

    - Use spaces around arithmetic operators.

    Okay: i = i + 1
    Okay: submitted += 1
    Okay: x = x * 2 - 1
    Okay: hypot2 = x * x + y * y
    Okay: c = (a + b) * (a - b)
    Okay: foo(bar, key='word', *args, **kwargs)
    Okay: baz(**kwargs)
    Okay: negative = -1
    Okay: spam(-1)
    Okay: alpha[:-i]
    Okay: if not -5 < x < +5:\n    pass
    Okay: lambda *args, **kw: (args, kw)

    E225: i=i+1
    E225: submitted +=1
    E225: x = x*2 - 1
    E225: hypot2 = x*x + y*y
    E225: c = (a+b) * (a-b)
    E225: c = alpha -4
    E225: z = x **y
    """

    __metaclass__ = LogicalLineChecker

    E225NOT_KEYWORDS = (frozenset(keyword.kwlist + ['print']) -
                        frozenset(['False', 'None', 'True']))

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = MissingWhitespaceAroundOperator()
        >>> checker.find_error(LogicalLine('i = i + 1', autotokenize=True))
        >>> checker.find_error(LogicalLine('submitted += 1', autotokenize=True))
        >>> checker.find_error(LogicalLine('x = x * 2 - 1', autotokenize=True))
        >>> checker.find_error(LogicalLine('hypot2 = x * x + y * y', autotokenize=True))
        >>> checker.find_error(LogicalLine('c = (a + b) * (a - b)', autotokenize=True))
        >>> checker.find_error(LogicalLine('foo(bar, key="word", *args, **kwargs)', autotokenize=True))
        >>> checker.find_error(LogicalLine('baz(**kwargs)', autotokenize=True))
        >>> checker.find_error(LogicalLine('negative = -1', autotokenize=True))
        >>> checker.find_error(LogicalLine('spam(-1)', autotokenize=True))
        >>> checker.find_error(LogicalLine('alpha[:-i]', autotokenize=True))
        >>> checker.find_error(LogicalLine('if not -5 < x < +5:\n    pass', autotokenize=True))
        >>> checker.find_error(LogicalLine('lambda *args, **kw: (args, kw)', autotokenize=True))
        >>> checker.find_error(LogicalLine('i=i+1', autotokenize=True))
        E225: (1, 1)
        >>> checker.find_error(LogicalLine('submitted +=1', autotokenize=True))
        E225: (1, 12)
        >>> checker.find_error(LogicalLine('x = x*2 - 1', autotokenize=True))
        E225: (1, 5)
        >>> checker.find_error(LogicalLine('hypot2 = x*x + y*y', autotokenize=True))
        E225: (1, 10)
        >>> checker.find_error(LogicalLine('c = (a+b) * (a-b)', autotokenize=True))
        E225: (1, 6)
        >>> checker.find_error(LogicalLine('c = alpha -4', autotokenize=True))
        E225: (1, 11)
        >>> checker.find_error(LogicalLine('z = x **y', autotokenize=True))
        E225: (1, 8)
        """
        tokens = line.tokens
        parens = 0
        need_space = False
        prev_type = tokenize.OP
        prev_text = prev_end = None
        for token_type, text, start, end, line in tokens:
            if token_type in (tokenize.NL, tokenize.NEWLINE, tokenize.ERRORTOKEN):
                # ERRORTOKEN is triggered by backticks in Python 3000
                continue
            if text in ('(', 'lambda'):
                parens += 1
            elif text == ')':
                parens -= 1
            if need_space:
                if start != prev_end:
                    need_space = False
                elif text == '>' and prev_text == '<':
                    # Tolerate the "<>" operator, even if running Python 3
                    pass
                else:
                    return CheckerError("E225", column=prev_end)
            elif token_type == tokenize.OP and prev_end is not None:
                if text == '=' and parens:
                    # Allow keyword args or defaults: foo(bar=None).
                    pass
                elif text in BINARY_OPERATORS:
                    need_space = True
                elif text in UNARY_OPERATORS:
                    # Allow unary operators: -123, -x, +1.
                    # Allow argument unpacking: foo(*args, **kwargs).
                    if prev_type == tokenize.OP:
                        if prev_text in CLOSE_PARENS:
                            need_space = True
                    elif prev_type == tokenize.NAME:
                        if prev_text not in self.E225NOT_KEYWORDS:
                            need_space = True
                    else:
                        need_space = True
                if need_space and start == prev_end:
                    return CheckerError("E225", column=prev_end)
            prev_type = token_type
            prev_text = text
            prev_end = end



class WhitespaceAroundComma(object):
    r"""
    Avoid extraneous whitespace in the following situations:

    - More than one space around an assignment (or other) operator to
      align it with another.

    JCR: This should also be applied around comma etc.
    Note: these checks are disabled by default

    Okay: a = (1, 2)
    E241: a = (1,  2)
    E242: a = (1,\t2)
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = WhitespaceAroundComma()
        >>> checker.find_error(LogicalLine('a = (1, 2)'))
        >>> checker.find_error(LogicalLine('a = (1,  2)'))
        E241: 7
        >>> checker.find_error(LogicalLine('a = (1,\t2)'))
        E242: 7
        """
        for separator in ',;:':
            found = line.logical_line.find(separator + '  ')
            if found > -1:
                return CheckerError("E241", column=found + 1, separator=separator)
            found = line.logical_line.find(separator + '\t')
            if found > -1:
                return CheckerError("E242", column=found + 1, separator=separator)


class WhitespaceAroundNamedParameterEquals(object):
    r"""
    Don't use spaces around the '=' sign when used to indicate a
    keyword argument or a default parameter value.

    Okay: def complex(real, imag=0.0):
    Okay: return magic(r=real, i=imag)
    Okay: boolean(a == b)
    Okay: boolean(a != b)
    Okay: boolean(a <= b)
    Okay: boolean(a >= b)

    E251: def complex(real, imag = 0.0):
    E251: return magic(r = real, i = imag)
    """

    __metaclass__ = LogicalLineChecker

    WHITESPACE_AROUND_NAMED_PARAMETER_REGEX = re.compile(r'[()]|\s=[^=]|[^=!<>]=\s')

    def find_error(self, line, previous_line=None, document=None):
        """
        >>> checker = WhitespaceAroundNamedParameterEquals()
        >>> checker.find_error(LogicalLine('def complex(real, imag=0.0):'))
        >>> checker.find_error(LogicalLine('return magic(r=real, i=imag)'))
        >>> checker.find_error(LogicalLine('boolean(a == b)'))
        >>> checker.find_error(LogicalLine('boolean(a != b)'))
        >>> checker.find_error(LogicalLine('boolean(a <= b)'))
        >>> checker.find_error(LogicalLine('boolean(a >= b)'))
        >>> checker.find_error(LogicalLine('def complex(real, imag = 0.0):'))
        E251: 22
        >>> checker.find_error(LogicalLine('return magic(r = real, i = imag)'))
        E251: 14
        """
        parens = 0
        for match in self.WHITESPACE_AROUND_NAMED_PARAMETER_REGEX.finditer(line.logical_line):
            text = match.group()
            if parens and len(text) == 3:
                column = match.start()
                return CheckerError("E251", column=column)
            if text == '(':
                parens += 1
            elif text == ')':
                parens -= 1


class WhitespaceAroundInlineComment(object):
    """
    Separate inline comments by at least two spaces.
    
    An inline comment is a comment on the same line as a statement. Inline
    comments should be separated by at least two spaces from the statement.
    They should start with a # and a single space.

    Okay: x = x + 1  # Increment x
    Okay: x = x + 1    # Increment x
    E261: x = x + 1 # Increment x
    E262: x = x + 1  #Increment x
    E262: x = x + 1  #  Increment x
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        """
        >>> checker = WhitespaceAroundInlineComment()
        >>> checker.find_error(LogicalLine('x = x + 1  # Increment x', autotokenize=True))
        >>> checker.find_error(LogicalLine('x = x + 1    # Increment x', autotokenize=True))
        >>> checker.find_error(LogicalLine('x = x + 1 # Increment x', autotokenize=True))
        E261: (1, 9)
        >>> checker.find_error(LogicalLine('x = x + 1  #Increment x', autotokenize=True))
        E262: (1, 11)
        >>> checker.find_error(LogicalLine('x = x + 1  #  Increment x', autotokenize=True))
        E262: (1, 11)
        >>> checker.find_error(LogicalLine('x = x + 1  #', autotokenize=True))
        """
        prev_end = (0, 0)
        for token_type, text, start, end, line in line.tokens:
            if token_type == tokenize.NL:
                continue
            if token_type == tokenize.COMMENT:
                # TODO: What does this if statement do? Write a test for it.
                if not line[:start[1]].strip():
                    continue
                if len(text) > 1 and (text.startswith('#  ') or not text.startswith('# ')):
                    return CheckerError("E262", column=start)
                if prev_end[0] == start[0] and start[1] < prev_end[1] + 2:
                    return CheckerError("E261", column=prev_end)
            else:
                prev_end = end


class ImportsOnSeparateLines(object):
    r"""
    Imports should usually be on separate lines.

    Okay: import os\nimport sys
    E401: import sys, os
    
    Okay: from subprocess import Popen, PIPE
    Okay: from myclas import MyClass
    Okay: from foo.bar.yourclass import YourClass
    Okay: import myclass
    Okay: import foo.bar.yourclass
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = ImportsOnSeparateLines()
        >>> checker.find_error(LogicalLine('import os\nimport sys'))
        >>> checker.find_error(LogicalLine('import sys, os'))
        E401: 10
        >>> checker.find_error(LogicalLine('from subprocess import Popen, PIPE'))
        >>> checker.find_error(LogicalLine('from myclas import MyClass'))
        >>> checker.find_error(LogicalLine('from foo.bar.yourclass import YourClass'))
        >>> checker.find_error(LogicalLine('import myclass'))
        >>> checker.find_error(LogicalLine('import foo.bar.yourclass'))
        """
        if line.logical_line.startswith('import '):
            found = line.logical_line.find(',')
            if found > -1:
                return CheckerError("E401", found)


class CompoundStatement(object):
    r"""
    Compound statements (multiple statements on the same line) are
    generally discouraged.
    
    While sometimes it's okay to put an if/for/while with a small body
    on the same line, never do this for multi-clause statements. Also
    avoid folding such long lines!

    Okay: if foo == 'blah':\n    do_blah_thing()
    Okay: do_one()
    Okay: do_two()
    Okay: do_three()

    E701: if foo == 'blah': do_blah_thing()
    E701: for x in lst: total += x
    E701: while t < 10: t = delay()
    E701: if foo == 'blah': do_blah_thing()
    E701: else: do_non_blah_thing()
    E701: try: something()
    E701: finally: cleanup()
    E701: if foo == 'blah': one(); two(); three()
    E702: do_one(); do_two(); do_three()
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = CompoundStatement()
        >>> checker.find_error(LogicalLine('if foo == "blah": do_blah_thing()'))
        E701: 16
        >>> checker.find_error(LogicalLine('while t < 10: t = delay()'))
        E701: 12
        >>> checker.find_error(LogicalLine('if foo == "blah": do_blah_thing()'))
        E701: 16
        >>> checker.find_error(LogicalLine('else: do_non_blah_thing()'))
        E701: 4
        >>> checker.find_error(LogicalLine('try: something()'))
        E701: 3
        >>> checker.find_error(LogicalLine('finally: cleanup()'))
        E701: 7
        >>> checker.find_error(LogicalLine('if foo == "blah": one(); two(); three()'))
        E701: 16
        >>> checker.find_error(LogicalLine('do_one(); do_two(); do_three()'))
        E702: 8
        """
        found = line.logical_line.find(':')
        if -1 < found < len(line.logical_line) - 1:
            before = line.logical_line[:found]
            if (before.count('{') <= before.count('}') and  # {'a': 1} (dict)
                before.count('[') <= before.count(']') and  # [1:2] (slice)
                not re.search(r'\blambda\b', before)):      # lambda x: x
                return CheckerError("E701", column=found)
        found = line.logical_line.find(';')
        if -1 < found:
            return CheckerError("E702", column=found)


class Python3000HasKey(object):
    """
    The {}.has_key() method will be removed in the future version of
    Python. Use the 'in' operation instead, like:
    d = {"a": 1, "b": 2}
    if "b" in d:
        print d["b"]
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = Python3000HasKey()
        >>> checker.find_error(LogicalLine('{"A": 3}.has_key("A")'))
        W601: 8
        >>> checker.find_error(LogicalLine('"A" in {"A": 3}'))
        """
        pos = line.logical_line.find('.has_key(')
        if pos > -1:
            return CheckerError("W601", column=pos)


class Python3000RaiseComma(object):
    """
    When raising an exception, use "raise ValueError('message')"
    instead of the older form "raise ValueError, 'message'".
    
    The paren-using form is preferred because when the exception arguments
    are long or include string formatting, you don't need to use line
    continuation characters thanks to the containing parentheses. The older
    form will be removed in Python 3000.
    """

    __metaclass__ = LogicalLineChecker

    RAISE_COMMA_REGEX = re.compile(r'raise\s+\w+\s*(,)')

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = Python3000RaiseComma()
        >>> checker.find_error(LogicalLine('raise ValueError, "message"'))
        W602: 16
        >>> checker.find_error(LogicalLine('raise ValueError("message")'))
        """
        match = self.RAISE_COMMA_REGEX.match(line.logical_line)
        if match:
            return CheckerError("W602", column=match.start(1))


class Python3000NotEqual(object):
    """
    != can also be written <>, but this is an obsolete usage kept for
    backwards compatibility only. New code should always use !=.
    The older syntax is removed in Python 3000.
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = Python3000NotEqual()
        >>> checker.find_error(LogicalLine('a <> b'))
        W603: 2
        >>> checker.find_error(LogicalLine('a != b'))
        >>> checker.find_error(LogicalLine('a > b'))
        >>> checker.find_error(LogicalLine('a < b'))
        """
        pos = line.logical_line.find('<>')
        if pos > -1:
            return CheckerError("W603", column=pos)


class Python3000Backticks(object):
    """
    Backticks are removed in Python 3000.
    Use repr() instead.
    """

    __metaclass__ = LogicalLineChecker

    def find_error(self, line, previous_line=None, document=None):
        r"""
        >>> checker = Python3000Backticks()
        >>> checker.find_error(LogicalLine('print `{}`'))
        W604: 6
        >>> checker.find_error(LogicalLine('print repr({})'))
        """
        pos = line.logical_line.find('`')
        if pos > -1:
            return CheckerError("W604", column=pos)


##############################################################################
# Helper functions
##############################################################################


def indentation_level(line):
    """
    Return the amount of indentation.
    Tabs are expanded to the next multiple of 8.

    >>> indentation_level('    ')
    4
    >>> indentation_level('\\t')
    8
    >>> indentation_level('    \\t')
    8
    >>> indentation_level('       \\t')
    8
    >>> indentation_level('        \\t')
    16
    """
    result = 0
    for char in line:
        if char == '\t':
            result = result // 8 * 8 + 8
        elif char == ' ':
            result += 1
        else:
            break
    return result


def mute_string(text):
    """
    Replace contents with 'xxx' to prevent syntax matching.

    >>> mute_string('"abc"')
    '"xxx"'
    >>> mute_string("'''abc'''")
    "'''xxx'''"
    >>> mute_string("r'abc'")
    "r'xxx'"
    """
    start = 1
    end = len(text) - 1
    # String modifiers (e.g. u or r)
    if text.endswith('"'):
        start += text.index('"')
    elif text.endswith("'"):
        start += text.index("'")
    # Triple quotes
    if text.endswith('"""') or text.endswith("'''"):
        start += 2
        end -= 2
    return text[:start] + 'x' * (end - start) + text[end:]


if '' == ''.encode():
    # Python 2: implicit encoding.
    def readlines(filename):
        return open(filename).readlines()
else:
    # Python 3: decode to latin-1.
    # This function is lazy, it does not read the encoding declaration.
    # XXX: use tokenize.detect_encoding()
    def readlines(filename):
        return open(filename, encoding='latin-1').readlines()


def leading_indentation(s, indent_chars=INDENTATION_WHITESPACE):
    r"""
    Returns the leading indentation for a string s.
    
    >>> leading_indentation("   abc")
    '   '
    >>> leading_indentation(" abc ")
    ' '
    >>> leading_indentation("\tabc")
    '\t'
    >>> leading_indentation(" \t \t abc  \t\t  def  ")
    ' \t \t '
    >>> leading_indentation("")
    ''
    >>> leading_indentation("a bcdef", indent_chars="ab ")
    'a b'
    """
    return s[:len(s) - len(s.lstrip(indent_chars))]


def line_ending(s):
    r"""
    Returns the line_ending string for a string s.
    >>> line_ending("\n")
    '\n'
    >>> line_ending("abc\n")
    '\n'
    >>> line_ending("abc \n")
    '\n'
    >>> line_ending("")
    ''
    >>> line_ending("abc \r")
    '\r'
    >>> line_ending("abc \r\n")
    '\r\n'
    """
    without_line_endings = rstrip_newlines(s)
    return s[len(without_line_endings):]


##############################################################################
# Framework to run all checks
##############################################################################


def build_line_from_tokens(tokens, lines):
    """
    Build a logical line from tokens.
    """
    mapping = []
    logical = []
    length = 0
    previous = None
    for token in tokens:
        token_type, text = token[:2]
        if token_type in SKIP_TOKENS:
            continue
        if token_type == tokenize.STRING:
            text = mute_string(text)
        if previous:
            end_line, end = previous[3]
            start_line, start = token[2]
            if end_line != start_line:  # different row
                prev_text = lines[end_line - 1][end - 1]
                if prev_text == ',' or (prev_text not in OPEN_PARENS
                                        and text not in CLOSE_PARENS):
                    logical.append(' ')
                    length += 1
            elif end != start:  # different column
                fill = lines[end_line - 1][end:start]
                logical.append(fill)
                length += len(fill)
        mapping.append((length, token))
        logical.append(text)
        length += len(text)
        previous = token
    logical_line = ''.join(logical)
    assert logical_line.lstrip() == logical_line
    assert logical_line.rstrip() == logical_line

    first_line = lines[mapping[0][1][2][0] - 1]
    indent = first_line[:mapping[0][1][2][1]]
    full_logical_line = indent + logical_line

    return full_logical_line, mapping


def logical_lines(readline_fn, lines):
    blank_lines = 0
    blank_lines_before_comment = 0
    tokens = []
    parens = 0
    for token in tokenize.generate_tokens(readline_fn):
        tokens.append(token)
        token_type, text = token[0:2]
        if token_type == tokenize.OP and text in OPEN_PARENS:
            parens += 1
        if token_type == tokenize.OP and text in CLOSE_PARENS:
            parens -= 1
        if token_type == tokenize.NEWLINE and not parens:
            logical_line, mapping = build_line_from_tokens(tokens, lines)
            line_obj = LogicalLine(logical_line,
                                   tokens=tokens,
                                   blank_lines=blank_lines,
                                   blank_lines_before_comment=blank_lines_before_comment,
                                   token_offset_mapping=mapping)
            yield line_obj
            blank_lines = 0
            blank_lines_before_comment = 0
            tokens = []
        if token_type == tokenize.NL and not parens:
            if len(tokens) <= 1:
                # The physical line contains only this token.
                blank_lines += 1
            tokens = []
        if token_type == tokenize.COMMENT:
            source_line = token[4]
            token_start = token[2][1]
            if source_line[:token_start].strip() == '':
                blank_lines_before_comment = max(blank_lines,
                    blank_lines_before_comment)
                blank_lines = 0
            if text.endswith('\n') and not parens:
                # The comment also ends a physical line. This works around
                # Python < 2.6 behaviour, which does not generate NL after
                # a comment which is on a line by itself.
                tokens = []


class Results(object):

    def __init__(self):
        self.errors = []

    def add_error(self, error):
        self.errors.append(error)

    def contains_error_with_code(self, code):
        return any(error for error in self.errors if error.code == code)

    def errors_ignoring(self, codes_to_ignore):
        return [error for error in self.errors if error.code not in codes_to_ignore]


class Checker(object):
    """
    Check coding style of a source file, code string, or list of code lines.
    """

    def __init__(self, lines=None, code=None):
        if not lines:
            if code:
                lines = code.splitlines(True)
            else:
                lines = readlines(filename)

        self.document = Document(lines)
        self.checker_config = {}  # e.g. {"max_line_length": 200}
        self.results = Results()

    def readline_check_physical(self):
        """
        Check and return the next physical line. This method can be
        used to feed tokenize.generate_tokens.
        """
        line = self.document.readline()
        if line:
            line_obj = PhysicalLine(line, line_number=self.document.line_number)
            self.check_line(line_obj, None)
        return line

    def check_line(self, line, previous_line):
        """
        Run all physical checks on an input line.
        """
        if isinstance(line, PhysicalLine):
            checker_classes = PHYSICAL_LINE_CHECKERS
        elif isinstance(line, LogicalLine):
            checker_classes = LOGICAL_LINE_CHECKERS
        else:
            raise TypeError

        for checker_class in checker_classes:
            checker_instance = checker_class(**self.checker_config)
            error = checker_instance.find_error(line=line, previous_line=previous_line, document=self.document)
            if error is not None:
                error.pep8 = checker_class.__doc__
                error.line = line
                self.results.add_error(error)

    def check_all(self):
        """
        Run all checks on the input file.
        """
        previous_line = None
        for logical_line in logical_lines(self.readline_check_physical, self.document.lines):
            logical_line.line_number = self.document.line_number
            self.check_line(logical_line, previous_line)
            previous_line = logical_line

        return self.results

    def autofix(self):
        """
        Try to fix the input file as much as possible.

        Note that checking is done in a single pass, interweaving physical and logical lines.
        Fixing is done in two passes, first fixing all physical line errors, and then all logical line errors.
        """
        fixed_lines = []
        # Fix physical lines
        for line in self.document.lines:
            autofixed_line = line
            for physical_checker in PHYSICAL_LINE_CHECKERS:
                instance = physical_checker(**self.checker_config)
                input_line = PhysicalLine(autofixed_line, line_number=self.document.line_number)
                try:
                    autofixed_line = instance.autofix(line=input_line, previous_line=None, document=self.document)
                except AttributeError:
                    pass
            fixed_lines.append(autofixed_line)
        return fixed_lines

