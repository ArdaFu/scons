#!/usr/bin/env python
#
# Copyright The SCons Foundation
#
"""runtest - wrapper script for running SCons tests

The SCons test suite consists of:

 * unit tests        - *Tests.py files from the SCons/ directory
 * end-to-end tests  - *.py files in the test/ directory that
                       require the custom SCons framework from
                       testing/framework.

This script adds SCons/ and testing/ directories to PYTHONPATH,
performs test discovery and processes tests according to options.
"""

from __future__ import annotations

import argparse
import itertools
import os
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from io import StringIO
from pathlib import Path, PurePath, PureWindowsPath
from queue import Queue

cwd = os.getcwd()
debug: str | None = None
scons: str | None = None
catch_output: bool = False
suppress_output: bool = False
script = PurePath(sys.argv[0]).name
usagestr = f"{script} [OPTIONS] [TEST ...]"
epilogstr = """\
Environment Variables:
  PRESERVE, PRESERVE_{PASS,FAIL,NO_RESULT}: preserve test subdirs
  TESTCMD_VERBOSE: turn on verbosity in TestCommand\
"""

# this is currently expected to be global, maybe refactor later?
unittests: list[str]

parser = argparse.ArgumentParser(
    usage=usagestr,
    epilog=epilogstr,
    allow_abbrev=False,
    formatter_class=argparse.RawDescriptionHelpFormatter,
)

# test selection options:
testsel = parser.add_argument_group(description='Test selection options:')
testsel.add_argument(metavar='TEST', nargs='*', dest='testlist',
                     help="Select TEST(s) (tests and/or directories) to run")
testlisting = testsel.add_mutually_exclusive_group()
testlisting.add_argument('-f', '--file', metavar='FILE', dest='testlistfile',
                     help="Select only tests in FILE")
testlisting.add_argument('-a', '--all', action='store_true',
                     help="Select all tests")
testlisting.add_argument('--retry', action='store_true',
                     help="Rerun the last failed tests in 'failed_tests.log'")
testsel.add_argument('--exclude-list', metavar="FILE", dest='excludelistfile',
                     help="""Exclude tests in FILE from current selection""")
testtype = testsel.add_mutually_exclusive_group()
testtype.add_argument('--e2e-only', action='store_true',
                      help="Exclude unit tests from selection")
testtype.add_argument('--unit-only', action='store_true',
                      help="Exclude end-to-end tests from selection")

# miscellaneous options
parser.add_argument('-b', '--baseline', metavar='BASE',
                    help="Run test scripts against baseline BASE.")
parser.add_argument('-d', '--debug', action='store_true',
                    help="Run test scripts under the Python debugger.")
parser.add_argument('-D', '--devmode', action='store_true',
                    help="Run tests in Python's development mode (Py3.7+ only).")
parser.add_argument('-e', '--external', action='store_true',
                    help="Run the script in external mode (for external Tools)")

def posint(arg: str) -> int:
    """Special positive-int type for argparse"""
    num = int(arg)
    if num < 0:
        raise argparse.ArgumentTypeError("JOBS value must not be negative")
    return num

parser.add_argument('-j', '--jobs', metavar='JOBS', default=1, type=posint,
                    help="Run tests in JOBS parallel jobs (0 for cpu_count).")
parser.add_argument('-l', '--list', action='store_true', dest='list_only',
                    help="List available tests and exit.")
parser.add_argument('-n', '--no-exec', action='store_false',
                    dest='execute_tests',
                    help="No execute, just print command lines.")
parser.add_argument('--nopipefiles', action='store_false',
                    dest='allow_pipe_files',
                    help="""Do not use the "file pipe" workaround for subprocess
                           for starting tests. See source code for warnings.""")
parser.add_argument('-P', '--python', metavar='PYTHON',
                    help="Use the specified Python interpreter.")
parser.add_argument('--quit-on-failure', action='store_true',
                    help="Quit on any test failure.")
parser.add_argument('--runner', metavar='CLASS',
                    help="Test runner class for unit tests.")
parser.add_argument('-X', dest='scons_exec', action='store_true',
                    help="Test script is executable, don't feed to Python.")
parser.add_argument('-x', '--exec', metavar="SCRIPT",
                    help="Test using SCRIPT as path to SCons.")
