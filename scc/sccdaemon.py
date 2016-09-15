#!/usr/bin/env python2
"""
SC-Controller - Daemon class
"""
from __future__ import unicode_literals
from scc.tools import _

from scc.lib import xwrappers as X
from scc.lib.daemon import Daemon
from scc.lib.usb1 import USBError
from scc.constants import SCButtons, LEFT, RIGHT, STICK, DAEMON_VERSION
from scc.paths import get_menus_path, get_default_menus_path
from scc.tools import find_profile, nameof, shsplit, shjoin
from scc.tools import set_logging_level, find_binary, clamp
from scc.parser import TalkingActionParser
from scc.menu_data import MenuData
from scc.uinput import Keys, Axes
from scc.profile import Profile
from scc.actions import Action
from scc.config import Config
from scc.mapper import Mapper
import scc.drivers

from SocketServer import UnixStreamServer, ThreadingMixIn, StreamRequestHandler
import os, sys, pkgutil, signal, socket, select, time, json, logging
import threading, traceback, subprocess
log = logging.getLogger("SCCDaemon")
tlog = logging.getLogger("Socket Thread")

class ThreadingUnixStreamServer(ThreadingMixIn, UnixStreamServer): daemon_threads = True


class SCCDaemon(Daemon):
	
	def __init__(self, piddile, socket_file):
		set_logging_level(True, True)
		Daemon.__init__(self, piddile)
		self.started = False
		self.exiting = False
		self.socket_file = socket_file
		self.xdisplay = None
		self.sserver = None			# UnixStreamServer instance
		self.error = None
		self.alone = False			# Set by launching script from --alone flag
		self.osd_daemon = None
		self.default_profile = None
		self.autoswitch_daemon = None
		self.controllers = []
		self.mainloops = []
		self.subprocs = []
		self.lock = threading.Lock()
		self.default_mapper = self.init_default_mapper()
		self.clients = set()
		self.cwd = os.getcwd()
	
	
	def init_drivers(self):
		"""
		Searchs and initializes all controller drivers.
		See __init__.py in scc.drivers.
		"""
		log.debug("Initializing drivers...")
		self._to_start = set()	# del-eted later by start_drivers
		for importer, modname, ispkg in pkgutil.walk_packages(path=scc.drivers.__path__, onerror=lambda x: None):
			if not ispkg and modname != "driver":
				mod = getattr(__import__('scc.drivers.%s' % (modname,)).drivers, modname)
				if hasattr(mod, "init"):
					getattr(mod, "init")(self)
				if hasattr(mod, "start"):
					self._to_start.add(getattr(mod, "start"))
		self.mainloops = tuple(self.mainloops)
	
	
	def init_default_mapper(self):
		"""
		default_mapper is persistent mapper assigned to first Controller instance.
		Even if all controllers are removed, this mapper stays active. It exists mainly
		to provide backwards compatibility and to allow GUI and external programs to
		work even if there is no controller connected.
		"""
		# But, despite all above, it's just mapper as every other :)
		return self.init_mapper()
	
	
	def set_default_profile(self, profile_file):
		"""
		Sets profile that is used for first available controller
		"""
		self.default_profile = profile_file
	
	
	def start_drivers(self):
		for s in self._to_start:
			s(self)
		del self._to_start
	
	
	def add_to_mainloop(self, fn):
		"""
		Adds function that is called in every mainloop iteration.
		Can be called only durring initialization, in driver 'init' method.
		"""
		if fn not in self.mainloops:
			self.mainloops.append(fn)
	
	
	def _send_to_all(self, message_str):
		"""
		Sends message to all connect clients.
		Should be called while lock is acquired.
		Message should be utf-8 encoded str.
		"""
		for client in self.clients:
			try:
				client.wfile.write(message_str)
			except: pass
	
	
	def on_sa_turnoff(self, mapper, action):
		""" Called when 'turnoff' action is used """
		if mapper.get_controller():
			mapper.get_controller().turnoff()
	
	
	def on_sa_led(self, mapper, action):
		""" Called when 'turnoff' action is used """
		if mapper.get_controller():
			mapper.get_controller().set_led_level(action.brightness)
	
	
	def on_sa_shell(self, mapper, action):
		""" Called when 'shell' action is used """
		os.system(action.command.encode('string_escape') + " &")
	
	
	def _osd(self, *data):
		""" Returns True on success """
		# Pre-format data
		data = b"OSD: %s\n" % (shjoin(data) ,)
		
		# Check if scc-osd-daemon is available
		with self.lock:
			if not self.osd_daemon:
				log.warning("Cannot show OSD; there is no scc-osd-daemon registered")
				return False
			# Send request
			try:
				self.osd_daemon.wfile.write(data)
				self.osd_daemon.wfile.flush()
			except Exception, e:
				log.error("Failed to display OSD: %s", e)
				self.osd_daemon = None
				return False
			return True
	
	
	def on_sa_osd(self, mapper, action):
		""" Called when 'osd' action is used """
		self._osd('message', '-t', action.timeout, action.text)
	
	
	def on_sa_area(self, mapper, action, x1, y1, x2, y2):
		""" Called when *AreaAction has OSD enabled """
		self._osd('area', '-x', x1, '-y', y1, '--width', x2-x1, '--height', y2-y1)
	
	
	def on_sa_clear_osd(self, *a):
		self._osd('clear')
	
	
	def on_sa_keyboard(self, mapper, action):
		""" Called when 'keyboard' action is used """
		self._osd('keyboard')
	
	
	def on_sa_menu(self, mapper, action, *pars):
		""" Called when 'osd' action is used """
		p = [ action.MENU_TYPE,
			"--confirm-with", nameof(action.confirm_with),
			"--cancel-with", nameof(action.cancel_with)
		]
		if "." in action.menu_id:
			path = None
			for d in ( get_menus_path(), get_default_menus_path() ):
				if os.path.exists(os.path.join(d, action.menu_id)):
					path = os.path.join(d, action.menu_id)
			if not path:
				log.error("Cannot show menu: Menu '%s' not found", action.menu_id)
				return
			p += [ "--from-file", path ]
		else:
			p += [ "--from-profile", mapper.profile.get_filename(), action.menu_id ]
		p += list(pars)
		
		self._osd(*p)
	
	on_sa_gridmenu = on_sa_menu
	
	
	def on_sa_profile(self, mapper, action):
		""" Called when 'profile' action is used """
		name = action.profile
		if "/" in name:
			# Small sanity check
			log.error("Cannot load profile: Profile '%s' not found", name)
			return
		path = find_profile(name)
		if path:
			with self.lock:
				try:
					self._set_profile(path)
					log.info("Loaded profile '%s'", name)
				except Exception, e:
					log.error(e)
			return
		log.error("Cannot load profile: Profile '%s' not found", name)
	
	
	def on_start(self):
		os.chdir(self.cwd)
	
	
	def on_controller_status(self, sc, onoff):
		if onoff:
			log.debug("Controller turned ON")
		else:
			log.debug("Controller turned OFF")
	
	
	def sigterm(self, *a):
		self.exiting = True
		for d in (self.osd_daemon, self.autoswitch_daemon):
			if d: d.wfile.close()
		self.osd_daemon, self.autoswitch_daemon = None, None
		for p in self.subprocs:
			p.kill()
		self.subprocs = []
		sys.exit(0)
	
	
	def connect_x(self):
		""" Creates connection to X Server """
		if "DISPLAY" not in os.environ:
			log.warning("DISPLAY env variable not set. Some functionality will be unavailable")
			self.xdisplay = None
			return
		
		self.xdisplay = X.open_display(os.environ["DISPLAY"])
		if self.xdisplay:
			log.debug("Connected to XServer %s", os.environ["DISPLAY"])
			if not self.alone:
				self.subprocs.append(Subprocess("scc-osd-daemon", True))
				if len(Config()["autoswitch"]):
					# Start scc-autoswitch-daemon only if there are some switch rules defined
					self.subprocs.append(Subprocess("scc-autoswitch-daemon", True))
		else:
			log.warning("Failed to connect to XServer. Some functionality will be unavailable")
			self.xdisplay = None
	
	
	def init_mapper(self):
		"""
		Setups new mapper instance.
		"""
		mapper = Mapper(Profile(TalkingActionParser()))
		mapper.set_special_actions_handler(self)
		mapper.set_xdisplay(self.xdisplay)
		return mapper
	
	
	def load_default_profile(self):
		try:
			self.default_mapper.profile.load(self.default_profile).compress()
		except Exception, e:
			log.warning("Failed to load profile. Starting with no mappings.")
			log.warning("Reason: %s", e)
	
	
	def add_controller(self, c):
		if len(self.controllers) == 0:
			# First controller added, use default_mapper for it
			c.set_mapper(self.default_mapper)
		else:
			c.set_mapper(self.init_mapper())
		
		if len(self.controllers) == 1:
			# TODO: Remove this
			c.get_mapper().profile.load(find_profile("XBox Controller")).compress()
		
		self.controllers.append(c)
		log.debug("Controller added: %s", c)
		with self.lock:
			self._send_to_all(("Controller count: %s\n" % (len(self.controllers),)).encode("utf-8"))
	
	
	def remove_controller(self, c):
		c.mapper.release_virtual_buttons()
		c.set_controller(None)
		while c in self.controllers:
			self.controllers.remove(c)
		log.debug("Controller removed: %s", c)
		with self.lock:
			self._send_to_all(("Controller count: %s\n" % (len(self.controllers),)).encode("utf-8"))
	
	
	def run(self):
		log.debug("Starting SCCDaemon...")
		signal.signal(signal.SIGTERM, self.sigterm)
		self.init_drivers()
		self.load_default_profile()
		self.lock.acquire()
		self.start_listening()
		self.connect_x()
		self.start_drivers()
		self.lock.release()
		
		while True:
			for fn in self.mainloops:
				fn()
		
		'''
		while True:
			try:
				sc = None
				sc = SCController(callback=self.mapper.callback)
				sc.configure(enable_gyros=bool(self.mapper.profile.gyro), led_level=Config()["led_level"])
				self.mapper.set_controller(sc)
				sc.setStatusCallback(self.on_controller_status)
				if self.error is not None:
					self.error = None
					log.debug("Recovered after error")
					self._send_to_all(b"Ready.\n")
				self.lock.release()
				sc.run()
				# Reaches here only if USB dongle is disconnected or gets stuck
				self.mapper.release_virtual_buttons()
			except (ValueError, USBError), e:
				# When SCController fails to initialize, daemon should
				# still stay alive, so it is able to report this failure.
				#
				# As this is most likely caused by hw device being not
				# connected or busy, daemon will also repeadedly try to
				# reinitialize SCController instance expecting error to be
				# fixed by higher power (aka. user)
				was_error = self.error is not None
				self.error = unicode(e)
				if sc:
					try:
						sc.unclaim()
					except Exception, e:
						log.warning("Failed to unclaim device. Except bad things to happen.")
						log.warning(e)
				try:
					self.lock.release()
				except: pass
				log.error(e)
				if not was_error:
					self._send_to_all(("Error: %s\n" % (self.error,)).encode("utf-8"))
				self.mapper.release_virtual_buttons()
				time.sleep(5)
			self.lock.acquire()
			self.mapper.release_virtual_buttons()
		'''
	
	def start_listening(self):
		if os.path.exists(self.socket_file):
			os.unlink(self.socket_file)
		instance = self
		
		class SSHandler(StreamRequestHandler):
			def handle(self):
				instance._sshandler(self.connection, self.rfile, self.wfile)
		
		self.sserver = ThreadingUnixStreamServer(self.socket_file, SSHandler)
		t = threading.Thread(target=self.sserver.serve_forever)
		t.daemon = True
		t.start()
		os.chmod(self.socket_file, 0600)
		log.debug("Created control socket %s", self.socket_file)
	
	
	def _sshandler(self, connection, rfile, wfile):
		with self.lock:
			client = Client(connection, self.default_mapper, rfile, wfile)
			self.clients.add(client)
			wfile.write(b"SCCDaemon\n")
			wfile.write(("Version: %s\n" % (DAEMON_VERSION,)).encode("utf-8"))
			wfile.write(("PID: %s\n" % (os.getpid(),)).encode("utf-8"))
			wfile.write(("Current profile: %s\n" % (self.default_mapper.profile.get_filename(),)).encode("utf-8"))
			wfile.write(("Controller count: %s\n" % (len(self.controllers),)).encode("utf-8"))
			if self.error is None:
				wfile.write(b"Ready.\n")
			else:
				wfile.write(("Error: %s\n" % (self.error,)).encode("utf-8"))
		
		while True:
			try:
				line = rfile.readline()
			except Exception:
				# Connection terminated
				break
			if len(line) == 0: break
			if len(line.strip("\t\n ")) > 0:
				self._handle_message(client, line.strip("\n"))
		
		with self.lock:
			client.unlock_actions(self)
			if self.osd_daemon == client:
				log.info("scc-osd-daemon lost")
				self.osd_daemon = None
			if self.autoswitch_daemon == client:
				log.info("scc-autoswitch-daemon lost")
				self.autoswitch_daemon = None
			self.clients.remove(client)
	
	
	def _handle_message(self, client, message):
		"""
		Handles message recieved from client.
		"""
		if message.startswith("Profile:"):
			with self.lock:
				try:
					filename = message[8:].decode("utf-8").strip("\t ")
					self._set_profile(filename)
					log.info("Loaded profile '%s'", filename)
					client.wfile.write(b"OK.\n")
				except Exception, e:
					log.error(e)
					tb = unicode(traceback.format_exc()).encode("utf-8").encode('string_escape')
					client.wfile.write(b"Fail: " + tb + b"\n")
		elif message.startswith("OSD:"):
			if not self.osd_daemon:
				client.wfile.write(b"Fail: Cannot show OSD; there is no scc-osd-daemon registered\n")
			else:
				try:
					text = message[5:].decode("utf-8").strip("\t ")
					if not self._osd("message", text):
						raise Exception()
					client.wfile.write(b"OK.\n")
				except Exception:
					client.wfile.write(b"Fail: cannot display OSD\n")
		elif message.startswith("Led:"):
			try:
				number = int(message[4:].decode("utf-8"))
				number = clamp(0, number, 100)
			except Exception, e:
				client.wfile.write(b"Fail: %s\n" % (e,))
			if client.mapper.get_controller():
				client.mapper.get_controller().set_led_level(led_level)
		elif message.startswith("Observe:"):
			if Config()["enable_sniffing"]:
				to_observe = [ x for x in message.split(":", 1)[1].strip(" \t\r").split(" ") ]
				with self.lock:
					for l in to_observe:
						client.observe_action(self, SCCDaemon.source_to_constant(l))
					client.wfile.write(b"OK.\n")
			else:
				log.warning("Refused 'Observe' request: Sniffing disabled")
				client.wfile.write(b"Fail: Sniffing disabled.\n")
		elif message.startswith("Lock:"):
			to_lock = [ x for x in message.split(":", 1)[1].strip(" \t\r").split(" ") ]
			with self.lock:
				try:
					for l in to_lock:
						if not self._can_lock_action(client.mapper, SCCDaemon.source_to_constant(l)):
							client.wfile.write(b"Fail: Cannot lock " + l.encode("utf-8") + b"\n")
							return
				except ValueError, e:
					tb = unicode(traceback.format_exc()).encode("utf-8").encode('string_escape')
					client.wfile.write(b"Fail: " + tb + b"\n")
					return
				for l in to_lock:
					client.lock_action(self, SCCDaemon.source_to_constant(l))
				client.wfile.write(b"OK.\n")
		elif message.startswith("Unlock."):
			with self.lock:
				client.unlock_actions(self)
				client.wfile.write(b"OK.\n")
		elif message.startswith("Reconfigure."):
			with self.lock:
				# Start or stop scc-autoswitch-daemon as needed
				need_autoswitch_daemon = len(Config()["autoswitch"]) > 0
				if need_autoswitch_daemon and self.xdisplay and not self.autoswitch_daemon:
					self.subprocs.append(Subprocess("scc-autoswitch-daemon", True))
				elif not need_autoswitch_daemon and self.autoswitch_daemon:
					self._remove_subproccess("scc-autoswitch-daemon")
					self.autoswitch_daemon.close()
					self.autoswitch_daemon = None
				try:
					client.wfile.write(b"OK.\n")
					self._send_to_all("Reconfigured.\n".encode("utf-8"))
				except:
					pass
		elif message.startswith("Selected:"):
			menuaction = None
			def press(mapper):
				try:
					menuaction.button_press(mapper)
					client.mapper.schedule(0.1, release)
				except Exception, e:
					log.error("Error while processing menu action")
					log.error(traceback.format_exc())
			def release(mapper):
				try:
					menuaction.button_release(mapper)
				except Exception, e:
					log.error("Error while processing menu action")
					log.error(traceback.format_exc())
			
			with self.lock:
				try:
					menu_id, item_id = shsplit(message)[1:]
					menuaction = None
					if "." in menu_id:
						# TODO: Move this common place
						data = json.loads(open(menu_id, "r").read())
						menudata = MenuData.from_json_data(data, TalkingActionParser())
						menuaction = menudata.get_by_id(item_id).action
					else:
						menuaction = client.mapper.profile.menus[menu_id].get_by_id(item_id).action
					client.wfile.write(b"OK.\n")
				except:
					log.warning("Selected menu item is no longer valid.")
					client.wfile.write(b"Fail: Selected menu item is no longer valid\n")
				if menuaction:
					client.mapper.schedule(0, press)
		elif message.startswith("Register:"):
			with self.lock:
				if message.strip().endswith("osd"):
					if self.osd_daemon: self.osd_daemon.close()
					self.osd_daemon = client
					log.info("Registered scc-osd-daemon")
				elif message.strip().endswith("autoswitch"):
					if self.autoswitch_daemon: self.autoswitch_daemon.close()
					self.autoswitch_daemon = client
					log.info("Registered scc-autoswitch-daemon")
				client.wfile.write(b"OK.\n")
		else:
			client.wfile.write(b"Fail: Unknown command\n")
	
	
	def _remove_subproccess(self, binary_name):
		"""
		Removes subproccess started with specified binary name from list of
		managed subproccesses, effectively preventing daemon from
		auto-restarting it.
		
		Should be called while lock is acquired
		"""
		n = []
		for i in self.subprocs:
			if i.binary_name == binary_name:
				i.mark_killed()
			else:
				n.append(i)
		self.subprocs = n
	
	
	def _can_lock_action(self, mapper, what):
		"""
		Returns True if action assigned to axis,
		pad or button is not yet locked.
		
		Should be called while self.lock is acquired.
		"""
		# TODO: Probably move to mapper
		is_locked = (lambda a: isinstance(a, LockedAction) or
			(isinstance(a, ObservingAction) and isinstance(a.original_action, LockedAction)))
		
		if what == STICK:
			if is_locked(mapper.profile.buttons[SCButtons.STICK]):
				return False
			if is_locked(mapper.profile.stick):
				return False
			return True
		if what == SCButtons.LT:
			return not is_locked(mapper.profile.triggers[LEFT])
		if what == SCButtons.RT:
			return not is_locked(mapper.profile.triggers[RIGHT])
		if what in SCButtons:
			return not is_locked(mapper.profile.buttons[what])
		if what in (LEFT, RIGHT):
			return not is_locked(mapper.profile.pads[what])
		return False
	
	
	def _apply(self, mapper, what, callback, *args):
		"""
		Applies callback on action that is currently set to input specified
		by 'what'. Raises ValueError if what is not known.
		
		For example, if what == STICK, executes
			mapper.profile.stick = callback(mapper.profile.stick, *args)
		"""
		if what == STICK:
			mapper.profile.stick = callback(mapper.profile.stick, *args)
		elif what == SCButtons.LT:
			mapper.profile.triggers[LEFT] = callback(mapper.profile.triggers[LEFT], *args)
		elif what == SCButtons.RT:
			mapper.profile.triggers[RIGHT] = callback(mapper.profile.triggers[RIGHT], *args)
		elif what in SCButtons:
			mapper.profile.buttons[what] = callback(mapper.profile.buttons[what], *args)
		elif what in (LEFT, RIGHT):
			if what == LEFT:
				mapper.buttons &= ~SCButtons.LPADTOUCH
			else:
				mapper.buttons &= ~SCButtons.RPADTOUCH
			a = callback(mapper.profile.pads[what], *args)
			a.whole(mapper, 0, 0, what)
			mapper.profile.pads[what] = a
		else:
			raise ValueError("Unknown source: %s" % (what,))
	
	
	@staticmethod
	def source_to_constant(s):
		"""
		Turns string as 'A', 'LEFT' or 'ABS_X' into one of SCButtons.*,
		LEFT, RIGHT or STICK constants.
		
		Raises ValueError if passed string cannot be converted.
		
		Used when parsing `Lock: ...` message
		"""
		s = s.strip(" \t\r\n")
		if s in (STICK, LEFT, RIGHT):
			return s
		if s == "STICKPRESS":
			# Special case, as that button is actually named STICK :(
			return SCButtons.STICK
		if hasattr(SCButtons, s):
			return getattr(SCButtons, s)
		raise ValueError("Unknown source: %s" % (s,))
	
	
	def _remove_socket(self):
		self.sserver.shutdown()
		if os.path.exists(self.socket_file):
			os.unlink(self.socket_file)
		log.debug("Control socket removed")
	
	
	def debug(self):
		set_logging_level(True, True)
		self.on_start()
		self.write_pid()
		try:
			self.run()
		except KeyboardInterrupt:
			log.debug("Break")
		self.sigterm()


