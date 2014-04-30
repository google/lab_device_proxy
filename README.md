Google BSD license <http://code.google.com/google_bsd_license.html>   
Copyright 2014 Google Inc.  <wrightt@google.com>


Lab Device Proxy
================

The Lab Device Proxy is an HTTP-based client and server that allows users to execute remote Android ([adb](http://android-test-tw.blogspot.com/2012/10/android-linux-command-list.html)) and iOS ([idevice_id](http://manpages.ubuntu.com/manpages/trusty/man1/idevice_id.1.html), [ideviceinfo](http://manpages.ubuntu.com/manpages/trusty/man1/ideviceinfo.1.html), [etc](https://github.com/libimobiledevice/libimobiledevice/tree/master/docs)) commands with the same arguments, file I/O, and output streams as local commands.

For example, the local command to install an Android app is:

    adb -s HT9CYP123456 install /private/Test.apk

We can provide remote access by running the proxy server:

    ./lab_device_proxy_server.py  # or use upstart

which will allow a remote client to invoke the same adb command:

    ./lab_device_proxy_client.py \
        --url http://foo.com:8084 \
        adb -s HT9CYP123456 install /local/Test.apk

Note that this will install the *client's* local file, not the server's file -- the proxy intentionally hides the server's file system from the client.

The above command can be simplified by setting:

    export LAB_DEVICE_PROXY_URL=http://foo.com:8084

    # create symlink anywhere in your $PATH
    ln -s lab_device_proxy_client.py /usr/local/bin/adb 

    # Same API as if "adb" were local :)
    adb -s HT9CYP123456 install /local/Test.apk


Requirements
------------

Linux, OS X, and BSD are supported and known to work.  Windows hasn't been tested yet.

The proxy client and server both require [Python 2.7](https://www.python.org/download/releases/2.7) or newer.

The proxy server executes "adb" and "idevice\*" commands on demand.  Both are optional -- if you never plan to control iOS devices, you don't need to install "idevice\*" -- otherwise see [Android SDK's platform-tools/](http://developer.android.com/sdk/index.html) and [libimobiledevice](http://www.libimobiledevice.org/).  On Linux the idevice\* commands require the "usbmuxd" daemon to be running, as noted on the libimobiledevice page.

Installation
------------

The client (lab_device_proxy_client.py) is a self-contained Python script, and can be installed in any directory.

The server (lab_device_proxy_server.py) reuses the client, so both Python files must be installed in the same directory.  The server looks for the adb and idevice\* command in the $PATH.

You can simply run `./lab_device_proxy_server.py`, as noted below in "Usage", or you can install the following optional startup scripts:

   1. Install the two Python files (if using a different path, modify the below \*\_conf files accordingly):

        sudo cp lab_device_proxy_server.py /usr/local/bin/
        sudo cp lab_device_proxy_client.py /usr/local/bin/

   1. If you installed adb and/or the idevice\* commands in a path other than /usr/local/bin, modify the below \*\_conf file according.

   1. Verify user "nobody" and group "nobody" exist (or modify the below \*\_conf file):

        id nobody || fail 'missing user "nobody"'
        grep '^nobody:' /etc/group || sudo groupadd -g 99 nobody

   1. On Linux (upstart):

        sudo cp linux_conf /etc/init/lab-device-proxy.conf
        sudo chmod 644 /etc/init/lab-device-proxy.conf
        sudo service lab-device-proxy start

   1. On OS X (launchctl):

        sudo mkdir /var/log/lab_device_proxy
        sudo chmod 777 /var/log/lab_device_proxy
        sudo cp osx_conf /Library/LaunchDaemons/com.google.lab-device-proxy.plist 
        sudo chmod 644 /Library/LaunchDaemons/com.google.lab-device-proxy.plist
        sudo launchctl load /Library/LaunchDaemons/com.google.lab-device-proxy.plist

   1. On BSD (rc):

        sudo cp bsd_conf /usr/local/etc/rc.d/lab_device_proxy
        sudo chmod 644 /usr/local/etc/rc.d/lab_device_proxy
        sudo service lab_device_proxy start

Usage
-----

See the top-level introduction for basic usage.

The proxy is command-specific, both for security and because it must identify which arguments represent input vs output files.


Enhancements Ideas
------------------

  1. Add caching, e.g.:
     1. Client sends "adb -s X install IN_MD5:Test.apk" with file checksum d7a0db7 instead of "/local/Test.apk"'s content.
     1. Server checks if d7a0db7 exists in local LRU cache, if not it returns an HTTP-417 "Precondition failed" error to client.
     1. Client handles the HTTP-417 error by re-sending the command with file content, via the usual "adb -s install IN:Test.apk".

  1. Improved access control, e.g.:
     1. We create a device manager host that authorizes device use.
     1. Client obtains (or is given) a signed token to use device X.
     1. Client provides its token in all server requests.
     1. Server verifies that the token is signed by the manager, the command is for X, and it hasn't seen a more recent token for X (in case the manager has re-allocated the device to a different client).

  1. Custom command validation, e.g.:
     1. Add an optional server "command" flag, similar to ssh [force command](http://oreilly.com/catalog/sshtdg/chapter/ch08.html#22858).  If specified, the server will call this script with each request's args (e.g. "adb -s X install IN:/tmp/uid/Foo.apk" but NUL-separated as in "find . -print0"), read the stdout (e.g. "adb -s X install /tmp/uid/Foo.apk" -- typically the exact same command w/o the IN/OUT's), verify the errcode is 0, then exec the returned command.
     1. Add a similar client pre-"command" flag.  If specified, the client will call this script with the user's command (e.g. "adb -s X install /data/Foo.apk"), read the stdout (e.g. "adb -s X install IN:/data/Foo.apk"), and send that to the server.