parser.add_argument('--faillog', dest='error_log', metavar="FILE",
                    default='failed_tests.log',
                    help="Log failed tests to FILE (enabled by default, "
                         "default file 'failed_tests.log')")
parser.add_argument('--no-faillog', dest='error_log',
                    action='store_const', const=None,
                    default='failed_tests.log',
                    help="Do not log failed tests to a file")

parser.add_argument('--no-ignore-skips', dest='dont_ignore_skips',
                    action='store_true',
                    default=False,
                    help="If any tests are skipped, exit status 2")

outctl = parser.add_argument_group(description='Output control options:')
outctl.add_argument('-k', '--no-progress', action='store_false',
                    dest='print_progress',
                    help="Suppress count and progress percentage messages.")
outctl.add_argument('--passed', action='store_true',
                    dest='print_passed_summary',
                    help="Summarize which tests passed.")
outctl.add_argument('-q', '--quiet', action='store_false',
                    dest='printcommand',
                    help="Don't print the test being executed.")
outctl.add_argument('-s', '--short-progress', action='store_true',
                    help="""Short progress, prints only the command line
                             and a progress percentage, no results.""")
outctl.add_argument('-t', '--time', action='store_true', dest='print_times',
                    help="Print test execution time.")
outctl.add_argument('--verbose', metavar='LEVEL', type=int, choices=range(1, 4),
                    help="""Set verbose level
                             (1=print executed commands,
                             2=print commands and non-zero output,
                             3=print commands and all output).""")
# maybe add?
# outctl.add_argument('--version', action='version', version=f'{script} 1.0')

logctl = parser.add_argument_group(description='Log control options:')
logctl.add_argument('-o', '--output', metavar='LOG', help="Save console output to LOG.")
logctl.add_argument(
    '--xml',
    metavar='XML',
    help="Save results to XML in SCons XML format (use - for stdout).",
)

# process args and handle a few specific cases:
args: argparse.Namespace = parser.parse_args()

# we can't do this check with an argparse exclusive group, since those
# only work with optional args, and the cmdline tests (args.testlist)
# are not optional args,
if args.testlist and (args.testlistfile or args.all or args.retry):
    sys.stderr.write(
        parser.format_usage()
        + "error: command line tests cannot be combined with -f/--file, -a/--all or --retry\n"
    )
    sys.exit(1)

if args.retry:
    args.testlistfile = 'failed_tests.log'

if args.testlistfile:
    # args.testlistfile changes from a string to a pathlib Path object
    try:
        ptest = Path(args.testlistfile)
        args.testlistfile = ptest.resolve(strict=True)
    except FileNotFoundError:
        sys.stderr.write(
            parser.format_usage()
            + f'error: -f/--file testlist file "{args.testlistfile}" not found\n'
        )
        sys.exit(1)

if args.excludelistfile:
    # args.excludelistfile changes from a string to a pathlib Path object
    try:
        pexcl = Path(args.excludelistfile)
        args.excludelistfile = pexcl.resolve(strict=True)
    except FileNotFoundError:
        sys.stderr.write(
            parser.format_usage()
            + f'error: --exclude-list file "{args.excludelistfile}" not found\n'
        )
        sys.exit(1)

if args.jobs == 0:
    try:
        # on Linux, check available rather than physical CPUs
        args.jobs = len(os.sched_getaffinity(0))
    except AttributeError:
        # Windows
        args.jobs = os.cpu_count()

# sanity check
if args.jobs == 0:
    sys.stderr.write(
        parser.format_usage()
        + "Unable to detect CPU count, give -j a non-zero value\n"
    )
    sys.exit(1)

if args.jobs > 1 or args.output:
    # 1. don't let tests write stdout/stderr directly if multi-job,
    # else outputs will interleave and be hard to read.
    # 2. If we're going to write a logfile, we also need to catch the output.
    catch_output = True

if not args.printcommand:
    suppress_output = catch_output = True

if args.verbose:
    os.environ['TESTCMD_VERBOSE'] = str(args.verbose)

if args.short_progress:
    args.print_progress = True
    suppress_output = catch_output = True

if args.debug:
    # TODO: add a way to pass a specific debugger
    debug = "pdb"

if args.exec:
    scons = args.exec

# --- setup stdout/stderr ---
class Unbuffered:
    def __init__(self, file):
        self.file = file

    def write(self, arg):
        self.file.write(arg)
        self.file.flush()

    def __getattr__(self, attr):
        return getattr(self.file, attr)

