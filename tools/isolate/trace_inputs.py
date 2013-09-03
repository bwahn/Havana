#!/usr/bin/env python
# coding=utf-8
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Runs strace or dtrace on a test and processes the logs to extract the
dependencies from the source tree.

Automatically extracts directories where all the files are used to make the
dependencies list more compact.
"""

import codecs
import csv
import logging
import optparse
import os
import posixpath
import re
import subprocess
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(BASE_DIR))

KEY_TRACKED = 'isolate_dependency_tracked'
KEY_UNTRACKED = 'isolate_dependency_untracked'


if sys.platform == 'win32':
  from ctypes.wintypes import create_unicode_buffer
  from ctypes.wintypes import windll, FormatError  # pylint: disable=E0611
  from ctypes.wintypes import GetLastError  # pylint: disable=E0611


  def QueryDosDevice(drive_letter):
    """Returns the Windows 'native' path for a DOS drive letter."""
    assert re.match(r'^[a-zA-Z]:$', drive_letter), drive_letter
    # Guesswork. QueryDosDeviceW never returns the required number of bytes.
    chars = 1024
    drive_letter = unicode(drive_letter)
    p = create_unicode_buffer(chars)
    if 0 == windll.kernel32.QueryDosDeviceW(drive_letter, p, chars):
      err = GetLastError()
      if err:
        # pylint: disable=E0602
        raise WindowsError(
            err,
            'QueryDosDevice(%s): %s (%d)' % (
              str(drive_letter), FormatError(err), err))
    return p.value


  def GetShortPathName(long_path):
    """Returns the Windows short path equivalent for a 'long' path."""
    long_path = unicode(long_path)
    chars = windll.kernel32.GetShortPathNameW(long_path, None, 0)
    if chars:
      p = create_unicode_buffer(chars)
      if windll.kernel32.GetShortPathNameW(long_path, p, chars):
        return p.value

    err = GetLastError()
    if err:
      # pylint: disable=E0602
      raise WindowsError(
          err,
          'GetShortPathName(%s): %s (%d)' % (
            str(long_path), FormatError(err), err))


  def get_current_encoding():
    """Returns the 'ANSI' code page associated to the process."""
    return 'cp%d' % int(windll.kernel32.GetACP())


  class DosDriveMap(object):
    """Maps \Device\HarddiskVolumeN to N: on Windows."""
    # Keep one global cache.
    _MAPPING = {}

    def __init__(self):
      if not self._MAPPING:
        for letter in (chr(l) for l in xrange(ord('C'), ord('Z')+1)):
          try:
            letter = '%s:' % letter
            mapped = QueryDosDevice(letter)
            # It can happen. Assert until we see it happens in the wild. In
            # practice, prefer the lower drive letter.
            assert mapped not in self._MAPPING
            if mapped not in self._MAPPING:
              self._MAPPING[mapped] = letter
          except WindowsError:  # pylint: disable=E0602
            pass

    def to_dos(self, path):
      """Converts a native NT path to DOS path."""
      m = re.match(r'(^\\Device\\[a-zA-Z0-9]+)(\\.*)?$', path)
      if not m or m.group(1) not in self._MAPPING:
        assert False, path
      drive = self._MAPPING[m.group(1)]
      if not m.group(2):
        return drive
      return drive + m.group(2)


def get_flavor():
  """Returns the system default flavor. Copied from gyp/pylib/gyp/common.py."""
  flavors = {
    'cygwin': 'win',
    'win32': 'win',
    'darwin': 'mac',
    'sunos5': 'solaris',
    'freebsd7': 'freebsd',
    'freebsd8': 'freebsd',
  }
  return flavors.get(sys.platform, 'linux')


def isEnabledFor(level):
  return logging.getLogger().isEnabledFor(level)


def fix_python_path(cmd):
  """Returns the fixed command line to call the right python executable."""
  out = cmd[:]
  if out[0] == 'python':
    out[0] = sys.executable
  elif out[0].endswith('.py'):
    out.insert(0, sys.executable)
  return out


def posix_relpath(path, root):
  """posix.relpath() that keeps trailing slash."""
  out = posixpath.relpath(path, root)
  if path.endswith('/'):
    out += '/'
  return out


class Strace(object):
  """strace implies linux."""
  IGNORED = (
    '/bin',
    '/dev',
    '/etc',
    '/lib',
    '/proc',
    '/sys',
    '/tmp',
    '/usr',
    '/var',
  )

  class _Context(object):
    """Processes a strace log line and keeps the list of existent and non
    existent files accessed.

    Ignores directories.
    """
    # This is the most common format. pid function(args) = result
    RE_HEADER = re.compile(r'^(\d+)\s+([^\(]+)\((.+?)\)\s+= (.+)$')
    # An interrupted function call, only grab the minimal header.
    RE_UNFINISHED = re.compile(r'^(\d+)\s+([^\(]+).*$')
    UNFINISHED = ' <unfinished ...>'
    # A resumed function call.
    RE_RESUMED = re.compile(r'^(\d+)\s+<\.\.\. ([^ ]+) resumed> (.+)$')
    # A process received a signal.
    RE_SIGNAL = re.compile(r'^\d+\s+--- SIG[A-Z]+ .+ ---')
    # A process didn't handle a signal.
    RE_KILLED = re.compile(r'^(\d+)\s+\+\+\+ killed by ([A-Z]+) \+\+\+$')
    # A call was canceled.
    RE_UNAVAILABLE = re.compile(r'\)\s+= \? <unavailable>$')

    # Arguments parsing.
    RE_CHDIR = re.compile(r'^\"(.+?)\"$')
    RE_EXECVE = re.compile(r'^\"(.+?)\", \[.+?\], \[.+?\]$')
    RE_OPEN2 = re.compile(r'^\"(.*?)\", ([A-Z\_\|]+)$')
    RE_OPEN3 = re.compile(r'^\"(.*?)\", ([A-Z\_\|]+), (\d+)$')
    RE_RENAME = re.compile(r'^\"(.+?)\", \"(.+?)\"$')

    def __init__(self, blacklist):
      self._cwd = {}
      self.blacklist = blacklist
      self.files = set()
      self.non_existent = set()
      # Key is a tuple(pid, function name)
      self._pending_calls = {}

    @classmethod
    def traces(cls):
      prefix = 'handle_'
      return [i[len(prefix):] for i in dir(cls) if i.startswith(prefix)]

    def on_line(self, line):
      line = line.strip()
      if self.RE_SIGNAL.match(line):
        # Ignore signals.
        return

      m = self.RE_KILLED.match(line)
      if m:
        self.handle_exit_group(int(m.group(1)), m.group(2), None, None)
        return

      if line.endswith(self.UNFINISHED):
        line = line[:-len(self.UNFINISHED)]
        m = self.RE_UNFINISHED.match(line)
        assert m, line
        self._pending_calls[(m.group(1), m.group(2))] = line
        return

      m = self.RE_UNAVAILABLE.match(line)
      if m:
        # This usually means a process was killed and a pending call was
        # canceled.
        # TODO(maruel): Look up the last exit_group() trace just above and make
        # sure any self._pending_calls[(pid, anything)] is properly flushed.
        return

      m = self.RE_RESUMED.match(line)
      if m:
        pending = self._pending_calls.pop((m.group(1), m.group(2)))
        # Reconstruct the line.
        line = pending + m.group(3)

      m = self.RE_HEADER.match(line)
      assert m, line
      return getattr(self, 'handle_%s' % m.group(2))(
          int(m.group(1)),
          m.group(2),
          m.group(3),
          m.group(4))

    def handle_chdir(self, pid, _function, args, result):
      """Updates cwd."""
      if result.startswith('0'):
        cwd = self.RE_CHDIR.match(args).group(1)
        if not cwd.startswith('/'):
          cwd2 = os.path.join(self._cwd[pid], cwd)
          logging.debug('handle_chdir(%d, %s) -> %s' % (pid, cwd, cwd2))
          self._cwd[pid] = cwd2
        else:
          logging.debug('handle_chdir(%d, %s)' % (pid, cwd))
          self._cwd[pid] = cwd
      else:
        assert False, 'Unexecpected fail: %s' % result

    def handle_clone(self, pid, _function, _args, result):
      """Transfers cwd."""
      if result == '? ERESTARTNOINTR (To be restarted)':
        return
      self._cwd[int(result)] = self._cwd[pid]

    def handle_execve(self, pid, _function, args, result):
      self._handle_file(pid, self.RE_EXECVE.match(args).group(1), result)

    def handle_exit_group(self, pid, _function, _args, _result):
      """Removes cwd."""
      del self._cwd[pid]

    @staticmethod
    def handle_fork(_pid, _function, args, result):
      assert False, (args, result)

    def handle_open(self, pid, _function, args, result):
      args = (self.RE_OPEN3.match(args) or self.RE_OPEN2.match(args)).groups()
      if 'O_DIRECTORY' in args[1]:
        return
      self._handle_file(pid, args[0], result)

    def handle_rename(self, pid, _function, args, result):
      args = self.RE_RENAME.match(args).groups()
      self._handle_file(pid, args[0], result)
      self._handle_file(pid, args[1], result)

    @staticmethod
    def handle_stat64(_pid, _function, args, result):
      assert False, (args, result)

    @staticmethod
    def handle_vfork(_pid, _function, args, result):
      assert False, (args, result)

    def _handle_file(self, pid, filepath, result):
      if result.startswith('-1'):
        return
      old_filepath = filepath
      if not filepath.startswith('/'):
        filepath = os.path.join(self._cwd[pid], filepath)
      if self.blacklist(filepath):
        return
      if old_filepath != filepath:
        logging.debug(
            '_handle_file(%d, %s) -> %s' % (pid, old_filepath, filepath))
      else:
        logging.debug('_handle_file(%d, %s)' % (pid, filepath))
      if filepath not in self.files and filepath not in self.non_existent:
        if os.path.isfile(filepath):
          self.files.add(filepath)
        else:
          self.non_existent.add(filepath)

  @classmethod
  def gen_trace(cls, cmd, cwd, logname):
    """Runs strace on an executable."""
    logging.info('gen_trace(%s, %s, %s)' % (cmd, cwd, logname))
    silent = not isEnabledFor(logging.INFO)
    stdout = stderr = None
    if silent:
      stdout = stderr = subprocess.PIPE
    traces = ','.join(cls._Context.traces())
    trace_cmd = ['strace', '-f', '-e', 'trace=%s' % traces, '-o', logname]
    child = subprocess.Popen(
        trace_cmd + cmd, cwd=cwd, stdout=stdout, stderr=stderr)
    out, err = child.communicate()
    # Once it's done, inject a chdir() call to cwd to be able to reconstruct
    # the full paths.
    # TODO(maruel): cwd should be saved at each process creation, so forks needs
    # to be traced properly.
    if os.path.isfile(logname):
      with open(logname) as f:
        content = f.read()
      with open(logname, 'w') as f:
        pid = content.split(' ', 1)[0]
        f.write('%s chdir("%s") = 0\n' % (pid, cwd))
        f.write(content)

    if child.returncode != 0:
      print 'Failure: %d' % child.returncode
      # pylint: disable=E1103
      if out:
        print ''.join(out.splitlines(True)[-100:])
      if err:
        print ''.join(err.splitlines(True)[-100:])
    return child.returncode

  @classmethod
  def parse_log(cls, filename, blacklist):
    """Processes a strace log and returns the files opened and the files that do
    not exist.

    It does not track directories.

    Most of the time, files that do not exist are temporary test files that
    should be put in /tmp instead. See http://crbug.com/116251
    """
    logging.info('parse_log(%s, %s)' % (filename, blacklist))
    context = cls._Context(blacklist)
    for line in open(filename):
      context.on_line(line)
    # Resolve any symlink we hit.
    return (
        set(os.path.realpath(f) for f in context.files),
        set(os.path.realpath(f) for f in context.non_existent))


class Dtrace(object):
  """Uses DTrace framework through dtrace. Requires root access.

  Implies Mac OSX.

  dtruss can't be used because it has compatibility issues with python.

  Also, the pid->cwd handling needs to be done manually since OSX has no way to
  get the absolute path of the 'cwd' dtrace variable from the probe.

  Also, OSX doesn't populate curpsinfo->pr_psargs properly, see
  https://discussions.apple.com/thread/1980539.
  """
  IGNORED = (
    '/.vol',
    '/Library',
    '/System',
    '/dev',
    '/etc',
    '/private/var',
    '/tmp',
    '/usr',
    '/var',
  )

  # pylint: disable=C0301
  # To understand the following code, you'll want to take a look at:
  # http://developers.sun.com/solaris/articles/dtrace_quickref/dtrace_quickref.html
  # https://wikis.oracle.com/display/DTrace/Variables
  # http://docs.oracle.com/cd/E19205-01/820-4221/
  #
  # The list of valid probes can be retrieved with:
  # sudo dtrace -l -P syscall | less
  D_CODE = """
      proc:::start /trackedpid[ppid]/ {
        trackedpid[pid] = 1;
        current_processes += 1;
        printf("%d %d:%d %s_%s(\\"%s\\", %d) = 0\\n",
               logindex, ppid, pid, probeprov, probename, execname,
               current_processes);
        logindex++;
      }
      proc:::exit /trackedpid[pid] && current_processes == 1/ {
        trackedpid[pid] = 0;
        current_processes -= 1;
        printf("%d %d:%d %s_%s(\\"%s\\", %d) = 0\\n",
               logindex, ppid, pid, probeprov, probename, execname,
               current_processes);
        logindex++;
        exit(0);
      }
      proc:::exit /trackedpid[pid]/ {
        trackedpid[pid] = 0;
        current_processes -= 1;
        printf("%d %d:%d %s_%s(\\"%s\\", %d) = 0\\n",
               logindex, ppid, pid, probeprov, probename, execname,
               current_processes);
        logindex++;
      }

      /* Finally what we care about! */
      syscall::open*:entry /trackedpid[pid]/ {
        self->arg0 = arg0;
        self->arg1 = arg1;
        self->arg2 = arg2;
      }
      syscall::open*:return /trackedpid[pid] && errno == 0/ {
        printf("%d %d:%d %s(\\"%s\\", %d, %d) = %d\\n",
               logindex, ppid, pid, probefunc, copyinstr(self->arg0),
               self->arg1, self->arg2, errno);
        logindex++;
        self->arg0 = 0;
        self->arg1 = 0;
        self->arg2 = 0;
      }

      syscall::rename:entry /trackedpid[pid]/ {
        self->arg0 = arg0;
        self->arg1 = arg1;
      }
      syscall::rename:return /trackedpid[pid]/ {
        printf("%d %d:%d %s(\\"%s\\", \\"%s\\") = %d\\n",
               logindex, ppid, pid, probefunc, copyinstr(self->arg0),
               copyinstr(self->arg1), errno);
        logindex++;
        self->arg0 = 0;
        self->arg1 = 0;
      }

      /* Track chdir, it's painful because it is only receiving relative path */
      syscall::chdir:entry /trackedpid[pid]/ {
        self->arg0 = arg0;
      }
      syscall::chdir:return /trackedpid[pid] && errno == 0/ {
        printf("%d %d:%d %s(\\"%s\\") = %d\\n",
               logindex, ppid, pid, probefunc, copyinstr(self->arg0), errno);
        logindex++;
        self->arg0 = 0;
      }
      /* TODO(maruel): *stat* functions and friends
        syscall::access:return,
        syscall::chdir:return,
        syscall::chflags:return,
        syscall::chown:return,
        syscall::chroot:return,
        syscall::getattrlist:return,
        syscall::getxattr:return,
        syscall::lchown:return,
        syscall::lstat64:return,
        syscall::lstat:return,
        syscall::mkdir:return,
        syscall::pathconf:return,
        syscall::readlink:return,
        syscall::removexattr:return,
        syscall::setxattr:return,
        syscall::stat64:return,
        syscall::stat:return,
        syscall::truncate:return,
        syscall::unlink:return,
        syscall::utimes:return,
      */
      """

  @classmethod
  def code(cls, pid, cwd):
    """Setups the D code to implement child process tracking.

    Injects a fake chdir() trace to simplify parsing. The reason is that the
    child process is already running at that point so:
    - no proc_start() is logged for it.
    - there is no way to figure out the absolute path of cwd in kernel on OSX

    Since the child process is already started, initialize current_processes to
    1.
    """
    pid = str(pid)
    cwd = os.path.realpath(cwd).replace('\\', '\\\\').replace('%', '%%')
    return (
        'dtrace:::BEGIN {\n'
        '  current_processes = 1;\n'
        '  logindex = 0;\n'
        '  trackedpid[') + pid + ('] = 1;\n'
        '  printf("%d %d:%d chdir(\\"' + cwd + '\\") = 0\\n",\n'
        '      logindex, 1, ' + pid + ');\n'
        '  logindex++;\n'
        '  printf("%d %d:%d %s_%s() = 0\\n",\n'
        '      logindex, ppid, pid, probeprov, probename);\n'
        '  logindex++;\n'
        '}\n') + cls.D_CODE

  class _Context(object):
    """Processes a dtrace log line and keeps the list of existent and non
    existent files accessed.

    Ignores directories.
    """
    # This is the most common format. index pid function(args) = result
    RE_HEADER = re.compile(r'^\d+ (\d+):(\d+) ([a-zA-Z_\-]+)\((.*?)\) = (.+)$')

    # Arguments parsing.
    RE_CHDIR = re.compile(r'^\"(.+?)\"$')
    RE_OPEN = re.compile(r'^\"(.+?)\", (\d+), (\d+)$')
    RE_RENAME = re.compile(r'^\"(.+?)\", \"(.+?)\"$')

    O_DIRECTORY = 0x100000

    def __init__(self, blacklist):
      # TODO(maruel): Handling chdir() and cwd in general on OSX is tricky
      # because OSX only keeps relative directory names. In addition, cwd is a
      # process local variable so forks need to be properly traced and cwd
      # saved.
      self._cwd = {}
      self.blacklist = blacklist
      self.files = set()
      self.non_existent = set()

    def on_line(self, line):
      m = self.RE_HEADER.match(line)
      assert m, line
      fn = getattr(
          self,
          'handle_%s' % m.group(3).replace('-', '_'),
          self._handle_ignored)
      return fn(
          int(m.group(1)),
          int(m.group(2)),
          m.group(3),
          m.group(4),
          m.group(5))

    def handle_dtrace_BEGIN(self, _ppid, _pid, _function, args, _result):
      pass

    def handle_proc_start(self, ppid, pid, _function, _args, result):
      """Transfers cwd."""
      assert result == '0'
      self._cwd[pid] = self._cwd[ppid]

    def handle_proc_exit(self, _ppid, pid, _function, _args, _result):
      """Removes cwd."""
      del self._cwd[pid]

    def handle_chdir(self, _ppid, pid, _function, args, result):
      """Updates cwd."""
      if result.startswith('0'):
        cwd = self.RE_CHDIR.match(args).group(1)
        if not cwd.startswith('/'):
          cwd2 = os.path.join(self._cwd[pid], cwd)
          logging.debug('handle_chdir(%d, %s) -> %s' % (pid, cwd, cwd2))
          self._cwd[pid] = cwd2
        else:
          logging.debug('handle_chdir(%d, %s)' % (pid, cwd))
          self._cwd[pid] = cwd
      else:
        assert False, 'Unexecpected fail: %s' % result

    def handle_open_nocancel(self, ppid, pid, function, args, result):
      return self.handle_open(ppid, pid, function, args, result)

    def handle_open(self, _ppid, pid, _function, args, result):
      args = self.RE_OPEN.match(args).groups()
      flag = int(args[1])
      if self.O_DIRECTORY & flag == self.O_DIRECTORY:
        # Ignore directories.
        return
      self._handle_file(pid, args[0], result)

    def handle_rename(self, _ppid, pid, _function, args, result):
      args = self.RE_RENAME.match(args).groups()
      self._handle_file(pid, args[0], result)
      self._handle_file(pid, args[1], result)

    def _handle_file(self, pid, filepath, result):
      if result.startswith(('-1', '2')):
        return
      orig_filepath = filepath
      if not filepath.startswith('/'):
        filepath = os.path.join(self._cwd[pid], filepath)
      filepath = os.path.normpath(filepath)
      if self.blacklist(filepath):
        return
      # Sadly, still need to filter out directories here;
      # saw open_nocancel(".", 0, 0) = 0 lines.
      if (filepath not in self.files and
          filepath not in self.non_existent and
          not os.path.isdir(filepath)):
        if orig_filepath:
          logging.debug(
              '_handle_file(%d, %s) -> %s' % (pid, orig_filepath, filepath))
        else:
          logging.debug('_handle_file(%d, %s)' % (pid, filepath))
        if os.path.isfile(filepath):
          self.files.add(filepath)
        else:
          self.non_existent.add(filepath)

    @staticmethod
    def _handle_ignored(_ppid, pid, function, args, result):
      logging.debug('%d %s(%s) = %s' % (pid, function, args, result))

  @classmethod
  def gen_trace(cls, cmd, cwd, logname):
    """Runs dtrace on an executable."""
    logging.info('gen_trace(%s, %s, %s)' % (cmd, cwd, logname))
    silent = not isEnabledFor(logging.INFO)
    logging.info('Running: %s' % cmd)
    signal = 'Go!'
    logging.debug('Our pid: %d' % os.getpid())

    # Part 1: start the child process.
    stdout = stderr = None
    if silent:
      stdout = stderr = subprocess.PIPE
    child_cmd = [
      sys.executable, os.path.join(BASE_DIR, 'trace_child_process.py'),
    ]
    child = subprocess.Popen(
        child_cmd + cmd,
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd)
    logging.debug('Started child pid: %d' % child.pid)

    # Part 2: start dtrace process.
    # Note: do not use the -p flag. It's useless if the initial process quits
    # too fast, resulting in missing traces from the grand-children. The D code
    # manages the dtrace lifetime itself.
    trace_cmd = [
      'sudo',
      'dtrace',
      '-x', 'dynvarsize=4m',
      '-x', 'evaltime=exec',
      '-n', cls.code(child.pid, cwd),
      '-o', '/dev/stderr',
      '-q',
    ]
    with open(logname, 'w') as logfile:
      dtrace = subprocess.Popen(
          trace_cmd, stdout=logfile, stderr=subprocess.STDOUT)
    logging.debug('Started dtrace pid: %d' % dtrace.pid)

    # Part 3: Read until one line is printed, which signifies dtrace is up and
    # ready.
    with open(logname, 'r') as logfile:
      while 'dtrace_BEGIN' not in logfile.readline():
        if dtrace.poll() is not None:
          break

    try:
      # Part 4: We can now tell our child to go.
      # TODO(maruel): Another pipe than stdin could be used instead. This would
      # be more consistent with the other tracing methods.
      out, err = child.communicate(signal)

      dtrace.wait()
      if dtrace.returncode != 0:
        print 'dtrace failure: %d' % dtrace.returncode
        with open(logname) as logfile:
          print ''.join(logfile.readlines()[-100:])
        # Find a better way.
        os.remove(logname)
      else:
        # Short the log right away to simplify our life. There isn't much
        # advantage in keeping it out of order.
        cls._sort_log(logname)
      if child.returncode != 0:
        print 'Failure: %d' % child.returncode
        # pylint: disable=E1103
        if out:
          print ''.join(out.splitlines(True)[-100:])
        if err:
          print ''.join(err.splitlines(True)[-100:])
    except KeyboardInterrupt:
      # Still sort when testing.
      cls._sort_log(logname)
      raise

    return dtrace.returncode or child.returncode

  @classmethod
  def parse_log(cls, filename, blacklist):
    """Processes a dtrace log and returns the files opened and the files that do
    not exist.

    It does not track directories.

    Most of the time, files that do not exist are temporary test files that
    should be put in /tmp instead. See http://crbug.com/116251
    """
    logging.info('parse_log(%s, %s)' % (filename, blacklist))
    context = cls._Context(blacklist)
    for line in open(filename, 'rb'):
      context.on_line(line)
    # Resolve any symlink we hit.
    return (
        set(os.path.realpath(f) for f in context.files),
        set(os.path.realpath(f) for f in context.non_existent))

  @staticmethod
  def _sort_log(logname):
    """Sorts the log back in order when each call occured.

    dtrace doesn't save the buffer in strict order since it keeps one buffer per
    CPU.
    """
    with open(logname, 'rb') as logfile:
      lines = [f for f in logfile.readlines() if f.strip()]
    lines = sorted(lines, key=lambda l: int(l.split(' ', 1)[0]))
    with open(logname, 'wb') as logfile:
      logfile.write(''.join(lines))


class LogmanTrace(object):
  """Uses the native Windows ETW based tracing functionality to trace a child
  process.
  """
  class _Context(object):
    """Processes a ETW log line and keeps the list of existent and non
    existent files accessed.

    Ignores directories.
    """

    EVENT_NAME = 0
    TYPE = 1
    PID = 9
    CHILD_PID = 20
    PARENT_PID = 21
    FILE_PATH = 25
    PROC_NAME = 26
    CMD_LINE = 27

    def __init__(self, blacklist):
      self.blacklist = blacklist
      self.files = set()
      self.non_existent = set()

      self._processes = set()
      self._drive_map = DosDriveMap()
      self._first_line = False

    def on_csv_line(self, line):
      """Processes a CSV Event line."""
      # So much white space!
      line = [i.strip() for i in line]
      if not self._first_line:
        assert line == [
          u'Event Name',
          u'Type',
          u'Event ID',
          u'Version',
          u'Channel',
          u'Level',  # 5
          u'Opcode',
          u'Task',
          u'Keyword',
          u'PID',
          u'TID',  # 10
          u'Processor Number',
          u'Instance ID',
          u'Parent Instance ID',
          u'Activity ID',
          u'Related Activity ID',  # 15
          u'Clock-Time',
          u'Kernel(ms)',
          u'User(ms)',
          u'User Data',
        ]
        self._first_line = True
        return

      # As you can see, the CSV is full of useful non-redundant information:
      # Event ID
      assert line[2] == '0'
      # Version
      assert line[3] in ('2', '3'), line[3]
      # Channel
      assert line[4] == '0'
      # Level
      assert line[5] == '0'
      # Task
      assert line[7] == '0'
      # Keyword
      assert line[8] == '0x0000000000000000'
      # Instance ID
      assert line[12] == ''
      # Parent Instance ID
      assert line[13] == ''
      # Activity ID
      assert line[14] == '{00000000-0000-0000-0000-000000000000}'
      # Related Activity ID
      assert line[15] == ''

      if line[0].startswith('{'):
        # Skip GUIDs.
        return

      # Convert the PID in-place from hex.
      line[self.PID] = int(line[self.PID], 16)

      # By Opcode
      handler = getattr(
          self,
          'handle_%s_%s' % (line[self.EVENT_NAME], line[self.TYPE]),
          None)
      if not handler:
        # Try to get an universal fallback
        handler = getattr(self, 'handle_%s_Any' % line[self.EVENT_NAME], None)
      if handler:
        handler(line)
      else:
        assert False, '%s_%s' % (line[self.EVENT_NAME], line[self.TYPE])

    def handle_EventTrace_Any(self, line):
      pass

    def handle_FileIo_Create(self, line):
      m = re.match(r'^\"(.+)\"$', line[self.FILE_PATH])
      self._handle_file(self._drive_map.to_dos(m.group(1)).lower())

    def handle_FileIo_Rename(self, line):
      # TODO(maruel): Handle?
      pass

    def handle_FileIo_Any(self, line):
      pass

    def handle_Image_DCStart(self, line):
      # TODO(maruel): Handle?
      pass

    def handle_Image_Load(self, line):
      # TODO(maruel): Handle?
      pass

    def handle_Image_Any(self, line):
      # TODO(maruel): Handle?
      pass

    def handle_Process_Any(self, line):
      pass

    def handle_Process_DCStart(self, line):
      """Gives historic information about the process tree.

      Use it to extract the pid of the trace_inputs.py parent process that
      started logman.exe.
      """
      ppid = int(line[self.PARENT_PID], 16)
      if line[self.PROC_NAME] == '"logman.exe"':
        # logman's parent is us.
        self._processes.add(ppid)
        logging.info('Found logman\'s parent at %d' % ppid)

    def handle_Process_End(self, line):
      # Look if it is logman terminating, if so, grab the parent's process pid
      # and inject cwd.
      if line[self.PID] in self._processes:
        logging.info('Terminated: %d' % line[self.PID])
        self._processes.remove(line[self.PID])

    def handle_Process_Start(self, line):
      """Handles a new child process started by PID."""
      ppid = line[self.PID]
      pid = int(line[self.CHILD_PID], 16)
      if ppid in self._processes:
        if line[self.PROC_NAME] == '"logman.exe"':
          # Skip the shutdown call.
          return
        self._processes.add(pid)
        logging.info(
            'New child: %d -> %d %s' % (ppid, pid, line[self.PROC_NAME]))

    def handle_SystemConfig_Any(self, line):
      pass

    def _handle_file(self, filename):
      """Handles a file that was touched.

      Interestingly enough, the file is always with an absolute path.
      """
      if (self.blacklist(filename) or
          os.path.isdir(filename) or
          filename in self.files or
          filename in self.non_existent):
        return
      logging.debug('_handle_file(%s)' % filename)
      if os.path.isfile(filename):
        self.files.add(filename)
      else:
        self.non_existent.add(filename)

  def __init__(self):
    # Most ignores need to be determined at runtime.
    self.IGNORED = set([os.path.dirname(sys.executable).lower()])
    # Add many directories from environment variables.
    vars_to_ignore = (
      'APPDATA',
      'LOCALAPPDATA',
      'ProgramData',
      'ProgramFiles',
      'ProgramFiles(x86)',
      'ProgramW6432',
      'SystemRoot',
      'TEMP',
      'TMP',
    )
    for i in vars_to_ignore:
      if os.environ.get(i):
        self.IGNORED.add(os.environ[i].lower())

    # Also add their short path name equivalents.
    for i in list(self.IGNORED):
      self.IGNORED.add(GetShortPathName(i).lower())

    # Add this one last since it has no short path name equivalent.
    self.IGNORED.add('\\systemroot')
    self.IGNORED = tuple(sorted(self.IGNORED))

  @classmethod
  def gen_trace(cls, cmd, cwd, logname):
    logging.info('gen_trace(%s, %s, %s)' % (cmd, cwd, logname))
    # Use "logman -?" for help.

    etl = logname + '.etl'

    silent = not isEnabledFor(logging.INFO)
    stdout = stderr = None
    if silent:
      stdout = stderr = subprocess.PIPE

    # 1. Start the log collection. Requires administrative access. logman.exe is
    # synchronous so no need for a "warmup" call.
    # 'Windows Kernel Trace' is *localized* so use its GUID instead.
    # The GUID constant name is SystemTraceControlGuid. Lovely.
    cmd_start = [
      'logman.exe',
      'start',
      'NT Kernel Logger',
      '-p', '{9e814aad-3204-11d2-9a82-006008a86939}',
      '(process,img,file,fileio)',
      '-o', etl,
      '-ets',  # Send directly to kernel
    ]
    logging.debug('Running: %s' % cmd_start)
    subprocess.check_call(cmd_start, stdout=stdout, stderr=stderr)

    # 2. Run the child process.
    logging.debug('Running: %s' % cmd)
    try:
      child = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=stderr)
      out, err = child.communicate()
    finally:
      # 3. Stop the log collection.
      cmd_stop = [
        'logman.exe',
        'stop',
        'NT Kernel Logger',
        '-ets',  # Send directly to kernel
      ]
      logging.debug('Running: %s' % cmd_stop)
      subprocess.check_call(cmd_stop, stdout=stdout, stderr=stderr)

    # 4. Convert the traces to text representation.
    # Use "tracerpt -?" for help.
    LOCALE_INVARIANT = 0x7F
    windll.kernel32.SetThreadLocale(LOCALE_INVARIANT)
    cmd_convert = [
      'tracerpt.exe',
      '-l', etl,
      '-o', logname,
      '-gmt',  # Use UTC
      '-y',  # No prompt
    ]

    # Normally, 'csv' is sufficient. If complex scripts are used (like eastern
    # languages), use 'csv_unicode'. If localization gets in the way, use 'xml'.
    logformat = 'csv'

    if logformat == 'csv':
      # tracerpt localizes the 'Type' column, for major brainfuck
      # entertainment. I can't imagine any sane reason to do that.
      cmd_convert.extend(['-of', 'CSV'])
    elif logformat == 'csv_utf16':
      # This causes it to use UTF-16, which doubles the log size but ensures the
      # log is readable for non-ASCII characters.
      cmd_convert.extend(['-of', 'CSV', '-en', 'Unicode'])
    elif logformat == 'xml':
      cmd_convert.extend(['-of', 'XML'])
    else:
      assert False, logformat
    logging.debug('Running: %s' % cmd_convert)
    subprocess.check_call(cmd_convert, stdout=stdout, stderr=stderr)

    if child.returncode != 0:
      print 'Failure: %d' % child.returncode
      # pylint: disable=E1103
      if out:
        print ''.join(out.splitlines(True)[-100:])
      if err:
        print ''.join(err.splitlines(True)[-100:])
    return child.returncode

  @classmethod
  def parse_log(cls, filename, blacklist):
    logging.info('parse_log(%s, %s)' % (filename, blacklist))

    # Auto-detect the log format
    with open(filename, 'rb') as f:
      hdr = f.read(2)
      assert len(hdr) == 2
      if hdr == '<E':
        # It starts with <Events>
        logformat = 'xml'
      elif hdr == '\xFF\xEF':
        # utf-16 BOM.
        logformat = 'csv_utf16'
      else:
        logformat = 'csv'

    context = cls._Context(blacklist)

    if logformat == 'csv_utf16':
      def utf_8_encoder(unicode_csv_data):
        """Encodes the unicode object as utf-8 encoded str instance"""
        for line in unicode_csv_data:
          yield line.encode('utf-8')

      def unicode_csv_reader(unicode_csv_data, **kwargs):
        """Encodes temporarily as UTF-8 since csv module doesn't do unicode."""
        csv_reader = csv.reader(utf_8_encoder(unicode_csv_data), **kwargs)
        for row in csv_reader:
          # Decode str utf-8 instances back to unicode instances, cell by cell:
          yield [cell.decode('utf-8') for cell in row]

      # The CSV file is UTF-16 so use codecs.open() to load the file into the
      # python internal unicode format (utf-8). Then explicitly re-encode as
      # utf8 as str instances so csv can parse it fine. Then decode the utf-8
      # str back into python unicode instances. This sounds about right.
      for line in unicode_csv_reader(codecs.open(filename, 'r', 'utf-16')):
        # line is a list of unicode objects
        context.on_csv_line(line)

    elif logformat == 'csv':
      def ansi_csv_reader(ansi_csv_data, **kwargs):
        """Loads an 'ANSI' code page and returns unicode() objects."""
        assert sys.getfilesystemencoding() == 'mbcs'
        encoding = get_current_encoding()
        for row in csv.reader(ansi_csv_data, **kwargs):
          # Decode str 'ansi' instances to unicode instances, cell by cell:
          yield [cell.decode(encoding) for cell in row]

      # The fastest and smallest format but only supports 'ANSI' file paths.
      # E.g. the filenames are encoding in the 'current' encoding.
      for line in ansi_csv_reader(open(filename)):
        # line is a list of unicode objects
        context.on_csv_line(line)

    else:
      raise NotImplementedError('Implement %s' % logformat)

    return (
        set(os.path.realpath(f) for f in context.files),
        set(os.path.realpath(f) for f in context.non_existent))


