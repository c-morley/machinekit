#!/usr/bin/python
import os
import sys
from stat import *
import zmq
import netifaces
import threading
import signal
import time
import argparse

import ConfigParser
from machinekit import service

from message_pb2 import Container
from config_pb2 import *
from types_pb2 import *


class ConfigServer:
    def __init__(self, context, appDirs=[], topdir=".",
                 ip="", svcUuid=None, debug=False, name=None, ipInName=True):
        self.appDirs = appDirs
        self.ip = ip
        self.name = name
        self.debug = debug
        self.shutdown = threading.Event()
        self.running = False
        self.cfg = ConfigParser.ConfigParser()

        for rootdir in self.appDirs:
            for root, subFolders, files in os.walk(rootdir):
                if 'description.ini' in files:
                    inifile = os.path.join(root, 'description.ini')
                    cfg = ConfigParser.ConfigParser()
                    cfg.read(inifile)
                    appName = cfg.get('Default', 'name')
                    description = cfg.get('Default', 'description')
                    appType = cfg.get('Default', 'type')
                    self.cfg.add_section(appName)
                    self.cfg.set(appName, 'description', description)
                    self.cfg.set(appName, 'type', appType)
                    self.cfg.set(appName, 'files', root)
                    if self.debug:
                        print(("name: " + cfg.get('Default', 'name')))
                        print(("description: " + cfg.get('Default', 'description')))
                        print(("type: " + cfg.get('Default', 'type')))
                        print(("files: " + root))

        self.rx = Container()
        self.tx = Container()
        self.topdir = topdir
        self.context = context
        self.baseUri = "tcp://" + self.ip
        self.socket = context.socket(zmq.ROUTER)
        self.port = self.socket.bind_to_random_port(self.baseUri)
        self.dsname = self.socket.get_string(zmq.LAST_ENDPOINT, encoding='utf-8')

        if self.name is None:
            self.name = "Machinekit"
        if ipInName:
            self.name = self.name + " on " + self.ip
        self.service = service.Service(type='config',
                                   svcUuid=svcUuid,
                                   dsn=self.dsname,
                                   port=self.port,
                                   ip=self.ip,
                                   name=self.name,
                                   debug=self.debug)

        self.publish()

        threading.Thread(target=self.process_sockets).start()
        self.running = True

    def process_sockets(self):
        poll = zmq.Poller()
        poll.register(self.socket, zmq.POLLIN)

        while not self.shutdown.is_set():
            s = dict(poll.poll(1000))
            if self.socket in s:
                self.process(self.socket)

        self.unpublish()
        self.running = False

    def publish(self):
        try:
            self.service.publish()
        except Exception as e:
            print (('cannot register DNS service' + str(e)))
            sys.exit(1)

    def unpublish(self):
        self.service.unpublish()

    def stop(self):
        self.shutdown.set()

    def typeToPb(self, type):
        if type == 'QT5_QML':
            return QT5_QML
        elif type == 'GLADEVCP':
            return GLADEVCP
        else:
            return JAVASCRIPT

    def send_msg(self, dest, type):
        self.tx.type = type
        txBuffer = self.tx.SerializeToString()
        if self.debug:
            print(("send_msg " + str(self.tx)))
        self.tx.Clear()
        self.socket.send_multipart([dest, txBuffer])

    def list_apps(self, origin):
        for name in self.cfg.sections():
            app = self.tx.app.add()
            app.name = name
            app.description = self.cfg.get(name, 'description')
            app.type = self.typeToPb(self.cfg.get(name, 'type'))
        self.send_msg(origin, MT_DESCRIBE_APPLICATION)

    def add_files(self, basePath, path, app):
        if self.debug:
            print(("add files " + path))
        for f in os.listdir(path):
            pathname = os.path.join(path, f)
            mode = os.stat(pathname).st_mode
            if S_ISREG(mode):
                filename = os.path.join(os.path.relpath(path, basePath), f)
                if self.debug:
                    print(("add " + pathname))
                    print(("name " + filename))
                fileBuffer = open(pathname, 'rb').read()
                appFile = app.file.add()
                appFile.name = filename
                appFile.encoding = CLEARTEXT
                appFile.blob = fileBuffer
            elif S_ISDIR(mode):
                self.add_files(basePath, pathname, app)

    def retrieve_app(self, origin, name):
        if self.debug:
            print(("retrieve app " + name))
        app = self.tx.app.add()
        app.name = name
        app.description = self.cfg.get(name, 'description')
        app.type = self.typeToPb(self.cfg.get(name, 'type'))
        self.add_files(self.cfg.get(name, 'files'),
                       self.cfg.get(name, 'files'), app)

        self.send_msg(origin, MT_APPLICATION_DETAIL)

    def process(self, s):
        if self.debug:
            print("process called")
        try:
            (origin, msg) = s.recv_multipart()
        except Exception as e:
            print(("Exception " + str(e)))
            return
        self.rx.ParseFromString(msg)

        if self.rx.type == MT_LIST_APPLICATIONS:
            self.list_apps(origin)
            return

        if self.rx.type == MT_RETRIEVE_APPLICATION:
            a = self.rx.app[0]
            self.retrieve_app(origin, a.name)
        return

        note = self.tx.note.add()
        note = "unsupported request type %d" % (self.rx.type)
        self.send_msg(origin, MT_ERROR)


