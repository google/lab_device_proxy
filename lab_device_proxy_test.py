#!/usr/bin/env python2.7
# PLEASE LEAVE THE SHEBANG: the proxy test runs as a standalone Python file.

# Google BSD license http://code.google.com/google_bsd_license.html
# Copyright 2014 Google Inc. wrightt@google.com

"""Lab Device Proxy Unit Tests.

This script runs in two modes:
  1) The main 'Unit Test' client-side mode
  2) The server-side mocked command mode

The basic flow is:
  if _IS_CLIENT:
    # The main 'Unit Test' mode:
    spawn the proxy_server (reused for all tests)
    for each test (e.g. 'testFoo'):
      write a mock '/tmp/test_server/adb' file with content:
          './lab_device_proxy_test.py --mock testFoo'
      run `lab_device_proxy_client.py adb X`
      assert that we got the expected output
    kill the proxy_server
  else:
    # The server-side 'mock' mode:  (via '/tmp/test_server/adb')
    generate mock output for the 'testFoo' test

This is organized into test-centric methods, e.g.:
  def testFoo(self):
    if _IS_CLIENT:
      run proxy via mock script, assert expected output
    else
      generate mocked output

As illustrated above, we spawn real client and server processes, which provides
maximum code coverage.  Another option would be to use mocks and inline the
client and/or server in our unittest process, but this would require tricky
stubs and wouldn't test the end-to-end system.
"""


import functools
import httplib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


_IS_CLIENT = not (
    __name__ == '__main__' and len(sys.argv) >= 3 and sys.argv[1] == '--mock')


def ClientOnly(f):
  """A decorator that asserts _IS_CLIENT."""
  @functools.wraps(f)
  def Wrapper(*args, **kwargs):
    assert _IS_CLIENT
    return f(*args, **kwargs)
  return Wrapper


def main():
  if _IS_CLIENT:
    # Run the unit tests, e.g. sys.argv=[__file__, '-v']
    unittest.main()
  else:
    # Run the mock server command, e.g. sys.argv=[__file__,
    #    '--mock', 'LabDeviceProxyTest.testStdout', 'adb', 'devices']
    test_name = sys.argv[2]
    sys.argv = sys.argv[3:]
    cls = LabDeviceProxyTest
    assert test_name.startswith('%s.test' % cls.__name__), test_name
    test_name = test_name[test_name.rfind('.') + 1:]
    test_method = getattr(cls(method_name=test_name), test_name)
    test_method()


