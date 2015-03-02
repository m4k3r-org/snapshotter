#!/usr/bin/env python
"""

A script for making incremental snapshot backups of directories using rsync.
See README.markdown for instructions.

"""
import datetime
import sys
import os
import subprocess
import optparse


class CalledProcessError(Exception):

    """Exception type that's raised if an external command fails."""

    def __init__(self, command, output, exit_value):
        super(CalledProcessError, self).__init__(
            output + " " + str(exit_value))
        self.command = command
        self.output = output
        self.exit_value = exit_value


class NoSuchCommandError(Exception):

    """Raised when trying to run an external command that doesn't exist."""

    def __init__(self, command, message):
        super(NoSuchCommandError, self).__init__(message)
        self.command = command


def _run(command):
    """Run the given command as a subprocess and return its output.

    This redirects the subprocess's stderr to stdout so the returned string
    should contain everything written to stdout and stderr together.

    :raises CalledProcessError: If running the command fails or the command
        exits with non-zero status. The command's stdout and stderr will be
        availabled as error.output, and its exit status as error.exit_value.

    :raises NoSuchCommandError: If the command doesn't exist.

    """
    try:
        return subprocess.check_output(command, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as err:
        raise CalledProcessError(
            ' '.join(command), err.output, err.returncode)
    except OSError as err:
        if err.errno == 2:
            raise NoSuchCommandError(' '.join(command), err.strerror)
        else:
            raise


def _rsync(source, dest, debug=False, exclude=None):
    """Run an rsync command as a subprocess.

    :raises CalledProcessError: if rsync exits with a non-zero exit value
    :raises NoSuchCommandError: if rsync is not installed in the expected
        location

    """
    # Make sure source ends with / because this affects how rsync behaves.
    if not source.endswith(os.sep):
        source += os.sep

    rsync_cmd = [
        "rsync",
        # Copy recursively and preserve times, permissions, symlinks, etc.
        '--archive',
        '--partial',
        # Keep partially transferred files if the transfer is interrupted.
        '--partial-dir=partially_transferred_files',
        '--one-file-system',  # Don't cross filesystem boundaries.
        '--delete',  # Delete extraneous files from dest dirs.
        '--delete-excluded',  # Also delete excluded files from dest dirs.
        '--itemize-changes',  # Output a change-summary for all updates.
        # Make hard-links to the previous snapshot, if any.
        '--link-dest=../latest.snapshot',
        '--human-readable',  # Output numbers in a human-readable format.
        '--quiet',  # Suppress non-error output messages.
        '--compress',  # Compress files during transfer.
        '--fuzzy',  # Look for basis files for any missing destination files.
        ]

    if os.path.isfile(os.path.expanduser("~/.snapshotter/excludes")):
        # Read exclude patterns from file.
        rsync_cmd.append('--exclude-from=$HOME/.snapshotter/excludes')

    if debug:
        rsync_cmd.append('--dry-run')

    if exclude is not None:
        for pattern in exclude:
            rsync_cmd.append("--exclude '%s'" % pattern)

    rsync_cmd.append(source)

    user, host, snapshots_root = _parse_path(dest)
    dest = ''
    if host is not None:
        if user is not None:
            dest += "%s@" % user
        dest += "%s:" % host
    dest += "%s/incomplete.snapshot" % snapshots_root
    rsync_cmd.append(dest)

    print(rsync_cmd)
    _run(rsync_cmd)


def _wrap_in_ssh(command, user, host):
    """Return the given command with ssh prepended to run it remotely.

    For example for ["mv", "source", "dest"] return
    ["ssh", "user@host", "mv", "source", "dest"].

    """
    if not host:
        # We aren't dealing with a remote destination so there's no need
        # to wrap the command in an ssh command.
        return command

    ssh_command = ["ssh"]
    host_part = ""
    if user is not None:
        host_part += "%s@" % user
    host_part += host
    ssh_command.append(host_part)
    ssh_command.extend(command)
    return ssh_command


def _move_incomplete_dir(snapshots_root, date, user=None, host=None,
                         debug=False):
    """Move the incomplete.snapshot dir to YYYY-MM-DDTHH_MM_SS.snapshot.

    If snapshots_root is a remote path move the directory remotely
    by running `ssh [user@]host mv ...`.

    """
    src = "%s/incomplete.snapshot" % snapshots_root
    dest = "%s/%s.snapshot" % (snapshots_root, date)
    mv_cmd = _wrap_in_ssh(["mv", src, dest], user, host)
    if not debug:
        print(mv_cmd)
        _run(mv_cmd)


def _rm(path, user=None, host=None):
    """Remove the given filesystem path.

    If path is a remote path remove it remotely by running
    `ssh [user@]host rm ...`.

    """
    command = _wrap_in_ssh(["rm", "-f", path], user, host)
    print(command)
    _run(command)


def _ln(target, link_path, user=None, host=None):
    """Create a symlink to the given target of the given link path.

    If link_path is a remote path then create the symlink remotely by running
    `ssh [user@]host ln -s ...`.

    """
    command = _wrap_in_ssh(["ln", "-s", target, link_path], user, host)
    print(command)
    _run(command)


def _update_latest_symlink(date, snapshots_root, user=None, host=None,
                           debug=False):
    """Update the latest.snapshot symlink to point to the new  snapshot.

    If snapshots_root is a remote directory then update the symlink remotely.

    """
    target = "%s.snapshot" % date
    link_name = "%s/latest.snapshot" % snapshots_root
    if not debug:
        _rm("%s/latest.snapshot" % snapshots_root, user, host)
        _ln(target, link_name, user, host)


def _datetime():
    """Return the current datetime as a string.

    We wrap datetime.datetime.now() instead of calling it directly to make
    it easy for tests to patch this funtion.

    """
    return datetime.datetime.now().strftime("%Y-%m-%dT%H_%M_%S")


def _is_remote(path):
    """Return True if the given path is a remote path, False otherwise.

    :param path: a local or remote path as would be used in an rsync SRC or
        DEST argument on the command-line

    """
    # If it has a : before the first / then it's a remote path.
    return ':' in path.split('/')[0]


def _parse_path(path):
    """Parse the given local or remote path and return its parts.


    :param path: a local or remote path as would be used in an rsync SRC or
        DEST argument on the command-line

    :returns: A 3-tuple (user, host, path) of the username, hostname and path
        parts of the given path. Both user and host may be None.
        user may be None while host is not None.

    For example:

        "seanh@mydomain.org:/path/to/backups" ->
            ("seanh", "mydomain.org", "/path/to/backups")

        "mydomain.org:/path/to/backups" ->
            (None, "mydomain.org", "/path/to/backups")

        "/path/to/backups" ->
            (None, None, "/path/to/backups")

    When user and host are both None, then relative paths will be expanded
    to absolute paths and ~ will be expanded to the path to the user's home
    directory:

        "~/path/to/backups" ->
            (None, None, "/home/seanh/path/to/backups")

    """
    if _is_remote(path):
        before_first_colon, after_first_colon = path.split(':', 1)
        if '@' in before_first_colon:
            user = before_first_colon.split('@')[0]
        else:
            user = None
        host = before_first_colon.split('@')[-1]
        path = after_first_colon
    else:
        user = None
        host = None
        path = os.path.abspath(os.path.expanduser(path))
    return user, host, path


def snapshot(source, dest, debug=False, exclude=None):
    """Make a new snapshot of source in dest.

    Make a new snapshot means:

    1. Run rsync with all the correct arguments (including the --link-dest arg
       to tell rsync to make hard-links to files that haven't changed since the
       previous snapshot)
    2. Then if rsync succeeds move the incomplete.snapshot directory to
       YYYY-MM-DDTHH_MM_SS.snapshot
    3. Then if that succeeds update the latest.snapshot symlink to point to the
       newly-created snapshot.

    Either source or dest can be a local path or a remote path
    (e.g. seanh@mydomain.org:Snapshots/Documents or just
    mydomain.org:Snapshots/Documents). Either can be a relative path and can
    contain ~ (which will be expanded to the path to the user's home
    directory).

    If dest if a remote path then ssh will be used to run mv, rm and ln to
    move the incomplete.snapshot directory and update the latest.snapshot
    symlink remotely.

    :param source: the path to the source directory to be backed up
    :type source: string

    :param dest: the path to the destination directory that will contain the
        snapshots
    :type dest: string

    :param debug: if True do a dry-run: pass the --dry-run argument to rsync
        so it doesn't actually copy any files, and don't actually move the
        incomplete.snapshot directory or update the latest.snapshot symlink
    :type debug: bool

    :raises CalledProcessError: if any of the commands fails or exits with a
        non-zero exit value

    :raises NoSuchCommandError: if any of the rsync, mv, ln or ssh commands
        aren't found at the expected location

    """
    date = _datetime()
    user, host, snapshots_root = _parse_path(dest)
    _rsync(source, dest, debug, exclude)
    _move_incomplete_dir(snapshots_root, date, user, host, debug)
    _update_latest_symlink(date, snapshots_root, user, host, debug)


class CommandLineArgumentsError(Exception):

    """The exception that's raised if the command-line args are invalid."""

    pass


def _parse_cli(args=None):
    """Parse the command-line arguments."""
    args = args if args is not None else sys.argv[1:]

    parser = optparse.OptionParser(usage="usage: %prog [options] SRC DEST")
    parser.add_option(
        '-d', '--debug', '-n', '--dry-run', dest='debug', action='store_true',
        default=False,
        help="Perform a trial-run with no changes made (pass the --dry-run "
             "option to rsync)")
    parser.add_option(
        '--exclude', type='string', dest='exclude', metavar="PATTERN",
        action='append',
        help="Exclude files matching PATTERN, e.g. --exclude '.git/*' (see "
             "the --exclude option in `man rsync`)")
    (options, args) = parser.parse_args(args)

    if len(args) != 2:
        raise CommandLineArgumentsError(parser.get_usage())

    src = args[0]
    dest = args[1]
    return (src, dest, options.debug, options.exclude)


def main():
    """Parse command-line args and pass them to snapshot().

    Also turns any known exceptions raised into clean sys.exit()s with a
    non-zero exit status and an error message printed, instead of stack traces.

    """
    try:
        src, dest, debug, exclude = _parse_cli()
    except CommandLineArgumentsError as err:
        sys.exit(err.message)
    try:
        snapshot(src, dest, debug, exclude)
    except (CalledProcessError, NoSuchCommandError) as err:
        sys.exit(err.message)


if __name__ == "__main__":
    main()
