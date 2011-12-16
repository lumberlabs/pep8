#!/usr/bin/python
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
MAX_LINE_LENGTH = 79

INDENT_REGEX = re.compile(r'([ \t]*)')
SELFTEST_REGEX = re.compile(r'(Okay|[EW]\d{3}):\s(.*)')
ERRORCODE_REGEX = re.compile(r'[EW]\d{3}')
DOCSTRING_REGEX = re.compile(r'u?r?["\']')
EXTRANEOUS_WHITESPACE_REGEX = re.compile(r'[[({] | []}),;:]')


WHITESPACE = ' \t'

BINARY_OPERATORS = frozenset(['**=', '*=', '+=', '-=', '!=', '<>',
    '%=', '^=', '&=', '|=', '==', '/=', '//=', '<=', '>=', '<<=', '>>=',
    '%',  '^',  '&',  '|',  '=',  '/',  '//',  '<',  '>',  '<<'])
UNARY_OPERATORS = frozenset(['>>', '**', '*', '+', '-'])
OPERATORS = BINARY_OPERATORS | UNARY_OPERATORS
SKIP_TOKENS = frozenset([tokenize.COMMENT, tokenize.NL, tokenize.INDENT,
                         tokenize.DEDENT, tokenize.NEWLINE])
E225NOT_KEYWORDS = (frozenset(keyword.kwlist + ['print']) -
                    frozenset(['False', 'None', 'True']))
BENCHMARK_KEYS = ('directories', 'files', 'logical lines', 'physical lines')

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


class LogicalLine(object):

    def __init__(self, logical_line, tokens=None, autotokenize=None):
        self.logical_line = logical_line
        self.tokens = tokens

        if autotokenize:
            line_io = StringIO.StringIO(self.logical_line)
            self.tokens = tokenize.generate_tokens(line_io.readline)


class Document(object):

    def __init__(self, num_lines=None, indent_char=None):
        self.num_lines = num_lines
        self.indent_char = indent_char


##############################################################################
# Errors and warnings
##############################################################################

class CheckerError(object):
    "The base class for all checker errors."

    def __init__(self, column=None):
        self.column = column

    @property
    def description(self):
        return "%s %s" % (self.code, self.text)

    def __repr__(self):
        # This is a lame __repr__ implementation, but it's kept simple and
        # minimal so that doctests are easy to write and read.
        if self.column is not None:
            return "%s: %s" % (self.code, self.column)
        else:
            return self.code


def checker_error(error_code, error_text, error_class_name):
    class CheckerErrorSubclass(CheckerError):
        code = error_code
        text = error_text
    CheckerErrorSubclass.__name__ = error_class_name
    return CheckerErrorSubclass

NoSpaceAroundKeywordEqualsError = checker_error("E251", "no spaces around keyword / parameter equals", "NoSpaceAroundKeywordEqualsError")
NoWhitespaceAfterInlineCommentError = checker_error("E262", "inline comment should start with '# '", "NoWhitespaceAfterInlineCommentError")
MixedTabsAndSpacesError = checker_error("E101", "indentation contains mixed spaces and tabs", "MixedTabsAndSpacesError")
UsedTabsError = checker_error("W191", "indentation contains tabs", "UsedTabsError")
TrailingWhitespaceError = checker_error("W291", "trailing whitespace", "TrailingWhitespaceError")
LineOfWhiteSpaceError = checker_error("W293", "blank line contains whitespace", "LineOfWhiteSpaceError")
BlankLineAtEOFError = checker_error("W391", "blank line at end of file", "BlankLineAtEOFError")
NoNewlineAtEOFError = checker_error("W292", "no newline at end of file", "NoNewlineAtEOFError")
NotEnoughWhitespaceBeforeInlineCommentError = checker_error("E261", "at least two spaces before inline comment", "NotEnoughWhitespaceBeforeInlineCommentError")
MultipleImportsOnOneLineError = checker_error("E401", "multiple imports on one line", "MultipleImportsOnOneLineError")
CompoundStatementWithColonError = checker_error("E701", "multiple statements on one line (colon)", "CompoundStatementWithColonError")
CompoundStatementWithSemicolonError = checker_error("E702", "multiple statements on one line (semicolon)", "CompoundStatementWithSemicolonError")
HasKeyError = checker_error("W601", ".has_key() is deprecated, use 'in'", "HasKeyError")
ExceptionWithCommaError = checker_error("W602", "deprecated form of raising exception", "ExceptionWithCommaError")
DeprecatedNotEqualsError = checker_error("W603", "'<>' is deprecated, use '!='", "DeprecatedNotEqualsError")
DeprecatedBackticksError = checker_error("W604", "backticks are deprecated, use 'repr()'", "DeprecatedBackticksError")
MissingWhitespaceError = checker_error("E225", "missing whitespace around operator", "MissingWhitespaceError")


