import logging
import os
import re
import sys
import zlib

from rbtools.api.errors import APIError
from rbtools.clients import SCMClient, RepositoryInfo
from rbtools.utils.checks import check_gnu_diff, check_gnu_patch, check_install
from rbtools.utils.filesystem import make_tempfile, read_text_file
from rbtools.utils.process import die, execute

# This specific import is necessary to handle the paths for
# cygwin enabled machines.
if (sys.platform.startswith('win')
    or sys.platform.startswith('cygwin')):
    import ntpath as cpath
else:
    import posixpath as cpath


class ClearCaseClient(SCMClient):
    """
    A wrapper around the clearcase tool that fetches repository
    information and generates compatible diffs.
    This client assumes that cygwin is installed on windows.
    """
    viewtype = None
    HLINK_MERGE = re.compile(r'"Merge@.*?" <- ".*"')

    def __init__(self, **kwargs):
        super(ClearCaseClient, self).__init__(**kwargs)

        if self.options.exclude_files:
            logging.debug('excluding files, regexp pattern: %s',
                          self.options.exclude_files)
            self.exclude_re = re.compile(self.options.exclude_files)
        else:
            self.exclude_re = None

    def get_repository_info(self):
        """Returns information on the Clear Case repository.

        This will first check if the cleartool command is
        installed and in the path, and post-review was run
        from inside of the view.
        """
        if not check_install('cleartool help'):
            return None

        viewname = execute(["cleartool", "pwv", "-short"]).strip()
        if viewname.startswith('** NONE'):
            return None

        # Now that we know it's ClearCase, make sure we have GNU diff
        # installed, and error out if we don't.
        check_gnu_diff()

        # When the exclude merge option is enabled, make sure we have
        # GNU patch installed.
        if self.options.xmerge:
            check_gnu_patch()

        property_lines = execute(["cleartool", "lsview", "-full",
                                  "-properties", "-cview"],
                                 split_lines=True)
        for line in property_lines:
            properties = line.split(' ')
            if properties[0] == 'Properties:':
                # Determine the view type and check if it's supported.
                #
                # Specifically check if webview was listed in properties
                # because webview types also list the 'snapshot'
                # entry in properties.
                if 'webview' in properties:
                    die("Webviews are not supported. You can use post-review"
                        " only in dynamic or snapshot view.")
                if 'dynamic' in properties:
                    self.viewtype = 'dynamic'
                else:
                    self.viewtype = 'snapshot'

                break

        # Find current VOB's tag
        vobstag = execute(["cleartool", "describe", "-short", "vob:."],
                            ignore_errors=True).strip()
        if "Error: " in vobstag:
            die("To generate diff run post-review inside vob.")

        # From current working directory cut path to VOB.
        # VOB's tag contain backslash character before VOB's name.
        # I hope that first character of VOB's tag like '\new_proj'
        # won't be treat as new line character but two separate:
        # backslash and letter 'n'
        cwd = os.getcwd()
        base_path = cwd[:cwd.find(vobstag) + len(vobstag)]

        return ClearCaseRepositoryInfo(path=base_path,
                              base_path=base_path,
                              vobstag=vobstag,
                              supports_parent_diffs=False)

    def check_options(self):
        if ((self.options.revision_range or self.options.tracking)
            and self.viewtype != "dynamic"):
            die("To generate diff using parent branch or by passing revision "
                "ranges, you must use a dynamic view.")

    def _determine_version(self, version_path):
        """Determine numeric version of revision.

        CHECKEDOUT is marked as infinity to be treated
        always as highest possible version of file.
        CHECKEDOUT, in ClearCase, is something like HEAD.
        """
        branch, number = cpath.split(version_path)
        if number == 'CHECKEDOUT':
            return float('inf')
        return int(number)

    def _construct_extended_path(self, path, version):
        """Combine extended_path from path and version.

        CHECKEDOUT must be removed becasue this one version
        doesn't exists in MVFS (ClearCase dynamic view file
        system). Only way to get content of checked out file
        is to use filename only."""
        if not version or version.endswith('CHECKEDOUT'):
            return path

        return "%s@@%s" % (path, version)

    def _sanitize_branch_changeset(self, changeset):
        """Return changeset containing non-binary, branched file versions.

        Changeset contain only first and last version of file made on branch.
        """
        changelist = {}
        xpatchlist = {}

        for path, previous, current, hlinks in changeset:
            version_number = self._determine_version(current)

            if path not in changelist:
                changelist[path] = {
                    'highest': version_number,
                    'current': current,
                    'previous': previous
                }

            if self.options.xmerge and self.HLINK_MERGE.match(hlinks):
                if path not in xpatchlist:
                    xpatchlist[path] = {}

                diff = self._diff(
                    self._construct_extended_path(path, current),
                    self._construct_extended_path(path, previous),
                    unified=False)

                if diff:
                    xpatchlist[path][version_number] = diff

            if version_number == 0:
                # Previous version of 0 version on branch is base
                changelist[path]['previous'] = previous
            elif version_number > changelist[path]['highest']:
                changelist[path]['highest'] = version_number
                changelist[path]['current'] = current

        # Convert to list
        changeranges = []
        for path, version in changelist.iteritems():
            xpatches = []
            if path in xpatchlist:
                xpatches = [
                    xpatchlist[path][i]
                    for i in sorted(xpatchlist[path])
                ]

            changeranges.append(
                (self._construct_extended_path(path, version['previous']),
                 self._construct_extended_path(path, version['current']),
                 xpatches))

            if self.options.debug:
                logging.debug('adding changelist item: (%s, %s, %s)' % (
                    changeranges[-1][0], changeranges[-1][1],
                    len(changeranges[-1][2])))

        return changeranges

    def _sanitize_checkedout_changeset(self, changeset):
        """Return changeset containing non-binary, checkedout file
        versions."""

        changeranges = []
        for path, previous, current in changeset:
            changeranges.append(
                (self._construct_extended_path(path, previous),
                 self._construct_extended_path(path, current),
                 None)
            )

        return changeranges

    def _construct_changeset(self, output):
        changeset = []

        for info in output.splitlines():
            change = info.split('\t')
            if self.exclude_re and \
               self.exclude_re.search(change[0]):
                logging.debug('excluding %s from diff', change[0])
                continue
            changeset.append(change)

        return changeset

    def _content_diff(self, old_content, new_content, old_file,
                      new_file, unified=True):
        """Returns unified diff as a list of lines with no end lines,
        uses temp files. The input content should be a list of lines
        without end lines."""

        old_tmp = make_tempfile(content=os.linesep.join(old_content))
        new_tmp = make_tempfile(content=os.linesep.join(new_content))

        diff_cmd = ['diff']
        if unified:
            diff_cmd.append('-uN')
        diff_cmd.extend((old_tmp, new_tmp))

        dl = execute(diff_cmd, extra_ignore_errors=(1, 2),
                     translate_newlines=False, split_lines=False)

        eof_endl = dl.endswith('\n')
        dl = dl.splitlines()
        if eof_endl:
            dl.append('')

        try:
            os.unlink(old_tmp)
            os.unlink(new_tmp)
        except:
            pass

        if unified and dl and len(dl) > 1:
            # Because the modification time is for temporary files here
            # replacing it with headers without modification time.
            if dl[0].startswith('---') and dl[1].startswith('+++'):
                dl[0] = '--- %s\t' % old_file
                dl[1] = '+++ %s\t' % new_file

        return dl

    def _diff(self, old_file, new_file, xpatches=None, unified=True):
        """Calculate the diff.

        Content should be a list of strings with no endl. Supports
        exclude patches (list of list of strings with no endl). If the
        content is None, it is assumed that the file is binary. The file
        names (new_file and old_file) are only to be used as a header
        for a diff.

        Returns None if the files are different and binary. Otherwise
        returns a difference as a list of strings with no lineseps. The
        binary files which are equal also return an empty string."""

        old_content = None
        new_content = None

        # The content should have line endings removed from it!
        if cpath.isdir(new_file):
            # read directory content
            old_content = sorted(os.listdir(old_file)) + ['']
            new_content = sorted(os.listdir(new_file)) + ['']
        elif cpath.exists(new_file):
            # returns None for binary file
            old_content = read_text_file(old_file)
            new_content = read_text_file(new_file)
        else:
            logging.debug("File %s does not exist or access is denied."
                          % new_file)
            return None

        # check if binary files and if they differ
        if old_content is None or new_content is None:
            old_crc = zlib.crc32(open(old_file).read())
            new_crc = zlib.crc32(open(new_file).read())
            if old_crc != new_crc:
                return None
            else:
                return u''

        # check if we need to exclude anything from the diff
        if xpatches:
            for patch in reversed(xpatches):
                patched = self._patch(new_content, patch)
                if patched:
                    new_content = patched

        return self._content_diff(old_content, new_content, old_file,
                                  new_file, unified=unified)

    def _patch(self, content, patch):
        """Patch content with a patch. Returnes patched content.

        The content and the patch should be a list of lines with no
        endl."""

        content_file = make_tempfile(content=os.linesep.join(content))
        patch_file = make_tempfile(content=os.linesep.join(patch))
        reject_file = make_tempfile()
        output_file = make_tempfile()

        patch_cmd = ["patch", "-r", reject_file, "-o", output_file,
                     "-i", patch_file, content_file]

        output = execute(patch_cmd, extra_ignore_errors=(1,),
                         translate_newlines=False)

        if "FAILED at" in output:
            logging.debug("patching content FAILED:")
            logging.debug(output)

        patched = open(output_file).read()
        eof_endl = patched.endswith('\n')

        patched = patched.splitlines()
        if eof_endl:
            patched.append('')

        try:
            os.unlink(content_file)
            os.unlink(patch_file)
            os.unlink(reject_file)
            os.unlink(output_file)
        except:
            pass

        return patched

    def get_checkedout_changeset(self):
        """Return information about the checked out changeset.

        This function returns: kind of element, path to file,
        previews and current file version."""

        changeset = []
        # We ignore return code 1 in order to
        # omit files that Clear Case can't read.
        output = execute([
            "cleartool",
            "lscheckout",
            "-all",
            "-cview",
            "-me",
            "-fmt",
            r"%En\t%PVn\t%Vn\n"],
            extra_ignore_errors=(1,),
            with_errors=False)

        if output:
            changeset = self._construct_changeset(output)

        return self._sanitize_checkedout_changeset(changeset)

    def get_branch_changeset(self, branch):
        """Returns information about the versions changed on a branch.

        This takes into account the changes on the branch owned by the
        current user in all vobs of the current view."""

        changeset = []

        # We ignore return code 1 in order to
        # omit files that Clear Case can't read.
        if sys.platform.startswith('win'):
            CLEARCASE_XPN = '%CLEARCASE_XPN%'
        else:
            CLEARCASE_XPN = '$CLEARCASE_XPN'

        output = execute([
            "cleartool",
            "find",
            "-all",
            "-version",
            "brtype(%s)" % branch,
            "-exec",
            'cleartool descr -fmt ' \
            r'"%En\t%PVn\t%Vn\t%[hlink]p\n" ' \
            + CLEARCASE_XPN],
            extra_ignore_errors=(1,),
            with_errors=False)

        if output:
            changeset = self._construct_changeset(output)

        return self._sanitize_branch_changeset(changeset)

    def do_diff(self, changeset):
        """Generates a unified diff for all files in the changeset."""

        diff = []
        for old_file, new_file, xpatches in changeset:
            # We need oids of files to translate them to paths on
            # reviewboard repository
            old_oid = execute(["cleartool", "describe", "-fmt", "%On",
                               old_file])
            new_oid = execute(["cleartool", "describe", "-fmt", "%On",
                               new_file])

            dl = self._diff(old_file, new_file, xpatches=xpatches)
            oid_line = "==== %s %s ====" % (old_oid, new_oid)

            if dl is None:
                dl = [oid_line,
                    'Binary files %s and %s differ' % (old_file, new_file),
                    '']
            elif not dl:
                dl = [oid_line,
                    'File %s in your changeset is unmodified' % new_file,
                    '']
            else:
                dl.insert(2, oid_line)

            diff.append(os.linesep.join(dl))

        return (''.join(diff), None)

    def diff(self, files):
        """Performs a diff of the specified file and its previous version."""

        if self.options.tracking:
            changeset = self.get_branch_changeset(self.options.tracking)
        else:
            changeset = self.get_checkedout_changeset()

        return self.do_diff(changeset)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """Performs a diff between passed revisions or branch."""

        # Convert revision range to list of:
        # (previous version, current version) tuples
        revision_range = revision_range.split(';')
        changeset = zip(revision_range[0::2], revision_range[1::2])

        return (self.do_diff(changeset)[0], None)