sys.stdout = Unbuffered(sys.stdout)
sys.stderr = Unbuffered(sys.stderr)

# possible alternative: switch to using print, and:
# print = functools.partial(print, flush)

if args.output:
    class Tee:
        def __init__(self, openfile, stream):
            self.file = openfile
            self.stream = stream

        def write(self, data):
            self.file.write(data)
            self.stream.write(data)

        def flush(self, data):
            self.file.flush(data)
            self.stream.flush(data)

    logfile = open(args.output, 'w')
    # this is not ideal: we monkeypatch stdout/stderr a second time
    # (already did for Unbuffered), so here we can't easily detect what
    # state we're in on closedown. Just hope it's okay...
    sys.stdout = Tee(logfile, sys.stdout)
    sys.stderr = Tee(logfile, sys.stderr)

# --- define helpers ----
if sys.platform == 'win32':
    # thanks to Eryk Sun for this recipe
    import ctypes

    shlwapi = ctypes.OleDLL('shlwapi')
    shlwapi.AssocQueryStringW.argtypes = (
        ctypes.c_ulong,  # flags
        ctypes.c_ulong,  # str
        ctypes.c_wchar_p,  # pszAssoc
        ctypes.c_wchar_p,  # pszExtra
        ctypes.c_wchar_p,  # pszOut
        ctypes.POINTER(ctypes.c_ulong),  # pcchOut
    )

    ASSOCF_NOTRUNCATE = 0x00000020
    ASSOCF_INIT_IGNOREUNKNOWN = 0x00000400
    ASSOCSTR_COMMAND = 1
    ASSOCSTR_EXECUTABLE = 2
    E_POINTER = ctypes.c_long(0x80004003).value

    def get_template_command(filetype, verb=None):
        """Return the association-related string for *filetype*"""
        flags = ASSOCF_INIT_IGNOREUNKNOWN | ASSOCF_NOTRUNCATE
        assoc_str = ASSOCSTR_COMMAND
        cch = ctypes.c_ulong(260)
        while True:
            buf = (ctypes.c_wchar * cch.value)()
            try:
                shlwapi.AssocQueryStringW(
                    flags, assoc_str, filetype, verb, buf, ctypes.byref(cch)
                )
            except OSError as e:
                if e.winerror != E_POINTER:
                    raise
                continue
            break
        return buf.value


if not catch_output:
    # Without any output suppressed, we let the subprocess
    # write its stuff freely to stdout/stderr.

    def spawn_it(command_args, env):
        cp = subprocess.run(command_args, shell=False, env=env)
        return cp.stdout, cp.stderr, cp.returncode

else:
    # Else, we catch the output of both pipes...
    if args.allow_pipe_files:
        # The subprocess.Popen() suffers from a well-known
        # problem. Data for stdout/stderr is read into a
        # memory buffer of fixed size, 65K which is not very much.
        # When it fills up, it simply stops letting the child process
        # write to it. The child will then sit and patiently wait to
        # be able to write the rest of its output. Hang!
        # In order to work around this, we follow a suggestion
        # by Anders Pearson in
        #   https://thraxil.org/users/anders/posts/2008/03/13/Subprocess-Hanging-PIPE-is-your-enemy/
        # and pass temp file objects to Popen() instead of the ubiquitous
        # subprocess.PIPE.

        def spawn_it(command_args, env):
            # Create temporary files
            tmp_stdout = tempfile.TemporaryFile(mode='w+t')
            tmp_stderr = tempfile.TemporaryFile(mode='w+t')
            # Start subprocess...
            cp = subprocess.run(
                command_args,
                stdout=tmp_stdout,
                stderr=tmp_stderr,
                shell=False,
                env=env,
            )

            try:
                # Rewind to start of files
                tmp_stdout.seek(0)
                tmp_stderr.seek(0)
                # Read output
                spawned_stdout = tmp_stdout.read()
                spawned_stderr = tmp_stderr.read()
            finally:
                # Remove temp files by closing them
                tmp_stdout.close()
                tmp_stderr.close()

            # Return values
            return spawned_stderr, spawned_stdout, cp.returncode

    else:
        # We get here only if the user gave the '--nopipefiles'
        # option, meaning the "temp file" approach for
        # subprocess.communicate() above shouldn't be used.
        # He hopefully knows what he's doing, but again we have a
        # potential deadlock situation in the following code:
        #   If the subprocess writes a lot of data to its stderr,
        #   the pipe will fill up (nobody's reading it yet) and the
        #   subprocess will wait for someone to read it.
        #   But the parent process is trying to read from stdin
        #   (but the subprocess isn't writing anything there).
        #   Hence a deadlock.
        # Be dragons here! Better don't use this!

        def spawn_it(command_args, env):
            cp = subprocess.run(
                command_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=env,
            )
            return cp.stdout, cp.stderr, cp.returncode