def relevant_files(files, root):
  """Trims the list of files to keep the expected files and unexpected files.

  Unexpected files are files that are not based inside the |root| directory.
  """
  expected = []
  unexpected = []
  for f in files:
    if f.startswith(root):
      f = f[len(root):]
      assert f
      expected.append(f)
    else:
      unexpected.append(f)
  return sorted(set(expected)), sorted(set(unexpected))


def extract_directories(files, root):
  """Detects if all the files in a directory were loaded and if so, replace the
  individual files by the directory entry.
  """
  directories = set(os.path.dirname(f) for f in files)
  files = set(files)
  for directory in sorted(directories, reverse=True):
    actual = set(
      os.path.join(directory, f) for f in
      os.listdir(os.path.join(root, directory))
      if not f.endswith(('.svn', '.pyc'))
    )
    if not (actual - files):
      files -= actual
      files.add(directory + os.path.sep)
  return sorted(files)


def pretty_print(variables, stdout):
  """Outputs a gyp compatible list from the decoded variables.

  Similar to pprint.print() but with NIH syndrome.
  """
  # Order the dictionary keys by these keys in priority.
  ORDER = (
      'variables', 'condition', 'command', 'relative_cwd', 'read_only',
      KEY_TRACKED, KEY_UNTRACKED)

  def sorting_key(x):
    """Gives priority to 'most important' keys before the others."""
    if x in ORDER:
      return str(ORDER.index(x))
    return x

  def loop_list(indent, items):
    for item in items:
      if isinstance(item, basestring):
        stdout.write('%s\'%s\',\n' % (indent, item))
      elif isinstance(item, dict):
        stdout.write('%s{\n' % indent)
        loop_dict(indent + '  ', item)
        stdout.write('%s},\n' % indent)
      elif isinstance(item, list):
        # A list inside a list will write the first item embedded.
        stdout.write('%s[' % indent)
        for index, i in enumerate(item):
          if isinstance(i, basestring):
            stdout.write(
                '\'%s\', ' % i.replace('\\', '\\\\').replace('\'', '\\\''))
          elif isinstance(i, dict):
            stdout.write('{\n')
            loop_dict(indent + '  ', i)
            if index != len(item) - 1:
              x = ', '
            else:
              x = ''
            stdout.write('%s}%s' % (indent, x))
          else:
            assert False
        stdout.write('],\n')
      else:
        assert False

  def loop_dict(indent, items):
    for key in sorted(items, key=sorting_key):
      item = items[key]
      stdout.write("%s'%s': " % (indent, key))
      if isinstance(item, dict):
        stdout.write('{\n')
        loop_dict(indent + '  ', item)
        stdout.write(indent + '},\n')
      elif isinstance(item, list):
        stdout.write('[\n')
        loop_list(indent + '  ', item)
        stdout.write(indent + '],\n')
      elif isinstance(item, basestring):
        stdout.write(
            '\'%s\',\n' % item.replace('\\', '\\\\').replace('\'', '\\\''))
      elif item in (True, False, None):
        stdout.write('%s\n' % item)
      else:
        assert False, item

  stdout.write('{\n')
  loop_dict('  ', variables)
  stdout.write('}\n')


