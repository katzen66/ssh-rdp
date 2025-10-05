#!/usr/bin/env python3
import os, sys, re, signal, stat
import dbus
import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst
from dbus.mainloop.glib import DBusGMainLoop

# Config
FIFO_PATH = "/tmp/gfifo"
FPS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

# Setup
DBusGMainLoop(set_as_default=True)
Gst.init(None)
loop = GLib.MainLoop()
bus = dbus.SessionBus()

request_iface = 'org.freedesktop.portal.Request'
screen_cast_iface = 'org.freedesktop.portal.ScreenCast'

portal = bus.get_object('org.freedesktop.portal.Desktop',
                        '/org/freedesktop/portal/desktop')

# State
pipeline = None
session = None
pipewire_fd = None
starting = False
out_fd = None  # FIFO writer FD for fdsink

# Helpers
def ensure_fifo(path):
    try:
        st = os.stat(path)
        if not stat.S_ISFIFO(st.st_mode):
            os.remove(path)
            os.mkfifo(path, 0o600)
    except FileNotFoundError:
        os.mkfifo(path, 0o600)

def gst_bus_cb(_bus, message):
    t = message.type
    if t == Gst.MessageType.ERROR:
        err, dbg = message.parse_error()
        # Common case when FIFO reader disappears -> EPIPE
        print(f"GStreamer error: {err}{' — '+dbg if dbg else ''}", flush=True)
        stop_stream()
    elif t == Gst.MessageType.EOS:
        print("EOS", flush=True)
        stop_stream()

def build_and_start_pipeline(node_id, size):
    global pipeline, pipewire_fd, out_fd
    if pipewire_fd is None:
        empty = dbus.Dictionary(signature="sv")
        fd_obj = portal.OpenPipeWireRemote(session, empty, dbus_interface=screen_cast_iface)
        pipewire_fd = int(fd_obj.take())

    width = height = None
    if size and len(size) == 2:
        width, height = int(size[0]), int(size[1])

    ensure_fifo(FIFO_PATH)

    # Open FIFO for writing; this will block until a reader connects.
    # Ignore SIGPIPE globally so we don't get killed on EPIPE.
    flags = os.O_WRONLY
    try:
        flags |= os.O_CLOEXEC
    except AttributeError:
        pass
    print(f"Waiting for reader on {FIFO_PATH} …", flush=True)
    out_fd = os.open(FIFO_PATH, flags)
    print("Reader connected; starting pipeline", flush=True)

    # Force BGRA on the raw caps
    caps_parts = ["video/x-raw,format=BGRA", f"framerate={FPS}/1"]
    if width and height:
        caps_parts += [f"width={width}", f"height={height}"]
    caps = ",".join(caps_parts)

    desc = (
        f"pipewiresrc fd={pipewire_fd} path={node_id} do-timestamp=true "
        f"! videoconvert ! videorate ! {caps} "
        f"! fdsink fd={out_fd} sync=false"
    )
    print(f"Pipeline: {desc}", flush=True)

    pipeline = Gst.parse_launch(desc)
    gbus = pipeline.get_bus()
    gbus.add_signal_watch()
    gbus.connect('message', gst_bus_cb)
    pipeline.set_state(Gst.State.PLAYING)

def stop_stream(*_):
    global pipeline, starting, out_fd
    if pipeline is not None:
        print("Stopping pipeline…", flush=True)
        try:
            pipeline.set_state(Gst.State.NULL)
        finally:
            pipeline = None
    # Close our FIFO FD if still open (fdsink may already have closed it)
    if out_fd is not None:
        try:
            os.close(out_fd)
        except OSError:
            pass
        out_fd = None
    starting = False

def terminate(*_):
    stop_stream()
    try:
        if session:
            sess_obj = bus.get_object('org.freedesktop.portal.Desktop', session)
            sess_obj.Close(dbus_interface='org.freedesktop.portal.Session')
    except Exception:
        pass
    loop.quit()