class LineTooLongError(CheckerError):
    code = "E501"

    def __init__(self, line_length, max_line_length):
        self.column = max_line_length
        self.max_line_length = max_line_length
        self.line_length = line_length

    @property
    def text(self):
        return "line too long (%d characters)" % self.line_length


class WhitespaceSeparatorError(CheckerError):

    def __init__(self, column, separator):
        self.column = column
        self.separator = separator

    @property
    def text(self):
        return "%s after '%s'" % (self.space_type, self.separator)


class MultipleSpacesAfterSeparatorError(WhitespaceSeparatorError):
    code = "E241"
    space_type = "multiple spaces"


class TabAfterSeparatorError(WhitespaceSeparatorError):
    code = "E242"
    space_type = "tab"


class WhitespaceBeforeParametersError(CheckerError):
    code = "E211"

    def __init__(self, column, context):
        self.column = column
        self.context = context

    @property
    def text(self):
        return "whitespace before '%s'" % self.context


class WhitespaceAroundOperatorError(CheckerError):

    def __init__(self, column, whitespace, error_is_before_operator):
        self.column = column
        self.whitespace_is_tab = whitespace == "\t"
        self.whitespace_description = "tab" if self.whitespace_is_tab else "multiple spaces"
        self.before = error_is_before_operator
        self.location_description = "before" if self.before else "after"

    @property
    def code(self):
        code_map = {(True, True): "E223",
                    (True, False): "E221",
                    (False, True): "E224",
                    (False, False): "E222",
                   }
        return code_map[(self.before, self.whitespace_is_tab)]

    @property
    def text(self):
        return "%s %s operator" % (self.whitespace_description, self.location_description)


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
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, document):
        r"""
        >>> checker = TabsOrSpaces()
        >>> checker.find_error(PhysicalLine('if a == 0:'), Document(indent_char=' '))
        >>> checker.find_error(PhysicalLine('        a = 1'), Document(indent_char=' '))
        >>> checker.find_error(PhysicalLine('\ta = 1'), Document(indent_char=' '))
        E101: 0
        >>> checker.find_error(PhysicalLine('        \ta = 1'), Document(indent_char=' '))
        E101: 8
        >>> checker.find_error(PhysicalLine('\t        a = 1'), Document(indent_char=' '))
        E101: 0
        >>> checker.find_error(PhysicalLine('        a = 1'), Document(indent_char='\t'))
        E101: 0
        >>> checker.find_error(PhysicalLine('\ta = 1'), Document(indent_char='\t'))
        >>> checker.find_error(PhysicalLine('        \ta = 1'), Document(indent_char='\t'))
        E101: 0
        >>> checker.find_error(PhysicalLine('\t        a = 1'), Document(indent_char='\t'))
        E101: 1
        """
        indent = INDENT_REGEX.match(line.physical_line).group(1)
        for offset, char in enumerate(indent):
            if char != document.indent_char:
                return MixedTabsAndSpacesError(offset)