def trace_inputs(logfile, cmd, root_dir, cwd_dir, product_dir, force_trace):
  """Tries to load the logs if available. If not, trace the test.

  Symlinks are not processed at all.

  Arguments:
  - logfile:     Absolute path to the OS-specific trace.
  - cmd:         Command list to run.
  - root_dir:    Base directory where the files we care about live.
  - cwd_dir:     Cwd to use to start the process, relative to the root_dir
                 directory.
  - product_dir: Directory containing the executables built by the build
                 process, relative to the root_dir directory. It is used to
                 properly replace paths with <(PRODUCT_DIR) for gyp output.
  - force_trace: Will force to trace unconditionally even if a trace already
                 exist.
  """
  logging.debug(
      'trace_inputs(%s, %s, %s, %s, %s, %s)' % (
        logfile, cmd, root_dir, cwd_dir, product_dir, force_trace))

  # It is important to have unambiguous path.
  assert os.path.isabs(root_dir), root_dir
  assert os.path.isabs(logfile), logfile
  assert not cwd_dir or not os.path.isabs(cwd_dir), cwd_dir
  assert not product_dir or not os.path.isabs(product_dir), product_dir

  cmd = fix_python_path(cmd)
  assert (
      (os.path.isfile(logfile) and not force_trace) or os.path.isabs(cmd[0])
      ), cmd[0]

  # Resolve any symlink
  root_dir = os.path.realpath(root_dir)

  if sys.platform == 'win32':
    # Help ourself and lowercase all the paths.
    # TODO(maruel): handle short path names by converting them to long path name
    # as needed.
    root_dir = root_dir.lower()
    if cwd_dir:
      cwd_dir = cwd_dir.lower()
    if product_dir:
      product_dir = product_dir.lower()

  def print_if(txt):
    if cwd_dir is None:
      print(txt)

  flavor = get_flavor()
  if flavor == 'linux':
    api = Strace()
  elif flavor == 'mac':
    api = Dtrace()
  elif sys.platform == 'win32':
    api = LogmanTrace()
  else:
    print >> sys.stderr, 'Unsupported platform %s' % sys.platform
    return 1

  if not os.path.isfile(logfile) or force_trace:
    if os.path.isfile(logfile):
      os.remove(logfile)
    print_if('Tracing... %s' % cmd)
    cwd = root_dir
    # Use the proper relative directory.
    if cwd_dir:
      cwd = os.path.join(cwd, cwd_dir)
    returncode = api.gen_trace(cmd, cwd, logfile)
    if returncode and not force_trace:
      return returncode

  git_path = os.path.sep + '.git' + os.path.sep
  svn_path = os.path.sep + '.svn' + os.path.sep
  def blacklist(f):
    """Strips ignored paths."""
    return (
        f.startswith(api.IGNORED) or
        f.endswith('.pyc') or
        git_path in f or
        svn_path in f)

  print_if('Loading traces... %s' % logfile)
  files, non_existent = api.parse_log(logfile, blacklist)

  print_if('Total: %d' % len(files))
  print_if('Non existent: %d' % len(non_existent))
  for f in non_existent:
    print_if('  %s' % f)

  expected, unexpected = relevant_files(
      files, root_dir.rstrip(os.path.sep) + os.path.sep)
  if unexpected:
    print_if('Unexpected: %d' % len(unexpected))
    for f in unexpected:
      print_if('  %s' % f)

  simplified = extract_directories(expected, root_dir)
  print_if('Interesting: %d reduced to %d' % (len(expected), len(simplified)))
  for f in simplified:
    print_if('  %s' % f)

  if cwd_dir is not None:
    def cleanuppath(x):
      """Cleans up a relative path. Converts any os.path.sep to '/' on Windows.
      """
      if x:
        x = x.rstrip(os.path.sep).replace(os.path.sep, '/')
      if x == '.':
        x = ''
      if x:
        x += '/'
      return x

    # Both are relative directories to root_dir.
    cwd_dir = cleanuppath(cwd_dir)
    product_dir = cleanuppath(product_dir)

    def fix(f):
      """Bases the file on the most restrictive variable."""
      logging.debug('fix(%s)' % f)
      # Important, GYP stores the files with / and not \.
      if sys.platform == 'win32':
        f = f.replace('\\', '/')

      if product_dir and f.startswith(product_dir):
        return '<(PRODUCT_DIR)/%s' % f[len(product_dir):]
      else:
        # cwd_dir is usually the directory containing the gyp file. It may be
        # empty if the whole directory containing the gyp file is needed.
        return posix_relpath(f, cwd_dir) or './'

    corrected = [fix(f) for f in simplified]
    tracked = [f for f in corrected if not f.endswith('/') and ' ' not in f]
    untracked = [f for f in corrected if f.endswith('/') or ' ' in f]
    variables = {}
    if tracked:
      variables[KEY_TRACKED] = tracked
    if untracked:
      variables[KEY_UNTRACKED] = untracked
    value = {
      'conditions': [
        ['OS=="%s"' % flavor, {
          'variables': variables,
        }],
      ],
    }
    pretty_print(value, sys.stdout)
  return 0