class Client(object):
	def __init__(self, connection, mapper, rfile, wfile):
		self.connection = connection
		self.rfile = rfile
		self.wfile = wfile
		self.mapper = mapper
		self.locked_actions = set()
		self.observed_actions = set()
	
	
	def close(self):
		""" Closes connection to this client """
		try:
			self.connection.shutdown(True)
		except:
			pass
	
	
	def lock_action(self, daemon, what):
		"""
		Locks action so event can be send to client instead of handling it.
		
		Should be called while daemon.lock is acquired.
		"""
		def lock(action, what):
			# ObservingAction should be above LockedAction
			if isinstance(action, ObservingAction):
				action.original_action = LockedAction(what, self, action.original_action)
				return action
			return LockedAction(what, self, action)
		
		daemon._apply(self.mapper, what, lock, what)
	
	
	def observe_action(self, daemon, what):
		"""
		Enables observing of action so event is both sent to client and handled.
		
		Should be called while daemon.lock is acquired.
		"""
		daemon._apply(self.mapper, what, lambda a : ObservingAction(what, self, a))
	
	
	def unlock_actions(self, daemon):
		""" Should be called while daemon.lock is acquired """
		def unlock(a):
			if isinstance(a, ObservingAction):
				# Needs to be handled specifically, as it is in lock_action
				a.original_action = unlock(a.original_action)
				return a
			return a.original_action
		
		s, self.locked_actions = self.locked_actions, set()
		for a in s:
			daemon._apply(self.mapper, a.what, unlock)
			log.debug("%s unlocked", a.what)
		
		def unobserve(a):
			# I'm really proud of that name
			if isinstance(a, ObservingAction):
				if a.client == self:
					return a.original_action
				a.original_action = unobserve(a.original_action)
				return a
			if isinstance(a, LockedAction):
				a.original_action = unobserve(a.original_action)
				return a
			# Shouldn't be possible to reach here
			raise TypeError("Un-observing not observed action")
		
		s, self.observed_actions = self.observed_actions, set()
		for a in s:
			daemon._apply(self.mapper, a.what, unobserve)
			log.debug("%s no longer observed by %s", a.what, self)
	
	
	def reaply_locks(self, daemon):
		"""
		Called after profile is changed.
		Should be called while daemon.lock is acquired
		"""
		s, self.observed_actions = self.observed_actions, set()
		for a in s:
			self.observe_action(daemon, a.what)
		s, self.locked_actions = self.locked_actions, set()
		for a in s:
			self.lock_action(daemon, a.what)