class RuntestBase(ABC):
    """ Base class for tests """
    _ids = itertools.count(1)  # to geenerate test # automatically

    def __init__(self, path, spe=None):
        self.path = str(path)
        self.testno = next(self._ids)
        self.stdout = self.stderr = self.status = None
        self.abspath = path.absolute()
        self.command_args = []
        self.command_str = ""
        self.test_time = self.total_time = 0
        if spe:
            for d in spe:
                f = os.path.join(d, path)
                if os.path.isfile(f):
                    self.abspath = f
                    break

    @abstractmethod
    def execute(self, env):
        pass


class SystemExecutor(RuntestBase):
    """ Test class for tests executed with spawn_it() """
    def execute(self, env):
        self.stderr, self.stdout, s = spawn_it(self.command_args, env)
        self.status = s
        if s < 0 or s > 2 and s != 5:
            sys.stdout.write("Unexpected exit status %d\n" % s)


class PopenExecutor(RuntestBase):
    """ Test class for tests executed with Popen

    A bit of a misnomer as the Popen call is now wrapped
    by calling subprocess.run (behind the covers uses Popen.
    Very similar to SystemExecutor, but doesn't allow for not catching
    the output).
    """
    # For an explanation of the following 'if ... else'
    # and the 'allow_pipe_files' option, please check out the
    # definition of spawn_it() above.
    if args.allow_pipe_files:

        def execute(self, env) -> None:
            # Create temporary files
            tmp_stdout = tempfile.TemporaryFile(mode='w+t')
            tmp_stderr = tempfile.TemporaryFile(mode='w+t')
            # Start subprocess...
            cp = subprocess.run(
                self.command_args,
                stdout=tmp_stdout,
                stderr=tmp_stderr,
                shell=False,
                env=env,
                check=False,
            )
            self.status = cp.returncode

            try:
                # Rewind to start of files
                tmp_stdout.seek(0)
                tmp_stderr.seek(0)
                # Read output
                self.stdout = tmp_stdout.read()
                self.stderr = tmp_stderr.read()
            finally:
                # Remove temp files by closing them
                tmp_stdout.close()
                tmp_stderr.close()
    else:

        def execute(self, env) -> None:
            cp = subprocess.run(
                self.command_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=env,
                check=False,
            )
            self.status, self.stdout, self.stderr = cp.returncode, cp.stdout, cp.stderr

class XML(PopenExecutor):
    """ Test class for tests that will output in scons xml """
    @staticmethod
    def header(f):
        f.write('  <results>\n')

    def write(self, f):
        f.write('    <test>\n')
        f.write('      <file_name>%s</file_name>\n' % self.path)
        f.write('      <command_line>%s</command_line>\n' % self.command_str)
        f.write('      <exit_status>%s</exit_status>\n' % self.status)
        f.write('      <stdout>%s</stdout>\n' % self.stdout)
        f.write('      <stderr>%s</stderr>\n' % self.stderr)
        f.write('      <time>%.1f</time>\n' % self.test_time)
        f.write('    </test>\n')

    def footer(self, f):
        f.write('  <time>%.1f</time>\n' % self.total_time)
        f.write('  </results>\n')

if args.xml:
    Test = XML
else:
    Test = SystemExecutor

# --- start processing ---

if not args.baseline or args.baseline == '.':
    baseline = cwd
elif args.baseline == '-':
    sys.stderr.write(
        "'baseline' logic used to checkout from svn. It has been removed. "
        "If you used this, please let us know on devel mailing list, "
        "IRC, or discord server\n"
    )
    sys.exit(-1)
else:
    baseline = args.baseline
scons_runtest_dir = baseline

if not args.external:
    scons_script_dir = os.path.join(baseline, 'scripts')
    scons_tools_dir = os.path.join(baseline, 'bin')
    scons_lib_dir = baseline
