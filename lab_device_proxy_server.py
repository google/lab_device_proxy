#!/usr/bin/env python2.7
# PLEASE LEAVE THE SHEBANG: the proxy server runs as a standalone Python file.

# Google BSD license http://code.google.com/google_bsd_license.html
# Copyright 2014 Google Inc. wrightt@google.com

"""Lab device proxy server."""

import argparse
import BaseHTTPServer
import datetime
import httplib
import os
import re
import select
import shutil
import signal
import SocketServer
import subprocess
import sys
import tempfile
import time

# Reuse the client's parameter parser and tar/untar functions.
try:
  # pylint: disable=g-import-not-at-top
  # pylint: disable=g-import-not-at-top
  from lab_device_proxy import lab_device_proxy_client as lab_common
except ImportError:
  # pylint: disable=g-import-not-at-top
  import lab_device_proxy_client as lab_common

IDEVICE_PATH = 'IDEVICE_PATH'
SERVER_PORT = 8084

MAX_READ = 8192


def main(args):
  """Runs the server, forever.

  Args:
    args: List of strings, supports an optional '--port=PORT' arg.
  """
  signal.signal(signal.SIGINT, signal.SIG_DFL)  # Exit on Ctrl-C

  argparser = argparse.ArgumentParser()
  argparser.add_argument('-p', '--port', default=SERVER_PORT, type=int,
                         help='Port the web server should listen on.')
  parsed_args = argparser.parse_args(args[1:])
  server_port = parsed_args.port

  server = None
  try:
    server = ThreadedHTTPServer(
        ('', server_port), LabDeviceProxyRequestHandler)
    server.serve_forever(poll_interval=0.5)
  finally:
    if server:
      server.shutdown()


class ThreadedHTTPServer(SocketServer.ThreadingMixIn,
                         BaseHTTPServer.HTTPServer):
  """Spawns a thread per request."""

  pass


class LabDeviceProxyRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
  """Handles all client requests."""

  def do_GET(self):  # pylint: disable=g-bad-name
    """Handles a GET request."""
    if self.path == '/healthz':
      response_data = 'ok\n'
      self.send_response(httplib.OK)
      self.send_header('Content-Type', 'text/plain; charset=utf=8')
      self.send_header('Content-Length', str(len(response_data)))
      self.end_headers()
      self.wfile.write(response_data)
    else:
      return self.send_error(httplib.METHOD_NOT_ALLOWED)

  def do_POST(self):  # pylint: disable=g-bad-name
    """Handles a POST request."""
    params = []
    tmp_fs = TempFileSystem()

    timestamps = [('', time.time())]  # Never printed, only subtracted
    try:
      on_error = httplib.BAD_REQUEST
      while self._ReadChunk(self.rfile, params, tmp_fs):
        pass

      on_error = httplib.FORBIDDEN
      self._ValidateCommand(params)

      on_error = httplib.INTERNAL_SERVER_ERROR
      self._BeginResponse()
      on_error = None  # Sent our response status code

      timestamps.append(('req', time.time()))

      args = [str(curr.value) for curr in params]
      if IDEVICE_PATH in os.environ:
        args[0] = os.environ[IDEVICE_PATH] + '/' + args[0]

      self._RunCommand(args, self.rfile, self.wfile)
      timestamps.append(('cmd', time.time()))

      self._WriteChunks(params, self.wfile)
    except Exception, e:  # pylint: disable=broad-except
      timestamps.append(('err', time.time()))
      args = [str(curr.value) for curr in params]
      self.log_message('Failed: %s\n%s', ' '.join(args), lab_common.GetStack())
      if on_error is not None:
        self.send_error(on_error, str(e))
    finally:
      tmp_fs.Cleanup()
      timestamps.append(('resp', time.time()))

      timings = ' '.join(
          ['%s: %.1f' % (name, (timestamp - timestamps[i - 1][1]))
           for i, (name, timestamp) in enumerate(timestamps) if i > 0])
      self.log_message('(%s) %s', timings, ' '.join(args))

  @classmethod
  def _ReadChunk(cls, from_stream, to_params, to_fs):
    """Reads the next chunk and updates the to_params list.

    Args:
      from_stream: stream to read from
      to_params: List of Params
      to_fs: TempFileSystem
    Returns:
      False if there are no more chunks, else True.
    Raises:
      ValueError: when given an invalid chunk.
    """
    # Parse header
    header = lab_common.ChunkHeader()
    header_line = from_stream.readline()
    header.Parse(header_line)

    # Get the curr arg, which might be a continuation of the prev arg
    curr = None
    prev = (to_params[-1] if to_params else None)
    if header.len_ > 0:
      curr_index = None
      if re.match(r'[aio]\d+$', header.id_):
        curr_index = int(header.id_[1:])
      prev_index = len(to_params) - 1
      if curr_index == prev_index:
        curr = prev
      elif curr_index == prev_index + 1:
        curr = Param()
        curr.index = curr_index
        curr.header = header
        to_params.append(curr)
      else:
        # Arguments must be sent in order.  In particular, files split into >1
        # chunk must be sent in adjacent chunks.
        raise ValueError('Expecting id %s or %s, not: %s' % (
            prev_index, prev_index + 1, header_line))

    if curr != prev and prev and prev.in_fp:
      # Close the prev arg's input file
      prev.in_fp.close()
      prev.in_fp = None

    if curr == prev:
      prev.header.len_ = header.len_
      if header != prev.header:
        raise ValueError('Unexpected header change: %s' % header_line)
      if not header.in_:
        raise ValueError('Duplicate header: %s' % header_line)

    if header.len_ <= 0:
      # End of chunks
      return False

    if not header.in_ and not header.out_:
      # Typical non-i/o arg
      curr.value = lab_common.ReadExactly(from_stream, header.len_)
    elif header.in_:
      # Input file
      if header.out_:
        raise ValueError('Invalid header: %s' % header_line)
      if curr == prev:
        # Continue previous in_fp
        if not curr.in_fp or header.is_empty_ or header.is_absent_:
          raise ValueError('File done: %s' % header_line)
      else:
        # Start new in_fp
        parent_fn = to_fs.Mkdir('in%s_' % curr.index)
        in_fn = os.path.normpath(os.path.join(parent_fn, header.in_))
        if in_fn != parent_fn and not in_fn.startswith(parent_fn + '/'):
          raise ValueError('Invalid arg[%s] input path "%s"' % (
              curr.index, header.in_))
        if not header.is_absent_:
          curr.in_fp = (
              lab_common.Untar(parent_fn) if header.is_tar_ else
              open(in_fn, 'wb'))
        curr.value = in_fn
      if header.is_absent_ or header.is_empty_:
        lab_common.ReadExactly(from_stream, header.len_)
      else:
        bytes_read = 0
        while bytes_read < header.len_:
          data = from_stream.read(min(MAX_READ, header.len_ - bytes_read))
          bytes_read += len(data)
          curr.in_fp.write(data)
    else:
      # Output file placeholder
      lab_common.ReadExactly(from_stream, header.len_)
      curr.out_dn = to_fs.Mkdir('out%s_' % curr.index)
      out_fn = os.path.normpath(os.path.join(curr.out_dn, header.out_))
      if out_fn != curr.out_dn and not out_fn.startswith(curr.out_dn + '/'):
        raise ValueError('Invalid arg[%s] output path "%s"' % (
            curr.index, header.out_))
      curr.value = out_fn

    # Read end-of-chunk
    if lab_common.ReadExactly(from_stream, 2) != '\r\n':
      raise ValueError('Chunk does not end with crlf')

    # Keep reading chunks
    return True

  @staticmethod
  def _ValidateCommand(params):
    """Verifies the client's command is valid and allowed.

    Args:
      params: List of client-provided Params
    Raises:
      ValueError: if the command is illegal.
    """
    # Parse the command to verify the basic format and options
    args = [curr.value for curr in params]
    parser = lab_common.PARSER  # Reuse the client's parser
    try:
      reqs = parser.parse_args(args)
    except:
      raise ValueError('Unsupported command: %s' % ' '.join(args))
    if len(reqs) != len(params):
      raise ValueError('Parsed length mismatch?')

    # Verify that the expected in/out file args are provided
    for index in range(len(params)):
      required = reqs[index]
      provided = params[index]
      in_required = isinstance(required, lab_common.InputFileParameter)
      in_provided = (provided.header.in_ is not None)
      if in_required != in_provided:
        raise ValueError('arg[%s]=%s %s input file', index, required,
                         'provides' if in_provided else 'lacks')
      out_required = isinstance(required, lab_common.OutputFileParameter)
      out_provided = (provided.out_dn is not None)
      if out_required != out_provided:
        raise ValueError('arg[%s]=%s %s output file', index, required,
                         'provides' if out_provided else 'lacks')

  def _BeginResponse(self):
    """Begin the server response."""
    # This puts the output data into a MIME format
    self.send_response(httplib.OK)
    self.send_header('Content-Type', 'text/plain; charset=utf=8')
    self.send_header('Transfer-Encoding', 'chunked')
    self.send_header('Content-Encoding', 'UTF-8')
    self.end_headers()

  def log_request(self, code='-', size='-'):  # pylint: disable=g-bad-name
    """Suppresses worthless logging."""
    if (re.match(r'^POST / HTTP/1.[01]$', self.requestline) and
        code == 200 and size == '-'):
      return
    # Our superclass is an old-style class, so we can't use "super(...)"
    BaseHTTPServer.BaseHTTPRequestHandler.log_request(self, code, size)

  def log_message(self, fmt, *args):  # pylint: disable=g-bad-name
    """Logs to stderr."""
    # Sample log output: I0313 14:23:49.512168 hostname adb logcat
    now = datetime.datetime.now()
    timestamp = now.strftime('I%m%d %T') + ('.%06d' % now.microsecond)
    # Just keep up to the first two elements of the domain name.
    hostname = '.'.join(self.address_string().split('.', 2)[:2])
    print >>sys.stderr, '%s %s - %s' % (timestamp, hostname, fmt % args)

  @staticmethod
  def _RunCommand(args, from_stream, to_stream):
    """Runs a command and returns its status in the response body.

    Args:
      args: List of strings
      from_stream: stream to read from
      to_stream: stream to write to
    """
    stdout = lab_common.ChunkedOutputStream(lab_common.ChunkHeader(
        '1'), to_stream)
    stderr = lab_common.ChunkedOutputStream(lab_common.ChunkHeader(
        '2'), to_stream)
    exit_stream = lab_common.ChunkedOutputStream(lab_common.ChunkHeader(
        'exit'), to_stream)

    try:
      # bufsize=0 sets stdout/stderr to be unbuffered.  Even with this
      #   option,the command must periodically flush its output, otherwise we
      #   it'll be buffered at the OS layer.
      # close_fds=True ensures that, if we indirectly start the adb server, it
      #   won't inherit our server port and cause "Address already in use"
      #   errors.
      proc = subprocess.Popen(
          args, bufsize=0, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
          close_fds=True, shell=False)
    except Exception, e:  # pylint: disable=broad-except
      stderr.write('%s\n' % e)
      exit_stream.write(str(getattr(e, 'returncode', getattr(e, 'errno', 1))))
      return

    while True:
      # Through observation, it was discovered that from_stream becomes readable
      # immediately after a client ctrl-c, so if/when it becomes readable
      # the client has been lost and the server should break out of the select.
      reads = [proc.stdout, proc.stderr, from_stream]  # streams to select from
      rlist, _, _ = select.select(reads,
                                  [],  # writes
                                  [],  # exceptions
                                  2)  # timeout
      read_out = ''
      read_err = ''
      if from_stream in rlist:
        proc.kill()
        break
      if proc.stdout in rlist:
        read_out = os.read(proc.stdout.fileno(), MAX_READ)
        if read_out:
          stdout.write(read_out)
          stdout.flush()
      if proc.stderr in rlist:
        read_err = os.read(proc.stderr.fileno(), MAX_READ)
        if read_err:
          stderr.write(read_err)
          stderr.flush()
      if proc.poll() is not None and not read_out and not read_err:
        exit_stream.write(str(proc.returncode))
        break

    stdout.close()

  @classmethod
  def _WriteOutputFile(cls, curr, to_stream):
    """Write an output file to the response stream.

    Args:
      curr: Param with non-None out_dn
      to_stream: stream to write to
    """
    out_dn = curr.out_dn
    header = lab_common.ChunkHeader('o%d' % curr.index)
    header.out_ = curr.header.out_
    if not curr.header.is_tar_:
      out_fns = os.listdir(out_dn)
      if not out_fns:
        header.is_absent_ = True
        lab_common.SendChunk(header, None, to_stream)
        return
      # We created this out_dn path via Mkdir, so it's valid.
      fn = (os.path.join(out_dn, out_fns[0]) if len(out_fns) == 1 else None)
      if fn and os.path.isfile(fn):
        with open(fn, 'rb') as fp:
          data = fp.read(MAX_READ)
          if not data:
            lab_common.SendChunk(header, None, to_stream)
          else:
            while data:
              lab_common.SendChunk(header, data, to_stream)
              data = fp.read(MAX_READ)
        return
    header.is_tar_ = True
    lab_common.SendTar(out_dn, '/', header, to_stream)

  @classmethod
  def _WriteChunks(cls, params, to_stream):
    """Writes the output file chunks to the client.

    Args:
      params: List of Params
      to_stream: stream to write to
    """
    for curr in params:
      if curr.out_dn:
        cls._WriteOutputFile(curr, to_stream)
    to_stream.write('0\r\n\r\n')


class Param(object):
  """A server-side arg."""

  index = None   # int
  value = None   # string
  header = None  # ChunkHeader
  in_fp = None   # File object
  out_dn = None  # string path


class TempFileSystem(object):
  """A temporary file system manager."""

  def __init__(self):
    self._root_fn = None

  def Mkdir(self, prefix):
    """Makes a new directory.

    Args:
      prefix: string filename prefix
    Returns:
      string directory name
    """
    if not self._root_fn:
      self._root_fn = tempfile.mkdtemp(prefix='proxy_', dir='/tmp')
    return tempfile.mkdtemp(prefix=prefix, dir=self._root_fn)

  def Cleanup(self):
    """Deletes all Mkdir'd paths."""
    if self._root_fn:
      shutil.rmtree(self._root_fn)
      self._root_fn = None


if __name__ == '__main__':
  main(sys.argv)