class TabsObsolete(object):
    r"""
    For new projects, spaces-only are strongly recommended over tabs. Most
    editors have features that make this easy to do.

    Okay: if True:\n    return
    W191: if True:\n\treturn
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, document=None):
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
        indent = INDENT_REGEX.match(line.physical_line).group(1)
        try:
            column = indent.index('\t')
            return UsedTabsError(column)
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

    def find_error(self, line, document=None):
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
            return LineOfWhiteSpaceError()
        if without_newlines != without_spaces:
            column = len(without_spaces)
            return TrailingWhitespaceError(column)


class TrailingBlankLines(object):
    r"""
    JCR: Trailing blank lines are superfluous.

    Okay: spam(1)
    W391: spam(1)\n
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, document):
        r"""
        >>> checker = TrailingBlankLines()
        >>> checker.find_error(PhysicalLine('a == 0', line_number=1), Document(num_lines=1))
        >>> checker.find_error(PhysicalLine('', line_number=1), Document(num_lines=1))
        W391
        >>> checker.find_error(PhysicalLine('', line_number=1), Document(num_lines=2))
        >>> checker.find_error(PhysicalLine('a == 0', line_number=1), Document(num_lines=1))
        """
        if line.physical_line.strip() == '' and line.line_number == document.num_lines:
            return BlankLineAtEOFError()


class MissingNewline(object):
    """
    JCR: The last line should have a newline.
    """

    __metaclass__ = PhysicalLineChecker

    def find_error(self, line, document=None):
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
            return NoNewlineAtEOFError(column)


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

    def __init__(self, max_line_length=MAX_LINE_LENGTH, **kwargs):
        self.max_line_length = max_line_length

    def find_error(self, line, document=None):
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
            return LineTooLongError(line_length=length,
                                    max_line_length=self.max_line_length)


##############################################################################
# Plugins (check functions) for logical lines
##############################################################################


def blank_lines(logical_line, blank_lines, indent_level, line_number,
                previous_logical, previous_indent_level,
                blank_lines_before_comment):
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
    if line_number == 1:
        return  # Don't expect blank lines before the first line
    max_blank_lines = max(blank_lines, blank_lines_before_comment)
    if previous_logical.startswith('@'):
        if max_blank_lines:
            return 0, "E304 blank lines found after function decorator"
    elif max_blank_lines > 2 or (indent_level and max_blank_lines == 2):
        return 0, "E303 too many blank lines (%d)" % max_blank_lines
    elif (logical_line.startswith('def ') or
          logical_line.startswith('class ') or
          logical_line.startswith('@')):
        if indent_level:
            if not (max_blank_lines or previous_indent_level < indent_level or
                    DOCSTRING_REGEX.match(previous_logical)):
                return 0, "E301 expected 1 blank line, found 0"
        elif max_blank_lines != 2:
            return 0, "E302 expected 2 blank lines, found %d" % max_blank_lines


def extraneous_whitespace(logical_line):
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
    line = logical_line
    for match in EXTRANEOUS_WHITESPACE_REGEX.finditer(line):
        text = match.group()
        char = text.strip()
        found = match.start()
        if text == char + ' ' and char in '([{':
            return found + 1, "E201 whitespace after '%s'" % char
        if text == ' ' + char and line[found - 1] != ',':
            if char in '}])':
                return found, "E202 whitespace before '%s'" % char
            if char in ',;:':
                return found, "E203 whitespace before '%s'" % char


def missing_whitespace(logical_line):
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
    line = logical_line
    for index in range(len(line) - 1):
        char = line[index]
        if char in ',;:' and line[index + 1] not in WHITESPACE:
            before = line[:index]
            if char == ':' and before.count('[') > before.count(']'):
                continue  # Slice syntax, no space required
            if char == ',' and line[index + 1] == ')':
                continue  # Allow tuple with only one element: (3,)
            return index, "E231 missing whitespace after '%s'" % char