class ReportingAction(Action):
	"""
	Action used to send requested inputs to client.
	Base for LockedAction and ObservingAction
	"""
	MIN_DIFFERENCE = 300
	
	def __init__(self, what, client):
		self.what = what
		self.client = client
		self.old_pos = 0, 0
	
	
	def trigger(self, mapper, position, old_position):
		self.client.wfile.write(("Event: %s %s %s\n" % (
			self.what.name, position, old_position)).encode("utf-8"))
	
	
	def button_press(self, mapper):
		if self.what == SCButtons.STICK:
			self.client.wfile.write(b"Event: STICKPRESS 1\n")
		else:
			self.client.wfile.write(("Event: %s 1\n" % (self.what.name,)).encode("utf-8"))
	
	
	def button_release(self, mapper):
		if self.what == SCButtons.STICK:
			self.client.wfile.write(b"Event: STICKPRESS 0\n")
		else:
			self.client.wfile.write(("Event: %s 0\n" % (self.what.name,)).encode("utf-8"))
	
	
	def whole(self, mapper, x, y, what):
		if abs(x - self.old_pos[0]) > self.MIN_DIFFERENCE or abs(y - self.old_pos[1] > self.MIN_DIFFERENCE):
			self.old_pos = x, y
			self.client.wfile.write(("Event: %s %s %s\n" % (what, x, y)).encode("utf-8"))


