import os
import tempfile
import re

from rbtools.utils.process import die


TEMP_DIR = None
CONFIG_FILE = '.reviewboardrc'

tempfiles = []


def cleanup_tempfiles():
    for tmpfile in tempfiles:
        try:
            os.unlink(tmpfile)
        except:
            pass


def get_config_value(configs, name):
    for c in configs:
        if name in c:
            return c[name]

    return None


def load_config_files(homepath):
    """Loads data from .reviewboardrc files."""
    def _load_config(path):
        config = {
            'TREES': {},
        }

        filename = os.path.join(path, CONFIG_FILE)

        if os.path.exists(filename):
            try:
                execfile(filename, config)
            except SyntaxError, e:
                die('Syntax error in config file: %s\n'
                    'Line %i offset %i\n' % (filename, e.lineno, e.offset))

            return config

        return None

    configs = []

    for path in walk_parents(os.getcwd()):
        config = _load_config(path)

        if config:
            configs.append(config)

    return _load_config(homepath), configs


def make_tempfile(content=None):
    """
    Creates a temporary file and returns the path. The path is stored
    in an array for later cleanup.
    """
    fd, tmpfile = tempfile.mkstemp(dir=TEMP_DIR)

    if content:
        os.write(fd, content)

    os.close(fd)
    tempfiles.append(tmpfile)
    return tmpfile


def walk_parents(path):
    """
    Walks up the tree to the root directory.
    """
    while os.path.splitdrive(path)[1] != os.sep:
        yield path
        path = os.path.dirname(path)


def read_text_file(file, keepends=False):
    """Returns text file contents as a list of lines if the file is
    a text file.

    Returns None if the file is binary (contains 00 byte). By default
    will add an extra empty string at the end of the string list to
    indicate that the file has eol at its end. If eof eol is missing,
    the empty string wont be added."""

    fd = open(file, 'rb')
    content = ''

    try:
        chunksize = 1024
        while 1:
            chunk = fd.read(chunksize)
            if '\0' in chunk:
                return None

            if chunk == '':
                eof_endl = content.endswith('\n')
                content = content.splitlines(keepends)
                if not keepends and eof_endl:
                    content.append('')
                return content

            content += chunk
    finally:
        fd.close()


def diff_stats(diff):
    """Return some basic diff stats based on a view simple regular
    patters"""

    fls_count = 0
    ins_count = 0
    del_count = 0
    for line in diff.splitlines():
        fls_count += 1 if line.startswith('+++ ') else 0
        ins_count += 1 if line.startswith('+ ') else 0
        del_count += 1 if line.startswith('- ') else 0

    return (fls_count, ins_count, del_count)