class ClearCaseRepositoryInfo(RepositoryInfo):
    """
    A representation of a ClearCase source code repository. This version knows
    how to find a matching repository on the server even if the URLs differ.
    """

    def __init__(self, path, base_path, vobstag, supports_parent_diffs=False):
        RepositoryInfo.__init__(self, path, base_path,
                                supports_parent_diffs=supports_parent_diffs)
        self.vobstag = vobstag

    def find_server_repository_info(self, server):
        """
        The point of this function is to find a repository on the server that
        matches self, even if the paths aren't the same. (For example, if self
        uses an 'http' path, but the server uses a 'file' path for the same
        repository.) It does this by comparing VOB's name. If the
        repositories use the same path, you'll get back self, otherwise you'll
        get a different ClearCaseRepositoryInfo object (with a different path).
        """

        # Find VOB's family uuid based on VOB's tag
        uuid = self._get_vobs_uuid(self.vobstag)
        logging.debug("Repository's %s uuid is %r" % (self.vobstag, uuid))

        repositories = server.get_repositories()
        for repository in repositories:
            if repository['tool'] != 'ClearCase':
                continue

            info = self._get_repository_info(server, repository)

            if not info or uuid != info['uuid']:
                continue

            logging.debug('Matching repository uuid:%s with path:%s' % (uuid,
                          info['repopath']))
            return ClearCaseRepositoryInfo(info['repopath'],
                    info['repopath'], uuid)

        # We didn't found uuid but if version is >= 1.5.3
        # we can try to use VOB's name hoping it is better
        # than current VOB's path.
        if server.rb_version >= '1.5.3':
            self.path = cpath.split(self.vobstag)[1]

        # We didn't find a matching repository on the server.
        # We'll just return self and hope for the best.
        return self

    def _get_vobs_uuid(self, vobstag):
        """Return family uuid of VOB."""

        property_lines = execute(["cleartool", "lsvob", "-long", vobstag],
                                 split_lines=True)
        for line  in property_lines:
            if line.startswith('Vob family uuid:'):
                return  line.split(' ')[-1].rstrip()

    def _get_repository_info(self, server, repository):
        try:
            return server.get_repository_info(repository['id'])
        except APIError, e:
            # If the server couldn't fetch the repository info, it will return
            # code 210. Ignore those.
            # Other more serious errors should still be raised, though.
            if e.error_code == 210:
                return None

            raise e