# Portal glue
request_token_counter = 0
session_token_counter = 0
sender_name = re.sub(r'\.', r'_', bus.get_unique_name()[1:])

def new_request_path():
    global request_token_counter
    request_token_counter += 1
    token = f"u{request_token_counter}"
    path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{token}"
    return (path, token)

def new_session_path():
    global session_token_counter
    session_token_counter += 1
    token = f"u{session_token_counter}"
    path = f"/org/freedesktop/portal/desktop/session/{sender_name}/{token}"
    return (path, token)

def portal_call(method, callback, *args, options=None):
    if options is None:
        options = {}
    req_path, req_token = new_request_path()
    bus.add_signal_receiver(
        callback,
        signal_name='Response',
        dbus_interface=request_iface,
        bus_name='org.freedesktop.portal.Desktop',
        path=req_path
    )
    options['handle_token'] = req_token
    method(*(args + (options,)), dbus_interface=screen_cast_iface)

# Flow
def on_start_response(response, results):
    global starting
    starting = False
    if response != 0:
        print(f"Start failed: {response}", flush=True)
        return

    streams = results.get('streams', [])
    if not streams:
        print("No streams returned on Start", flush=True)
        return

    stream = streams[0]
    try:
        node_id = int(stream.get('node_id'))
        size = stream.get('size')
    except AttributeError:
        node_id = int(stream[0])
        props = stream[1] if len(stream) > 1 else {}
        size = props.get('size') if isinstance(props, dict) else None

    print(f"Start OK: node_id={node_id}{f', size={size[0]}x{size[1]}' if size else ''}", flush=True)
    build_and_start_pipeline(node_id, size)

def request_start():
    global starting
    if session is None:
        print("Cannot start: session not ready", flush=True)
        return
    if pipeline is not None or starting:
        print("Already streaming or starting; ignoring", flush=True)
        return
    print("Requesting Start on existing session…", flush=True)
    starting = True
    portal_call(portal.Start, on_start_response, session, '', options={})

def on_select_sources_response(response, _results):
    if response != 0:
        print(f"SelectSources failed: {response}", flush=True)
        terminate()
        return
    print("Sources selected; requesting initial Start…", flush=True)
    request_start()

def on_create_session_response(response, results):
    global session
    if response != 0:
        print(f"CreateSession failed: {response}", flush=True)
        terminate()
        return
    session = results['session_handle']
    print(f"Session created: {session}", flush=True)

    portal_call(
        portal.SelectSources, on_select_sources_response, session,
        options={
            'multiple': False,
            'types': dbus.UInt32(1 | 2),    # Monitor | Window
            'cursor_mode': dbus.UInt32(1),  # Include cursor
        }
    )

def restart_session(*_):
    """Close old session and start a new one from scratch."""
    global session, pipeline, pipewire_fd, starting, out_fd
    print("Restarting session from scratch…", flush=True)
    stop_stream()
    if session:
        try:
            sess_obj = bus.get_object('org.freedesktop.portal.Desktop', session)
            sess_obj.Close(dbus_interface='org.freedesktop.portal.Session')
        except Exception as e:
            print(f"(Warning) Failed to close session: {e}", flush=True)
    session = None
    pipewire_fd = None
    starting = False
    out_fd = None
    (session_path, session_token) = new_session_path()
    portal_call(
        portal.CreateSession, on_create_session_response,
        options={'session_handle_token': session_token}
    )

# Signals
signal.signal(signal.SIGINT, terminate)
signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGUSR1, lambda *_: stop_stream())  # stop
signal.signal(signal.SIGUSR2, restart_session)           # restart session
signal.signal(signal.SIGPIPE, signal.SIG_IGN)            # don't die when FIFO reader closes

if __name__ == "__main__":
    print(f"PID {os.getpid()} — USR1=stop, USR2=restart session", flush=True)
    (session_path, session_token) = new_session_path()
    portal_call(
        portal.CreateSession, on_create_session_response,
        options={'session_handle_token': session_token}
    )
    try:
        loop.run()
    finally:
        terminate()