def indentation(logical_line, previous_logical, indent_char,
                indent_level, previous_indent_level):
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
    if indent_char == ' ' and indent_level % 4:
        return 0, "E111 indentation is not a multiple of four"
    indent_expect = previous_logical.endswith(':')
    if indent_expect and indent_level <= previous_indent_level:
        return 0, "E112 expected an indented block"
    if indent_level > previous_indent_level and not indent_expect:
        return 0, "E113 unexpected indentation"


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
                (prev_type == tokenize.NAME or prev_text in '}])') and
                # Syntax "class A (B):" is allowed, but avoid it
                (index < 2 or tokens[index - 2][1] != 'class') and
                # Allow "return (a.foo for a in range(5))"
                (not keyword.iskeyword(prev_text))):
                return WhitespaceBeforeParametersError(prev_end, text)
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
        >>> checker.find_error(LogicalLine('a = 4 +  5'))
        E222: 7
        >>> checker.find_error(LogicalLine('a = 4\t+ 5'))
        E223: 5
        >>> checker.find_error(LogicalLine('a = 4 +\t5'))
        E224: 7
        """
        for match in self.WHITESPACE_AROUND_OPERATOR_REGEX.finditer(line.logical_line):
            before, whitespace, after = match.groups()
            tab = whitespace == '\t'
            offset = match.start(2)
            if before in OPERATORS:
                return WhitespaceAroundOperatorError(offset, whitespace, False)
            elif after in OPERATORS:
                return WhitespaceAroundOperatorError(offset, whitespace, True)


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
                    return MissingWhitespaceError(prev_end)
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
                        if prev_text in '}])':
                            need_space = True
                    elif prev_type == tokenize.NAME:
                        if prev_text not in E225NOT_KEYWORDS:
                            need_space = True
                    else:
                        need_space = True
                if need_space and start == prev_end:
                    return MissingWhitespaceError(prev_end)
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
                return MultipleSpacesAfterSeparatorError(found + 1, separator)
            found = line.logical_line.find(separator + '\t')
            if found > -1:
                return TabAfterSeparatorError(found + 1, separator)


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
                return NoSpaceAroundKeywordEqualsError(column)
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
        """
        prev_end = (0, 0)
        for token_type, text, start, end, line in line.tokens:
            if token_type == tokenize.NL:
                continue
            if token_type == tokenize.COMMENT:
                # TODO: What does this if statement do? Write a test for it.
                if not line[:start[1]].strip():
                    continue
                # TODO: "A and B or C" is ambiguous. Ick.
                if (len(text) > 1 and text.startswith('#  ')
                               or not text.startswith('# ')):
                    return NoWhitespaceAfterInlineCommentError(start)
                if prev_end[0] == start[0] and start[1] < prev_end[1] + 2:
                    return NotEnoughWhitespaceBeforeInlineCommentError(prev_end)
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
                return MultipleImportsOnOneLineError(found)


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
                return CompoundStatementWithColonError(found)
        found = line.logical_line.find(';')
        if -1 < found:
            return CompoundStatementWithSemicolonError(found)


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
            return HasKeyError(pos)


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
            return ExceptionWithCommaError(match.start(1))


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
            return DeprecatedNotEqualsError(pos)


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
            return DeprecatedBackticksError(pos)


##############################################################################
# Helper functions
##############################################################################


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


def message(text):
    """Print a message."""
    # print >> sys.stderr, options.prog + ': ' + text
    # print >> sys.stderr, text
    print(text)


##############################################################################
# Framework to run all checks
##############################################################################


def find_checks(argument_name):
    """
    Find all globally visible functions where the first argument name
    starts with argument_name.
    """
    checks = []
    for name, function in globals().items():
        if not inspect.isfunction(function):
            continue
        args = inspect.getargspec(function)[0]
        if args and args[0].startswith(argument_name):
            codes = ERRORCODE_REGEX.findall(inspect.getdoc(function) or '')
            for code in codes or ['']:
                if not code or not ignore_code(code):
                    checks.append((name, function, args))
                    break
    checks.sort()
    return checks


def leading_indentation(s, indent_chars=" \t"):
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