def main():
  parser = optparse.OptionParser(
      usage='%prog <options> [cmd line...]')
  parser.allow_interspersed_args = False
  parser.add_option(
      '-v', '--verbose', action='count', default=0, help='Use multiple times')
  parser.add_option('-l', '--log', help='Log file')
  parser.add_option(
      '-c', '--cwd',
      help='Signal to start the process from this relative directory. When '
           'specified, outputs the inputs files in a way compatible for '
           'gyp processing. Should be set to the relative path containing the '
           'gyp file, e.g. \'chrome\' or \'net\'')
  parser.add_option(
      '-p', '--product-dir', default='out/Release',
      help='Directory for PRODUCT_DIR. Default: %default')
  parser.add_option(
      '--root-dir', default=ROOT_DIR,
      help='Root directory to base everything off. Default: %default')
  parser.add_option(
      '-f', '--force',
      action='store_true',
      default=False,
      help='Force to retrace the file')

  options, args = parser.parse_args()
  level = [logging.ERROR, logging.INFO, logging.DEBUG][min(2, options.verbose)]
  logging.basicConfig(
        level=level,
        format='%(levelname)5s %(module)15s(%(lineno)3d):%(message)s')

  if not options.log:
    parser.error('Must supply a log file with -l')
  if not args:
    if not os.path.isfile(options.log) or options.force:
      parser.error('Must supply a command to run')
  else:
    args[0] = os.path.abspath(args[0])

  if options.root_dir:
    options.root_dir = os.path.abspath(options.root_dir)

  return trace_inputs(
      os.path.abspath(options.log),
      args,
      options.root_dir,
      options.cwd,
      options.product_dir,
      options.force)


if __name__ == '__main__':
  sys.exit(main())