class LabDeviceProxyTest(unittest.TestCase):
  """Lab Device Proxy Unit tests."""

  def __init__(self, method_name=None):
    """Creates a client or mocked-server command.

    Args:
      method_name: string, e.g. 'testStdout'.  Our base class specifies a
          default value of None, but (in practice) the value is never None.
    """
    assert method_name
    self._test_name = method_name
    super(LabDeviceProxyTest, self).__init__(method_name)

  def testStdout(self):
    """Verifies that server stdout is passed back to the client."""
    if _IS_CLIENT:
      # Create a mock './adb' script and invoke it via the proxy client.
      out = self._ProxyCheckOutput(['adb', 'devices'])
      self.assertEqual(out, '*mock*List of devices.\n\n')
    else:
      # This runs in a child process of the proxy server, NOT in the unittest
      # or proxy client process!
      self.assertEqual(sys.argv, ['adb', 'devices'])
      print '*mock*List of devices.\n'

  def testExitCode(self):
    """Verifies that the server's exit code is returned to the client."""
    if _IS_CLIENT:
      returncode = self._ProxyCall(['adb', 'uninstall', 'no_such_pkg'])
      self.assertEqual(returncode, 2, 'exit code')
    else:
      self.assertEqual(sys.argv, ['adb', 'uninstall', 'no_such_pkg'])
      sys.exit(2)

  # testStderr:
  #   client: popen, assert got stderr
  #   server: print to stderr

  # testMixedOutput:
  #   client: popen, assert got interleaved stdout,stderr,stdout
  #   server: print stdout,stderr,stdout

  # testChunked:
  #   client: popen, assert got stdout, waited 1s, got more stdout
  #   server: print, sleep 1, print

  def testPushFile(self):
    """Verifies that the client can push a file to the server."""
    if _IS_CLIENT:
      from_file = os.path.join(self._client_temp, 'from_file')
      with open(from_file, 'w') as f:
        f.write('push_me')
      out = self._ProxyCheckOutput(['adb', 'push', from_file, 'to_dev'])
      self.assertEqual(out, 'ok\n')
    else:
      if len(sys.argv) > 2:
        from_file, sys.argv[2] = sys.argv[2], 'FILE'
      self.assertEqual(sys.argv, ['adb', 'push', 'FILE', 'to_dev'])
      self.assertTrue(os.path.exists(from_file), 'missing %s' % from_file)
      with open(from_file, 'r') as f:
        self.assertEqual(f.read(), 'push_me', '%s content' % from_file)
      print 'ok'

  # testPushDir:
  #   client: mkdir w/ subfiles, check_output
  #   server: assert got dir w/ subfiles

  # testPushNone:
  #   client: check_output(adb push 'fake_filename' x)
  #   server: assert !exists(arg[2])

  def testPullFileToNewFile(self):
    """Verifies that the server can return a file to the client."""
    if _IS_CLIENT:
      to_file = os.path.join(self._client_temp, 'to_file')
      out = self._ProxyCheckOutput(['adb', 'pull', 'from_dev', to_file])
      self.assertEqual(out, 'ok\n')
      with open(to_file, 'r') as f:
        self.assertEqual(f.read(), 'pull_me', '%s content' % to_file)
    else:
      if len(sys.argv) > 3:
        to_file, sys.argv[3] = sys.argv[3], 'FILE'
      self.assertEqual(sys.argv, ['adb', 'pull', 'from_dev', 'FILE'])
      with open(to_file, 'w') as f:
        f.write('pull_me')
      print 'ok'

  # testPullFileToExistingFile:
  #   client: write X to file F, cmd, assert F contains Y
  #   server: write Y to file arg[2]

  # testPullFileToEmptyDir:
  #   client: mkdir D, cmd, assert D/F contains Y
  #   server: write F to arg[2]

  # testPullFileToExistingDir:
  #   client: mkdir D, write X to F, cmd, assert D/F contains Y
  #   server: write Y to arg[2]

  # testPullFileToNone:
  #   client: cmd(foo/bar/qux), assert error 'no such file or directory'
  #   server: write Y to arg[2]

  # testPullDirToFile:
  #   client: write X to F, cmd, assert error 'is a directory: (not copied)'
  #   server: mkdir arg[2], write Z to arg[2]/foo

  # testPullDirToDir:
  #   client: mkdir D, cmd, assert D/bar/foo contains Z
  #   server: mkdir arg[2]/bar, write Z to arg[2]/foo

  # testPullDirToNone:
  #   client: cmd(foo/bar/qux), assert error 'no such file or directory'
  #   server: mkdir arg[2]/bar, write Z to arg[2]/foo

  # testPullNone:
  #   client: cmd(F), assert !exists(F)
  #   server: no-op

  # testClientParse:
  #   client: popen(adb blah), assert errcode
  #   server: no-op

  # testServerParse:
  #   client: open url, write bogus http args, assert got http 405
  #   server: no-op

  # testClientRecv:
  #   client: bind 9999 w/ bad-chunk server, cmd, assert error
  #   server: no-op

  #
  # All the following methods only run on the client side.
  #

  _server_url = None   # Server URL
  _server_proc = None  # Server process
  _python_path = None  # Python binary path

  _client_temp = None  # Client temporary dir
  _server_temp = None  # Server temporary dir

  @classmethod
  @ClientOnly
  def setUpClass(cls):
    """Creates the mock proxy server."""
    server_port = 9094
    cls._server_url = 'http://localhost:%s' % server_port

    server_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'lab_device_proxy_server.py')
    assert os.path.exists(server_path), 'Missing %s' % server_path

    # Find the Python path for our _ProxyPopen script's environment.
    cls._python_path = os.path.dirname(os.path.abspath(sys.executable))
    python_version = 'python%s.%s' % (
        sys.version_info.major, sys.version_info.minor)
    if python_version not in os.listdir(cls._python_path):
      # Search our PATH -- this is required on some OS's (e.g. OS X).
      for env_path in os.environ.get('PATH', '').split(os.pathsep):
        if os.path.isdir(env_path) and python_version in os.listdir(env_path):
          cls._python_path = env_path
          break
      else:
        raise RuntimeError('Unable to find %s in %s:%s' % (
            python_version, cls._python_path, os.environ.get('PATH', '')))

    cls._server_temp = tempfile.mkdtemp(prefix='test_server', dir='/tmp')

    # Start server
    server_env = {'PATH': ':'.join([cls._server_temp, cls._python_path])}
    if 'PYTHONPATH' in os.environ:
      server_env['PYTHONPATH'] = os.environ['PYTHONPATH']
    cls._server_proc = subprocess.Popen(
        [server_path, '--port=%s' % server_port],
        close_fds=True,
        cwd=cls._server_temp,
        # stderr=open(os.devnull, 'w'),  # hide log_message output
        env=server_env)

    # Wait until the server is up
    timeout_time = time.time() + 3  # Arbitary timeout
    while True:
      time.sleep(0.2)  # Arbitrary delay; always delay the first try
      try:
        conn = httplib.HTTPConnection('localhost', server_port, timeout=5)
        conn.request('GET', '/healthz')
        res = conn.getresponse()
        assert res.status == httplib.OK, 'Server returned %s: %s' % (
            res.status, res.reason)
        break
      except IOError:
        if time.time() > timeout_time:
          raise

  @ClientOnly
  def setUp(self):
    """Sets up a test."""
    if not self._client_temp:
      self._client_temp = tempfile.mkdtemp(prefix='test_client', dir='/tmp')

  @ClientOnly
  def _ProxyCall(self, *args, **kwargs):
    """Returns the proxied equivalent of subprocess.call."""
    return self._ProxyPopen(*args, **kwargs).wait()

  @ClientOnly
  def _ProxyCheckCall(self, *args, **kwargs):
    """Returns the proxied equivalent of subprocess.check_call."""
    retcode = self._ProxyCall(*args, **kwargs)
    if retcode:
      cmd = kwargs.get('args', args[0] if args else None)
      raise subprocess.CalledProcessError(retcode, cmd)
    return 0

  @ClientOnly
  def _ProxyCheckOutput(self, *args, **kwargs):
    """Returns the proxied equivalent of subprocess.check_output."""
    if 'stdout' in kwargs:
      raise ValueError('stdout argument not allowed, it will be overridden.')
    process = self._ProxyPopen(stdout=subprocess.PIPE, *args, **kwargs)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
      cmd = kwargs.get('args', args[0] if args else None)
      raise subprocess.CalledProcessError(retcode, cmd, output=output)
    return output

  @ClientOnly
  def _ProxyPopen(self, args, **kwargs):
    """Returns the proxied equivalent of subprocess.Popen."""
    args = args[:]
    kwargs = kwargs.copy()

    test_path = os.path.abspath(__file__)
    client_path = os.path.join(
        os.path.dirname(test_path), 'lab_device_proxy_client.py')
    assert os.path.exists(client_path), 'Missing %s' % client_path

    # Write a script in the server's temp directory whose name matches the
    # name of the specified command, e.g.
    #   /tmp/test_server/adb
    # with content:
    #   #!/bin/sh
    #   ./lab_device_proxy_test.py --mock <test_name> <args>...
    # That way, when the client asks the proxy_server to run a command, e.g.:
    #   adb push foo
    # the server will run our script instead of the real 'adb', and our script
    # will run our test's test_method with !_IS_CLIENT.
    cmd = os.path.basename(args[0])
    server_file = os.path.join(self._server_temp, cmd)
    with open(server_file, 'w') as f:
      # We need this shebang line, otherwise the call will hang
      f.write('#!/bin/sh\nexec "%s" --mock "%s.%s" "%s" "$@"\n' % (
          test_path, self.__class__.__name__, self._test_name, cmd))
    os.chmod(server_file, 0755)

    # Set proxy_client args
    args = ([client_path, '--url', self._server_url] + args)
    kwargs.setdefault('env', {'PATH': self._python_path})
    kwargs.setdefault('cwd', self._server_temp)
    kwargs.setdefault('close_fds', True)

    return subprocess.Popen(args, **kwargs)

  @ClientOnly
  def tearDown(self):
    """Cleans up after a test."""
    if self._client_temp:
      for fn in os.listdir(self._client_temp):
        os.remove(os.path.join(self._client_temp, fn))

    if self._server_temp:
      for fn in os.listdir(self._server_temp):
        os.remove(os.path.join(self._server_temp, fn))

  @classmethod
  @ClientOnly
  def tearDownClass(cls):
    """Stops the server and cleans up."""
    if cls._server_proc:
      cls._server_proc.kill()
      cls._server_proc.wait()
      cls._server_proc = None

    if cls._server_temp:
      shutil.rmtree(cls._server_temp)
      cls._server_temp = None

    if cls._client_temp:
      shutil.rmtree(cls._client_temp)
      cls._client_temp = None


if __name__ == '__main__':
  main()
