import os
import subprocess
import sys

from rbtools.utils.process import die, execute


GNU_DIFF_WIN32_URL = 'http://gnuwin32.sourceforge.net/packages/diffutils.htm'
GNU_PATCH_WIN32_URL = 'http://gnuwin32.sourceforge.net/packages/patch.htm'


def check_install(command):
    """
    Try executing an external command and return a boolean indicating whether
    that command is installed or not.  The 'command' argument should be
    something that executes quickly, without hitting the network (for
    instance, 'svn help' or 'git --version').
    """
    try:
        subprocess.Popen(command.split(' '),
                         stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
        return True
    except OSError:
        return False


def check_gnu_diff():
    """Checks if GNU diff is installed, and informs the user if it's not."""
    has_gnu_diff = False

    try:
        result = execute(['diff', '--version'], ignore_errors=True)
        has_gnu_diff = 'GNU diffutils' in result
    except OSError:
        pass

    if not has_gnu_diff:
        sys.stderr.write('\n')
        sys.stderr.write('GNU diff is required to post this review.\n'
                         'Make sure it is installed and in the path.\n')
        sys.stderr.write('\n')

        if os.name == 'nt':
            sys.stderr.write('On Windows, you can install this from:\n')
            sys.stderr.write(GNU_DIFF_WIN32_URL)
            sys.stderr.write('\n')

        die()


def check_gnu_patch():
    """Checks if GNU patch is installed, and informs the user if it's not."""
    has_gnu_patch = False

    try:
        result = execute(['patch', '--version'], ignore_errors=True)
        has_gnu_patch = 'Free Software Foundation' in result
    except OSError:
        pass

    try:
        # SunOS has gpatch
        result = execute(['gpatch', '--version'], ignore_errors=True)
        has_gnu_patch = 'Free Software Foundation' in result
    except OSError:
        pass

    if not has_gnu_patch:
        sys.stderr.write('\n')
        sys.stderr.write('GNU patch is required to post this review.'
                         'Make sure it is installed and in the path.\n')
        sys.stderr.write('\n')

        if os.name == 'nt':
            sys.stderr.write('On Windows, you can install this from:\n')
            sys.stderr.write(GNU_PATCH_WIN32_URL)
            sys.stderr.write('\n')

        die()