def most_common_indent_char(list_of_strings, indent_chars=" \t"):
    r"""
    Determine which of a set of indentation characters occurs most in a list of lines.
    Behavior is undetermined if there is a tie.

    >>> most_common_indent_char([" a", " b", " c"], indent_chars=" \t")
    ' '
    >>> most_common_indent_char([" a", " b", "\tc"], indent_chars=" \t")
    ' '
    >>> most_common_indent_char([" a", "\tb", "\tc"], indent_chars=" \t")
    '\t'
    >>> most_common_indent_char([], indent_chars=" \t") in " \t"  # tie
    True
    >>> most_common_indent_char(["  a", "\tb", "\tc"], indent_chars=" \t") in " \t"  # tie
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


class Checker(object):
    """
    Load a Python source file, tokenize it, check coding style.
    """

    def __init__(self, filename, lines=None):
        self.filename = filename
        if filename is None:
            self.filename = 'stdin'
            self.lines = lines or []
        elif lines is None:
            self.lines = readlines(filename)
        else:
            self.lines = lines
        options.counters['physical lines'] += len(self.lines)

        self.indent_char = most_common_indent_char(self.lines)  # TODO: Remove me
        self.document = Document(num_lines=len(self.lines),
                                 indent_char=most_common_indent_char(self.lines))

    def readline(self):
        """
        Get the next line from the input buffer.
        """
        self.line_number += 1
        if self.line_number > len(self.lines):
            return ''
        return self.lines[self.line_number - 1]

    def readline_check_physical(self):
        """
        Check and return the next physical line. This method can be
        used to feed tokenize.generate_tokens.
        """
        line = self.readline()
        if line:
            self.check_physical(line)
        return line

    def run_check(self, check, argument_names):
        """
        Run a check plugin.
        """
        arguments = []
        for name in argument_names:
            arguments.append(getattr(self, name))
        return check(*arguments)

    def check_physical(self, line):
        """
        Run all physical checks on a raw input line.
        """
        line_number = self.line_number
        for cls in PHYSICAL_LINE_CHECKERS:
            checker_config = {}  # e.g. {"max_line_length": 200}
            instance = cls(**checker_config)
            line_obj = PhysicalLine(line, line_number=line_number)
            error = instance.find_error(line=line_obj, document=self.document)
            if error is not None:
                self.report_error(line_number, error.column or 0, error.description, cls)

    def build_tokens_line(self):
        """
        Build a logical line from tokens.
        """
        self.mapping = []
        logical = []
        length = 0
        previous = None
        for token in self.tokens:
            token_type, text = token[0:2]
            if token_type in SKIP_TOKENS:
                continue
            if token_type == tokenize.STRING:
                text = mute_string(text)
            if previous:
                end_line, end = previous[3]
                start_line, start = token[2]
                if end_line != start_line:  # different row
                    prev_text = self.lines[end_line - 1][end - 1]
                    if prev_text == ',' or (prev_text not in '{[('
                                            and text not in '}])'):
                        logical.append(' ')
                        length += 1
                elif end != start:  # different column
                    fill = self.lines[end_line - 1][end:start]
                    logical.append(fill)
                    length += len(fill)
            self.mapping.append((length, token))
            logical.append(text)
            length += len(text)
            previous = token
        self.logical_line = ''.join(logical)
        assert self.logical_line.lstrip() == self.logical_line
        assert self.logical_line.rstrip() == self.logical_line

    def check_logical(self):
        """
        Build a line from tokens and run all logical checks on it.
        """
        options.counters['logical lines'] += 1
        self.build_tokens_line()
        first_line = self.lines[self.mapping[0][1][2][0] - 1]
        indent = first_line[:self.mapping[0][1][2][1]]
        self.previous_indent_level = self.indent_level
        self.indent_level = indentation_level(indent)
        if options.verbose >= 2:
            print(self.logical_line[:80].rstrip())
        for name, check, argument_names in options.logical_checks:
            if options.verbose >= 4:
                print('   ' + name)
            result = self.run_check(check, argument_names)
            if result is not None:
                offset, text = result
                if isinstance(offset, tuple):
                    original_number, original_offset = offset
                else:
                    for token_offset, token in self.mapping:
                        if offset >= token_offset:
                            original_number = token[2][0]
                            original_offset = (token[2][1]
                                               + offset - token_offset)
                self.report_error(original_number, original_offset,
                                  text, check)

        for cls in LOGICAL_LINE_CHECKERS:

            checker_config = {}  # e.g. {"max_line_length": 200}
            instance = cls(**checker_config)
            line_obj = LogicalLine(self.logical_line, tokens=self.tokens)
            error = instance.find_error(line=line_obj, document=self.document)
            if error is not None:
                error_column = error.column or 0

                if isinstance(error_column, tuple):
                    original_number, original_offset = error_column
                else:
                    for token_offset, token in self.mapping:
                        if error_column >= token_offset:
                            original_number = token[2][0]
                            original_offset = (token[2][1]
                                               + error_column - token_offset)

                self.report_error(original_number, original_offset, error.description, cls)

        self.previous_logical = self.logical_line



    def check_all(self, expected=None, line_offset=0):
        """
        Run all checks on the input file.
        """
        self.expected = expected or ()
        self.line_offset = line_offset
        self.line_number = 0
        self.file_errors = 0
        self.indent_level = 0
        self.previous_logical = ''
        self.blank_lines = 0
        self.blank_lines_before_comment = 0
        self.tokens = []
        parens = 0
        for token in tokenize.generate_tokens(self.readline_check_physical):
            if options.verbose >= 3:
                if token[2][0] == token[3][0]:
                    pos = '[%s:%s]' % (token[2][1] or '', token[3][1])
                else:
                    pos = 'l.%s' % token[3][0]
                print('l.%s\t%s\t%s\t%r' %
                    (token[2][0], pos, tokenize.tok_name[token[0]], token[1]))
            self.tokens.append(token)
            token_type, text = token[0:2]
            if token_type == tokenize.OP and text in '([{':
                parens += 1
            if token_type == tokenize.OP and text in '}])':
                parens -= 1
            if token_type == tokenize.NEWLINE and not parens:
                self.check_logical()
                self.blank_lines = 0
                self.blank_lines_before_comment = 0
                self.tokens = []
            if token_type == tokenize.NL and not parens:
                if len(self.tokens) <= 1:
                    # The physical line contains only this token.
                    self.blank_lines += 1
                self.tokens = []
            if token_type == tokenize.COMMENT:
                source_line = token[4]
                token_start = token[2][1]
                if source_line[:token_start].strip() == '':
                    self.blank_lines_before_comment = max(self.blank_lines,
                        self.blank_lines_before_comment)
                    self.blank_lines = 0
                if text.endswith('\n') and not parens:
                    # The comment also ends a physical line. This works around
                    # Python < 2.6 behaviour, which does not generate NL after
                    # a comment which is on a line by itself.
                    self.tokens = []
        return self.file_errors

    def report_error(self, line_number, offset, text, check):
        """
        Report an error, according to options.
        """
        code = text[:4]
        if ignore_code(code):
            return
        if options.quiet == 1 and not self.file_errors:
            message(self.filename)
        if code in options.counters:
            options.counters[code] += 1
        else:
            options.counters[code] = 1
            options.messages[code] = text[5:]
        if options.quiet or code in self.expected:
            # Don't care about expected errors or warnings
            return
        self.file_errors += 1
        if options.counters[code] == 1 or options.repeat:
            message("%s:%s:%d: %s" %
                    (self.filename, self.line_offset + line_number,
                     offset + 1, text))
            if options.show_source:
                line = self.lines[line_number - 1]
                message(line.rstrip())
                message(' ' * offset + '^')
            if options.show_pep8:
                message(check.__doc__.lstrip('\n').rstrip())


def input_file(filename):
    """
    Run all checks on a Python source file.
    """
    if options.verbose:
        message('checking ' + filename)
    errors = Checker(filename).check_all()


def input_dir(dirname, runner=None):
    """
    Check all Python source files in this directory and all subdirectories.
    """
    dirname = dirname.rstrip('/')
    if excluded(dirname):
        return
    if runner is None:
        runner = input_file
    for root, dirs, files in os.walk(dirname):
        if options.verbose:
            message('directory ' + root)
        options.counters['directories'] += 1
        dirs.sort()
        for subdir in dirs:
            if excluded(subdir):
                dirs.remove(subdir)
        files.sort()
        for filename in files:
            if filename_match(filename) and not excluded(filename):
                options.counters['files'] += 1
                runner(os.path.join(root, filename))


def excluded(filename):
    """
    Check if options.exclude contains a pattern that matches filename.
    """
    basename = os.path.basename(filename)
    for pattern in options.exclude:
        if fnmatch(basename, pattern):
            # print basename, 'excluded because it matches', pattern
            return True


def filename_match(filename):
    """
    Check if options.filename contains a pattern that matches filename.
    If options.filename is unspecified, this always returns True.
    """
    if not options.filename:
        return True
    for pattern in options.filename:
        if fnmatch(filename, pattern):
            return True


def ignore_code(code):
    """
    Check if options.ignore contains a prefix of the error code.
    If options.select contains a prefix of the error code, do not ignore it.
    """
    for select in options.select:
        if code.startswith(select):
            return False
    for ignore in options.ignore:
        if code.startswith(ignore):
            return True


def reset_counters():
    for key in list(options.counters.keys()):
        if key not in BENCHMARK_KEYS:
            del options.counters[key]
    options.messages = {}


def get_error_statistics():
    """Get error statistics."""
    return get_statistics("E")


def get_warning_statistics():
    """Get warning statistics."""
    return get_statistics("W")


def get_statistics(prefix=''):
    """
    Get statistics for message codes that start with the prefix.

    prefix='' matches all errors and warnings
    prefix='E' matches all errors
    prefix='W' matches all warnings
    prefix='E4' matches all errors that have to do with imports
    """
    stats = []
    keys = list(options.messages.keys())
    keys.sort()
    for key in keys:
        if key.startswith(prefix):
            stats.append('%-7s %s %s' %
                         (options.counters[key], key, options.messages[key]))
    return stats


def get_count(prefix=''):
    """Return the total count of errors and warnings."""
    keys = list(options.messages.keys())
    count = 0
    for key in keys:
        if key.startswith(prefix):
            count += options.counters[key]
    return count


def print_statistics(prefix=''):
    """Print overall statistics (number of errors and warnings)."""
    for line in get_statistics(prefix):
        print(line)


def print_benchmark(elapsed):
    """
    Print benchmark numbers.
    """
    print('%-7.2f %s' % (elapsed, 'seconds elapsed'))
    for key in BENCHMARK_KEYS:
        print('%-7d %s per second (%d total)' % (
            options.counters[key] / elapsed, key,
            options.counters[key]))


def run_tests(filename):
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
            errors = Checker(filename, testcase).check_all(codes, line_offset)
            # Check if the expected errors were found
            for code in codes:
                if not options.counters.get(code):
                    errors += 1
                    message('%s: error %s not found' % (label, code))
            if options.verbose and not errors:
                message('%s: passed (%s)' % (label, ' '.join(codes)))
            # Keep showing errors for multiple tests
            reset_counters()
        # output the real line numbers
        line_offset = index
        # configure the expected errors
        codes = line.split()[1:]
        # empty the test case buffer
        del testcase[:]


def selftest():
    """
    Test all check functions with test cases in docstrings.
    """
    count_passed = 0
    count_failed = 0
    checks = options.physical_checks + options.logical_checks
    for name, check, argument_names in checks:
        for line in check.__doc__.splitlines():
            line = line.lstrip()
            match = SELFTEST_REGEX.match(line)
            if match is None:
                continue
            code, source = match.groups()
            checker = Checker(None)
            for part in source.split(r'\n'):
                part = part.replace(r'\t', '\t')
                part = part.replace(r'\s', ' ')
                checker.lines.append(part + '\n')
            options.quiet = 2
            checker.check_all()
            error = None
            if code == 'Okay':
                if len(options.counters) > len(BENCHMARK_KEYS):
                    codes = [key for key in options.counters.keys()
                             if key not in BENCHMARK_KEYS]
                    error = "incorrectly found %s" % ', '.join(codes)
            elif not options.counters.get(code):
                error = "failed to find %s" % code
            # Reset the counters
            reset_counters()
            if not error:
                count_passed += 1
            else:
                count_failed += 1
                if len(checker.lines) == 1:
                    print("pep8.py: %s: %s" %
                          (error, checker.lines[0].rstrip()))
                else:
                    print("pep8.py: %s:" % error)
                    for line in checker.lines:
                        print(line.rstrip())
    if options.verbose:
        print("%d passed and %d failed." % (count_passed, count_failed))
        if count_failed:
            print("Test failed.")
        else:
            print("Test passed.")


def process_options(arglist=None):
    """
    Process options passed either via arglist or via command line args.
    """
    global options, args
    parser = OptionParser(version=__version__,
                          usage="%prog [options] input ...")
    parser.add_option('-v', '--verbose', default=0, action='count',
                      help="print status messages, or debug with -vv")
    parser.add_option('-q', '--quiet', default=0, action='count',
                      help="report only file names, or nothing with -qq")
    parser.add_option('-r', '--repeat', action='store_true',
                      help="show all occurrences of the same error")
    parser.add_option('--exclude', metavar='patterns', default=DEFAULT_EXCLUDE,
                      help="exclude files or directories which match these "
                        "comma separated patterns (default: %s)" %
                        DEFAULT_EXCLUDE)
    parser.add_option('--filename', metavar='patterns', default='*.py',
                      help="when parsing directories, only check filenames "
                        "matching these comma separated patterns (default: "
                        "*.py)")
    parser.add_option('--select', metavar='errors', default='',
                      help="select errors and warnings (e.g. E,W6)")
    parser.add_option('--ignore', metavar='errors', default='',
                      help="skip errors and warnings (e.g. E4,W)")
    parser.add_option('--show-source', action='store_true',
                      help="show source code for each error")
    parser.add_option('--show-pep8', action='store_true',
                      help="show text of PEP 8 for each error")
    parser.add_option('--statistics', action='store_true',
                      help="count errors and warnings")
    parser.add_option('--count', action='store_true',
                      help="print total number of errors and warnings "
                        "to standard error and set exit code to 1 if "
                        "total is not null")
    parser.add_option('--benchmark', action='store_true',
                      help="measure processing speed")
    parser.add_option('--testsuite', metavar='dir',
                      help="run regression tests from dir")
    parser.add_option('--doctest', action='store_true',
                      help="run doctest on myself")
    options, args = parser.parse_args(arglist)
    if options.testsuite:
        args.append(options.testsuite)
    if not args and not options.doctest:
        parser.error('input not specified')
    options.prog = os.path.basename(sys.argv[0])
    options.exclude = options.exclude.split(',')
    for index in range(len(options.exclude)):
        options.exclude[index] = options.exclude[index].rstrip('/')
    if options.filename:
        options.filename = options.filename.split(',')
    if options.select:
        options.select = options.select.split(',')
    else:
        options.select = []
    if options.ignore:
        options.ignore = options.ignore.split(',')
    elif options.select:
        # Ignore all checks which are not explicitly selected
        options.ignore = ['']
    elif options.testsuite or options.doctest:
        # For doctest and testsuite, all checks are required
        options.ignore = []
    else:
        # The default choice: ignore controversial checks
        options.ignore = DEFAULT_IGNORE.split(',')
    options.physical_checks = find_checks('physical_line')
    options.logical_checks = find_checks('logical_line')
    options.counters = dict.fromkeys(BENCHMARK_KEYS, 0)
    options.messages = {}
    return options, args


def _main():
    """
    Parse options and run checks on Python source.
    """
    options, args = process_options()
    if options.doctest:
        import doctest
        doctest.testmod(verbose=options.verbose)
        selftest()
    if options.testsuite:
        runner = run_tests
    else:
        runner = input_file
    start_time = time.time()
    for path in args:
        if os.path.isdir(path):
            input_dir(path, runner=runner)
        elif not excluded(path):
            options.counters['files'] += 1
            runner(path)
    elapsed = time.time() - start_time
    if options.statistics:
        print_statistics()
    if options.benchmark:
        print_benchmark(elapsed)
    count = get_count()
    if count:
        if options.count:
            sys.stderr.write(str(count) + '\n')
        sys.exit(1)


if __name__ == '__main__':
    _main()