else:
    scons_script_dir = ''
    scons_tools_dir = ''
    scons_lib_dir = ''

testenv = {
    'SCONS_RUNTEST_DIR': scons_runtest_dir,
    'SCONS_TOOLS_DIR': scons_tools_dir,
    'SCONS_SCRIPT_DIR': scons_script_dir,
    'SCONS_CWD': cwd,
}

if scons:
    # Let the version of SCons that the -x option pointed to find
    # its own modules.
    testenv['SCONS'] = scons
elif scons_lib_dir:
    # Because SCons is really aggressive about finding its modules,
    # it sometimes finds SCons modules elsewhere on the system.
    # This forces SCons to use the modules that are being tested.
    testenv['SCONS_LIB_DIR'] = scons_lib_dir

if args.scons_exec:
    testenv['SCONS_EXEC'] = '1'

if args.external:
    testenv['SCONS_EXTERNAL_TEST'] = '1'

# Insert scons path and path for testing framework to PYTHONPATH
scriptpath = os.path.dirname(os.path.realpath(__file__))
frameworkpath = os.path.join(scriptpath, 'testing', 'framework')
testenv['PYTHONPATH'] = os.pathsep.join((scons_lib_dir, frameworkpath))
pythonpath = os.environ.get('PYTHONPATH')
if pythonpath:
    testenv['PYTHONPATH'] = testenv['PYTHONPATH'] + os.pathsep + pythonpath

if sys.platform == 'win32':
    # Windows doesn't support "shebang" lines directly (the Python launcher
    # and Windows Store version do, but you have to get them launched first)
    # so to directly launch a script we depend on an assoc for .py to work.
    # Some systems may have none, and in some cases IDE programs take over
    # the assoc.  Detect this so the small number of tests affected can skip.
    try:
        python_assoc = get_template_command('.py')
    except OSError:
        python_assoc = None
    if not python_assoc or "py" not in python_assoc:
        testenv['SCONS_NO_DIRECT_SCRIPT'] = '1'

os.environ.update(testenv)

# Clear _JAVA_OPTIONS which java tools output to stderr when run breaking tests
if '_JAVA_OPTIONS' in os.environ:
    del os.environ['_JAVA_OPTIONS']


# ---[ Test Discovery ]------------------------------------
# This section determines which tests to run based on three
# mutually exclusive options:
# 1. Reading test paths from a testlist file (--file or --retry option)
# 2. Using test paths given as command line arguments
# 3. Automatically finding all tests (--all option)
#
# Test paths can specify either individual test files, or directories to
# scan for tests. The following test types are recognized:
#
# - Unit tests: Files ending in 'Tests.py' under the 'SCons' directory
# - End-to-end tests: Python scripts (*.py) under the 'test' directory
# - External tests: End-to-end tests in paths containing a 'test'
#   component (not expected to be local)
#
# find_unit_tests() and find_e2e_tests() perform the directory scanning.
#
# After the initial test list is built, any test exclusions specified via
# --exclude-list are applied to produce the final test set.

def scanlist(testfile):
    """ Process a testlist file """
    data = StringIO(testfile.read_text())
    tests = [t.strip() for t in data.readlines() if not t.startswith('#')]
    # in order to allow scanned lists to work whether they use forward or
    # backward slashes, on non-Windows first create the object as a
    # PureWindowsPath which accepts either, then use that to make a Path
    # object for use in comparisons like "if file in scanned_list".
    if sys.platform == 'win32':
        return [Path(t) for t in tests if t]
    else:
        return [Path(PureWindowsPath(t).as_posix()) for t in tests if t]


def find_unit_tests(directory):
    """ Look for unit tests """
    result = []
    for dirpath, _, filenames in os.walk(directory):
        # Skip folders containing a sconstest.skip file
        if 'sconstest.skip' in filenames:
            continue
        for fname in filenames:
            if fname.endswith("Tests.py"):
                result.append(Path(dirpath, fname))

    return sorted(result)


def find_e2e_tests(directory):
    """ Look for end-to-end tests """
    result = []
    for dirpath, _, filenames in os.walk(directory):
        # Skip folders containing a sconstest.skip file
        if 'sconstest.skip' in filenames:
            continue

        # Gather up the data from any exclude lists
        excludes = []
        if ".exclude_tests" in filenames:
            excludefile = Path(dirpath, ".exclude_tests").resolve()
            excludes = scanlist(excludefile)

        for fname in filenames:
            if fname.endswith(".py") and Path(fname) not in excludes:
                result.append(Path(dirpath, fname))

    return sorted(result)