def choose_ip(pref):
    '''
    given an interface preference list, return a tuple (interface, ip)
    or None if no match found
    If an interface has several ip addresses, the first one is picked.
    pref is a list of interface names or prefixes:

    pref = ['eth0','usb3']
    or
    pref = ['wlan','eth', 'usb']
    '''

    # retrieve list of network interfaces
    interfaces = netifaces.interfaces()

    # find a match in preference oder
    for p in pref:
        for i in interfaces:
            if i.startswith(p):
                ifcfg = netifaces.ifaddresses(i)
                # we want the first ip address
                try:
                    ip = ifcfg[netifaces.AF_INET][0]['addr']
                except KeyError:
                    continue
                return (i, ip)
    return None


shutdown = False


def _exitHandler(signum, frame):
    global shutdown
    shutdown = True


# register exit signal handlers
def register_exit_handler():
    signal.signal(signal.SIGINT, _exitHandler)
    signal.signal(signal.SIGTERM, _exitHandler)


def check_exit():
    global shutdown
    return shutdown


def main():
    parser = argparse.ArgumentParser(description='Configserver is the entry point for Machinetalk based user interfaces')
    parser.add_argument('-n', '--name', help='Name of the machine', default="Machinekit")
    parser.add_argument('-s', '--suppress_ip', help='Do not show ip of machine in service name', action='store_false')
    parser.add_argument('-d', '--debug', help='Enable debug mode', action='store_true')
    parser.add_argument('dirs', nargs='*', help="List of directories to scan for user interface configurations")

    args = parser.parse_args()

    debug = args.debug

    mkini = os.getenv("MACHINEKIT_INI")
    if mkini is None:
        sys.stderr.write("no MACHINEKIT_INI environemnt variable set")
        sys.exit(1)

    mki = ConfigParser.ConfigParser()
    mki.read(mkini)
    uuid = mki.get("MACHINEKIT", "MKUUID")
    remote = mki.getint("MACHINEKIT", "REMOTE")
    prefs = mki.get("MACHINEKIT", "INTERFACES").split()

    if remote == 0:
        print("Remote communication is deactivated, configserver will use the loopback interfaces")
        print(("set REMOTE in " + mkini + " to 1 to enable remote communication"))
        iface = ['lo', '127.0.0.1']
    else:
        iface = choose_ip(prefs)
        if not iface:
            sys.stderr.write("failed to determine preferred interface (preference = %s)" % prefs)
            sys.exit(1)

    if debug:
        print(("announcing configserver on " + str(iface)))

    context = zmq.Context()
    context.linger = 0

    register_exit_handler()

    configService = None

    try:
        configService = ConfigServer(context,
                       svcUuid=uuid,
                       topdir=".",
                       ip=iface[1],
                       appDirs=args.dirs,
                       name=args.name,
                       ipInName=bool(args.suppress_ip),
                       debug=debug)

        while configService.running and not check_exit():
            time.sleep(1)
    except Exception as e:
        print("exception")
        print(e)
    except:
        print("other exception")

    if debug:
        print("stopping threads")
    if configService is not None:
        configService.stop()

    # wait for all threads to terminate
    while threading.active_count() > 1:
        time.sleep(0.1)

    if debug:
        print("threads stopped")
    sys.exit(0)


if __name__ == "__main__":
    main()