class LockedAction(ReportingAction):
	""" Temporal action used to send requested inputs to client """
	def __init__(self, what, client, original_action):
		ReportingAction.__init__(self, what, client)
		self.original_action = original_action
		self.client.locked_actions.add(self)
		log.debug("%s locked by %s", self.what, self.client)


class ObservingAction(ReportingAction):
	"""
	Similar to LockedAction, send inputs to client *and* executes actions.
	"""
	def __init__(self, what, client, original_action):
		ReportingAction.__init__(self, what, client)
		self.original_action = original_action
		self.client.observed_actions.add(self)
		log.debug("%s observed %s", self.what, self.client)
	
	
	def trigger(self, mapper, position, old_position):
		ReportingAction.trigger(self, mapper, position, old_position)
		self.original_action.trigger(mapper, position, old_position)
	
	
	def button_press(self, mapper):
		ReportingAction.button_press(self, mapper)
		self.original_action.button_press(mapper)
	
	
	def button_release(self, mapper):
		ReportingAction.button_release(self, mapper)
		self.original_action.button_release(mapper)
	
	
	def whole(self, mapper, x, y, what):
		ReportingAction.whole(self, mapper, x, y, what)
		self.original_action.whole(mapper, x, y, what)


class Subprocess(object):
	"""
	Part of scc-daemon executed as another process, killed along with scc-daemon.
	Currently scc-osd-daemon and scc-windowswitch-daemon.
	"""
	
	def __init__(self, binary_name, debug, restart_after=5):
		self.binary_name = binary_name
		self.restart_after = restart_after
		self.args = [ sys.executable, find_binary(binary_name) ]
		if debug:
			self.args.append('debug')
		self._killed = False
		self.p = None
		self.t = threading.Thread(target=self._threaded)
		self.t.daemon = True
		self.t.start()
	
	
	def _threaded(self, *a):
		while not self._killed:
			self.p = subprocess.Popen(self.args, stdin=None)
			self.p.communicate()
			self.p = None
			if not self._killed:
				log.warning("%s died; restarting after %ss",
					self.binary_name, self.restart_after)
				time.sleep(self.restart_after)
	
	
	def mark_killed(self):
		"""
		Prevents subprocess from being automatically restarted, but doesn't
		really kill it.
		"""
		self._killed = True
	
	
	def kill(self):
		self.mark_killed()
		if self.p:
			self.p.kill()
		self.p = None