# Initial test selection:
unittests = []
endtests = []
if args.testlistfile:
    tests = scanlist(args.testlistfile)
else:
    testpaths = []
    if args.all:  # -a flag
        testpaths = [Path('SCons'), Path('test')]
    elif args.testlist:  # paths given on cmdline
        if sys.platform == 'win32':
            testpaths = [Path(t) for t in args.testlist]
        else:
            testpaths = [Path(PureWindowsPath(t).as_posix()) for t in args.testlist]

    for path in testpaths:
        # Clean up path removing leading ./ or .\
        name = str(path)
        if name.startswith('.') and name[1] in (os.sep, os.altsep):
            path = path.with_name(name[2:])

        if path.exists():
            if path.is_dir():
                if path.parts[0] == "SCons" or path.parts[0] == "testing":
                    unittests.extend(find_unit_tests(path))
                elif path.parts[0] == 'test':
                    endtests.extend(find_e2e_tests(path))
                elif args.external and 'test' in path.parts:
                    endtests.extend(find_e2e_tests(path))
            else:
                if path.match("*Tests.py"):
                    unittests.append(path)
                elif path.match("*.py"):
                    endtests.append(path)

    tests = sorted(unittests + endtests)

# Remove exclusions:
if args.e2e_only:
    tests = [t for t in tests if not t.match("*Tests.py")]
if args.unit_only:
    tests = [t for t in tests if t.match("*Tests.py")]
if args.excludelistfile:
    excludetests = scanlist(args.excludelistfile)
    tests = [t for t in tests if t not in excludetests]

# did we end up with any tests?
if not tests:
    sys.stderr.write(parser.format_usage() + """
error: no tests matching the specification were found.
       See "Test selection options" in the help for details on
       how to specify and/or exclude tests.
""")
    sys.exit(1)

# ---[ Test Processing ]-----------------------------------
tests = [Test(t) for t in tests]

if args.list_only:
    for t in tests:
        print(t.path)
    sys.exit(0)

if not args.python:
    if os.name == 'java':
        args.python = os.path.join(sys.prefix, 'jython')
    else:
        args.python = sys.executable
os.environ["python_executable"] = args.python

if args.print_times:

    def print_time(fmt, tm):
        print(fmt % tm)

else:

    def print_time(fmt, tm):
        pass

time_func = time.perf_counter
total_start_time = time_func()
total_num_tests = len(tests)


def log_result(t, io_lock=None):
    """ log the result of a test.

    "log" in this case means writing to stdout. Since we might be
    called from from any of several different threads (multi-job run),
    we need to lock access to the log to avoid interleaving. The same
    would apply if output was a file.

    Args:
        t (Test): (completed) testcase instance
        io_lock (threading.lock): (optional) lock to use
    """

    # there is no lock in single-job run, which includes
    # running test/runtest tests from multi-job run, so check.
    if io_lock:
        io_lock.acquire()
    try:
        if suppress_output or catch_output:
            sys.stdout.write(t.headline)
        if not suppress_output:
            if t.stdout:
                print(t.stdout)
            if t.stderr:
                print(t.stderr)
        print_time("Test execution time: %.1f seconds", t.test_time)
    finally:
        if io_lock:
            io_lock.release()

    if args.quit_on_failure and t.status == 1:
        print("Exiting due to error")
        print(t.status)
        sys.exit(1)


def run_test(t, io_lock=None, run_async=True):
    """ Run a testcase.

    Builds the command line to give to execute().
    Also the best place to record some information that will be
    used in output, which in some conditions is printed here.

    Args:
        t (Test): testcase instance
        io_lock (threading.Lock): (optional) lock to use
        run_async (bool): whether to run asynchronously
    """

    t.headline = ""
    command_args = []
    if debug:
        command_args.extend(['-m', debug])
    if args.devmode:
        command_args.append('-X dev')
    command_args.append(t.path)
    if args.runner and t.path in unittests:
        # For example --runner TestUnit.TAPTestRunner
        command_args.append('--runner ' + args.runner)
    t.command_args = [args.python] + command_args
    t.command_str = " ".join(t.command_args)
    if args.printcommand:
        if args.print_progress:
            t.headline += (
                f"{t.testno}/{total_num_tests} "
                f"({t.testno / total_num_tests:7.2%}) {t.command_str}\n"
            )
        else:
            t.headline += t.command_str + "\n"
    if not suppress_output and not catch_output:
        # defer printing the headline until test is done
        sys.stdout.write(t.headline)
    head, _ = os.path.split(t.abspath)
    fixture_dirs = []
    if head:
        fixture_dirs.append(head)
    fixture_dirs.append(os.path.join(scriptpath, 'test', 'fixture'))

    # Set the list of fixture dirs directly in the environment. Just putting
    # it in os.environ and spawning the process is racy. Make it reliable by
    # overriding the environment passed to execute().
    env = dict(os.environ)
    env['FIXTURE_DIRS'] = os.pathsep.join(fixture_dirs)

    test_start_time = time_func()
    if args.execute_tests:
        t.execute(env)

    t.test_time = time_func() - test_start_time
    log_result(t, io_lock=io_lock)


class RunTest(threading.Thread):
    """Test Runner thread.

    One will be created for each job in multi-job mode
    """

    def __init__(self, queue=None, io_lock=None, group=None, target=None, name=None):
        super().__init__(group=group, target=target, name=name)
        self.queue = queue
        self.io_lock = io_lock

    def run(self):
        for t in iter(self.queue.get, None):
            run_test(t, io_lock=self.io_lock, run_async=True)
            self.queue.task_done()

if args.jobs > 1:
    print("Running tests using %d jobs" % args.jobs)
    testq = Queue()
    for t in tests:
        testq.put(t)
    testlock = threading.Lock()
    # Start worker threads to consume the queue
    threads = [RunTest(queue=testq, io_lock=testlock) for _ in range(args.jobs)]
    for t in threads:
        t.daemon = True
        t.start()
    # wait on the queue rather than the individual threads
    testq.join()
else:
    for t in tests:
        run_test(t, io_lock=None, run_async=False)

# --- all tests are complete by the time we get here ---
if tests:
    tests[0].total_time = time_func() - total_start_time
    print_time("Total execution time for all tests: %.1f seconds", tests[0].total_time)

passed = [t for t in tests if t.status == 0]
fail = [t for t in tests if t.status == 1]
no_result = [t for t in tests if t.status == 2]

# print summaries, but only if multiple tests were run
if len(tests) != 1 and args.execute_tests:
    if passed and args.print_passed_summary:
        if len(passed) == 1:
            sys.stdout.write("\nPassed the following test:\n")
        else:
            sys.stdout.write("\nPassed the following %d tests:\n" % len(passed))
        paths = [x.path for x in passed]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")
    if fail:
        if len(fail) == 1:
            sys.stdout.write("\nFailed the following test:\n")
        else:
            sys.stdout.write("\nFailed the following %d tests:\n" % len(fail))
        paths = [x.path for x in fail]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")
    if no_result:
        if len(no_result) == 1:
            sys.stdout.write("\nNO RESULT from the following test:\n")
        else:
            sys.stdout.write("\nNO RESULT from the following %d tests:\n" % len(no_result))
        paths = [x.path for x in no_result]
        sys.stdout.write("\t" + "\n\t".join(paths) + "\n")

# save the fails to a file
if args.error_log:
    with open(args.error_log, "w") as f:
        if fail:
            paths = [x.path for x in fail]
            for test in paths:
                print(test, file=f)
        # if there are no fails, file will be cleared

if args.xml:
    if args.output == '-':
        f = sys.stdout
    else:
        f = open(args.xml, 'w')
    tests[0].header(f)
    #f.write("test_result = [\n")
    for t in tests:
        t.write(f)
    tests[0].footer(f)
    #f.write("];\n")
    if args.output != '-':
        f.close()

if args.output:
    if isinstance(sys.stdout, Tee):
        sys.stdout.file.close()
    if isinstance(sys.stderr, Tee):
        sys.stderr.file.close()

if fail:
    sys.exit(1)
elif no_result and args.dont_ignore_skips:
    # if no fails, but skips were found
    sys.exit(2)
else:
    sys.exit(0)

# Local Variables:
# tab-width:4
# indent-tabs-mode:nil
# End:
# vim: set expandtab tabstop=4 shiftwidth=4:
