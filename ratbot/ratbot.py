#! /usr/bin/env python
#
# Example program using irc.bot.
#
# Joel Rosdahl <joel@rosdahl.net>

"""A simple example bot.

"""

# stdlib imports
import logging
import logging.handlers
import json
from datetime import datetime, timedelta
import re
import os
from contextlib import contextmanager
# 3Plib imports
import irc.bot
import irc.strings
from irc.buffer import LenientDecodingLineBuffer
from irc.client import ip_numstr_to_quad, ip_quad_to_numstr, Connection, ServerConnection
# custom imports
from botlib import processing
from botlib.systemsearch import Systemsearch
import botlib.systemsearch

ServerConnection.buffer_class = LenientDecodingLineBuffer

FACTS = json.load(open('facts.json'))

logging.getLogger().setLevel(logging.INFO)

class Case:
  def __init__(self, client, active=True, msg=None, idx=None):
    self.client = client
    self.active = active
    self.idx=None
    self.rats = []
    self.buffer = [msg] if msg is not None else []

  def serialize(self):
    return [ self.client, self.active, self.rats, self.buffer ]

  def deserialize(v, idx):
    c = Case(v[0], v[1])
    c.idx = idx
    c.rats = v[2]
    c.buffer = v[3]
    return c

  def __str__(self):
    return "{} [{}]".format(self.client, self.idx)

  def __repr__(self):
    return str(self)

class Board:
  def __init__(self, file='board.json'):
    self.botlogger = logging.getLogger('RatBotLogger')
    self.file = file
    self.cases = {}
    self.caselist = []
    if os.path.isfile(self.file):
      with open(self.file) as f:
        self.caselist = [Case.deserialize(c,i) for i,c in enumerate(json.load(f))]
        self.cases = dict([(c.client.lower(), c) for c in self.caselist])

  @contextmanager
  def get(self, name):
    try:
      case = self.cases.get(name.lower())
      if case is None:
        case = self.caselist[int(name)]
    except (ValueError, KeyError):
      case = None
      pass
    yield case
    if case is not None:
      self.save()

  def append(self, client):
    inserted = False
    for i,c in enumerate(self.caselist):
      if c is None:
        self.caselist[i] = client
        client.idx = i
        inserted = True
        break
    if not inserted:
      self.botlogger.debug(self.caselist)
      self.caselist.append(client)
      client.idx = len(self.caselist) - 1
    self.cases[client.client.lower()] = client
    self.save()

  def remove(self, client):
    if client in self.cases:
      self.caselist[self.cases[client].idx] = None
      del self.cases[client]
      self.save()

  def save(self):
    with open(self.file, "w") as f:
      json.dump([c.serialize() for c in self.cases.values()], f)

class QConnection(Connection):
  socket = None

  def __init__(self, c, bot, proc):
    Connection.__init__(self, bot.reactor)
    self.c = c
    self.bot = bot
    self.proc = proc
    self.socket = proc.out_queue._reader

  def process_data(self):
    self.bot.cmd_readout(self.c, [self.proc.pid], None, None)

class RatBotError(Exception):
  pass

class RatBotKilledError(RatBotError):
  pass

class RatBotResetError(RatBotError):
  pass

class TestBot(irc.bot.SingleServerIRCBot):
  """
  Constructor
  Sets up irc bot and instance fields
  """
  def __init__(self, channels, nickname, server, port=6667, debug=False):
    irc.bot.SingleServerIRCBot.__init__(self, [(server, port)], nickname, nickname)
    self.debug = debug
    self.reset = None

    self.botlogger = logging.getLogger('RatBotLogger')

    self.botlogger.info('Ratbot started')
    self.realnick = nickname
    self._channels = channels
    self.chanlog = {}
    self.cases = Board()
    self.processes = {}
    self.processes_by_qout = {}
    self.cooldown = {}
    self.silenced = False
    self.cmd_handlers = {
        # bot management
        'die': [ 'Kills the bot.', [], self.cmd_die, True, 0 ],
        'reset': [ 'This command resets the bot', [], self.cmd_reset, False, 1 ],
        'join': [ 'Makes the bot join a channel', ['channel name'],
          self.cmd_join, True, 2 ],
        'part': [ 'Makes the bot part a channel',
          ['channel name (current channel if parameter not present and command issued from a channel)'], self.cmd_part, True, 3 ],
        'help': [ 'Prints help message listing commands', [], self.cmd_help, False, 4 ],
        # search
        'search': [ 'Search for a system',
          ['-x Restricted search: Only search systems with similar length to search term',
           '-d Distance search: Try to find close simple-named system',
           '-l / -ll / -lll Large radius: Search for close systems in 20 / 30 / 50Ly radius instead of 10',
           '-r Reload system list'
           'System'
          ],
          self.cmd_search, False, 5],
        # facts
        'fact': [ 'Recites a fact',
          ['Name of fact, empty prints all available facts', 'Nick to address fact to'],
          self.cmd_fact, False, 6 ],
        # board
        'grab': [ 'Grabs last message from nick',
          ['Nick to grab'],
          self.cmd_grab, False, 7 ],
        'quote': [ 'Recites grabbed messages from a nick',
          ['Previously grabbed nick'],
          self.cmd_quote, False, 8 ],
        'clear': [ 'Clears grab list',
          ['Nick'],
          self.cmd_clear, False, 9 ],
        'list': [ 'Lists grabs',
          ['-i Inactive cases'],
          self.cmd_list, False, 10 ],
        'inject': [ 'Injects custom text into grab list',
          ['Nick to inject for', 'Message'],
          self.cmd_inject, False, 11 ],
        'sub': [ 'Replace/Remove grabbed line',
          ['Nick', 'Line no', 'Message'],
          self.cmd_sub, False, 12 ],
        'active': [ 'Toggles active status of a case',
          ['Nick'],
          self.cmd_active, False, 12 ],
        'assign': [ 'Assign rats to a case',
          ['Client nick', '[Rat nicks] (assign self if not given)'],
          self.cmd_assign, False, 13 ],
        # misc
        'masters': ['Lists masters', [], self.cmd_masters, False, 14 ],
        'silence': ['Toggles verbosity', [], self.cmd_silence, False, 15]
        }

  """
  IRC event handlers
  """
  def on_nicknameinuse(self, c, e):
    if self.realnick == c.get_nickname():
      c.nick(c.get_nickname() + "_")

  def on_welcome(self, c, e):
    for channel in self._channels:
      self.botlogger.debug('Joining %s' % channel)
      c.join(channel)

  """
  IRC message handlers
  """
  def on_privmsg(self, c, e):
    try:
      self.do_command(c, e, e.arguments[0])
    except:
      self.botlogger.error("Error while processing: {}".format(e.arguments))
      raise

  def on_pubmsg(self, c, e):
    try:
      if self.realnick != c.get_nickname():
        c.nick(self.realnick)

      if not e.target in self.chanlog:
        self.chanlog[e.target] = {}
      self.chanlog[e.target][e.source.nick.lower()] = e.arguments[0]

      #self.botlogger.debug('Pubmsg arguments: {}'.format(e.arguments))
      if e.arguments[0].startswith('!') and len(e.arguments[0]) > 1:
        self.botlogger.debug('detected command {}'.format(e.arguments[0][1:]))
        self.do_command(c, e, e.arguments[0][1:])
      elif e.arguments[0].lower().startswith('ratsignal') and not self.silenced:
        self.do_command(c, e, 'grab ' + e.source.nick)
      else:
        a = e.arguments[0].split(":", 1)
        if len(a) > 1 and len(a[0]) > 0 and irc.strings.lower(a[0]) == irc.strings.lower(self.connection.get_nickname()):
          self.do_command(c, e, a[1].strip())
    except:
      self.botlogger.error("Error while processing: {}".format(e.arguments))
      raise


  """
  Command parsing
  """
  def do_command(self, c, e, cmd):
    nick = irc.strings.IRCFoldedCase(e.source.nick)
    c = self.connection

    split = cmd.split()

    cmd = split[0]
    args = split[1:]
    from_channel = e.target if e.type == "pubmsg" else None

    self.botlogger.info('Got command {} from {} via {}'.format(cmd, nick, e.type))

    if cmd in self.cmd_handlers:
      chan = self.channels.get(from_channel)
      privers = list(chan.opers()) + list(chan.voiced()) + list(chan.owners()) + list(chan.halfops()) if chan is not None else []

      if ((self.cmd_handlers[cmd][3]) == False) or (nick in privers):
        self.cmd_handlers[cmd][2](c, args, nick, from_channel)
      else:
        self.reply(c, nick, e.target, "Privileged operation - can only be called from a channel by someone having ~@%+ flag")
    elif cmd in FACTS:
      self.cmd_handlers['fact'][2](c, [cmd] + args, nick, from_channel)

  """
  Command handlers
  """

  """
  System commands
  """
  def cmd_die(self, c, params, sender_nick, from_channel):
    self.botlogger.info("Killed by " + sender_nick)
    self.reset = False
    if len(params) > 0:
      self.die(" ".join(params))
    else:
      self.die("Killed by !die")

  def cmd_reset(self, c, params, sender_nick, from_channel):
    self.botlogger.info("Reset by " + sender_nick)
    self.reset = True
    self.die("Killed by !reset")

  def cmd_join(self, c, params, sender_nick, from_channel):
    if len(params) > 0:
      c.join(params[0])
    else:
      self.reply(c, sender_nick, from_channel, "Failed - Please specify a channel to join")

  def cmd_part(self, c, params, sender_nick, from_channel):
    chan = None
    if len(params) > 0:
      chan = params[0]
    elif from_channel != None:
      chan = from_channel
    else:
      self.reply(c,sender_nick, from_channel, "Failed - Where do you want me to part from?")
    if chan is not None:
      c.part(chan)

  def cmd_help(self, c, params, sender_nick, from_channel):
    ##self.botlogger.debug(json.dumps(self.cmd_handlers, indent=2, default=lambda o: 'INVALID'))
    self.reply(c,sender_nick, None, "Commands:")
    for cmd, attribs in sorted(self.cmd_handlers.items(), key=lambda k: k[1][4]):
      self.reply(c,sender_nick, None, "  {0:10}: {1}{2}".format(
        cmd,
        attribs[0],
        " (Privileged)" if attribs[3] else ""
        ))
      for switch in attribs[1]:
        self.reply(c,sender_nick, None, "    " + switch)

  """
  Search
  """
  def cmd_search(self, c, params, sender_nick, from_channel):
    #self.botlogger.debug('Calling search')
    try:
      jp = " ".join(params)
      if jp in self.cooldown:
        delta = datetime.now() - self.cooldown[jp]
        if delta < timedelta(seconds=180):
          self.reply(c, sender_nick, from_channel, "I'm afraid I can't do that Dave. This search was just started {}s ago".format(delta.seconds))
      else:
        self.cooldown[jp] = datetime.now()
        proc = processing.ProcessManager(params, sender_nick=sender_nick, from_channel=from_channel)
        self.botlogger.info("Received command: "+" ".join(params))
        self.processes[proc.pid]=proc
        self.processes_by_qout[proc.out_queue._reader]=proc
        #self.select_on.append(proc.out_queue._reader)
        qconn = QConnection(c, self, proc)
        self.reactor.connections.append(qconn)

        self.reply(c,sender_nick, from_channel, proc.start_result)
        return proc
    except:
      self.reply(c,sender_nick, from_channel, "Failed to start process")
      self.botlogger.exception("Failed to start process")
      return None

  """
  Board
  """
  ratsignalre = re.compile("ratsignal", re.I)
  def cmd_grab(self, c, params, sender_nick, from_channel):
    if from_channel is None:
      self.reply(c, sender_nick, from_channel, "This command only works in a channel")
    elif len(params) < 1:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick to grab")
    else:
      grabnick = params[0]
      line = self.chanlog.get(from_channel, {}).get(grabnick.lower(), None)
      if line is None:
        self.reply(c, sender_nick, from_channel, "Sorry, couldn't find a grabbable line, did you misspell the nick?")
      else:
        line = self.ratsignalre.sub("R@signal", line)

        with self.cases.get(grabnick.lower()) as case:
          if case is not None:
            case.buffer.append(line)
          else:
            self.cases.append(Case(grabnick, True, line))
        if not self.silenced:
          self.reply(c, sender_nick, from_channel, "Grabbed '{}' from {}".format(line, grabnick))

  def cmd_quote(self, c, params, sender_nick, from_channel):
    if len(params) < 1:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick to quote")
    else:
      grabnick = params[0]
      with self.cases.get(grabnick.lower()) as case:
        lines = case.buffer if case is not None else None
        if lines is None:
          self.reply(c, sender_nick, from_channel, "Sorry, couldn't find grabbed lines, did you misspell the nick?")
        else:
          if len(case.rats) > 0:
            self.reply(c, sender_nick, from_channel, "Rats on case: {}".format(", ".join(case.rats)))
          for i in range(len(lines)):
            line = lines[i]
            self.reply(c, sender_nick, from_channel, "<{}> {} [{}]".format(case.client, line, i))

  def cmd_clear(self, c, params, sender_nick, from_channel):
    if len(params) > 0:
      with self.cases.get(params[0].lower()) as case:
        if case is not None:
          self.cases.remove(case.client.lower())
          self.reply(c, sender_nick, from_channel, "Cleared {}, {}".format(params[0], "Board is clear!" if len(self.cases.cases) <= 0 else "{} left on the board".format(len(self.cases.cases))))
        else:
          self.reply(c, sender_nick, from_channel, "Can't find {} on the board".format(params[0]))
    else:
      self.reply(c, sender_nick, from_channel, "Need a nick to clear")

  def cmd_list(self, c, params, sender_nick, from_channel):
    if len(self.cases.cases) > 0:
      active_cases = [str(c) for c in self.cases.caselist if c is not None and c.active]
      inactive_cases = [str(c) for c in self.cases.caselist if c is not None and not c.active]
      #self.reply(c, sender_nick, from_channel, "On the board: {}".format(", ".join([c.client + ' (Inactive)' if not c.active else '' for c in self.cases.values() if c.active or '-i' in self.params])))
      self.reply(c, sender_nick, from_channel, "Active cases: {}{}".format(", ".join(active_cases), "; Inactive cases: {}".format(", ".join(inactive_cases)) if '-i' in params else " (Plus {} inactive)".format(len(inactive_cases)) if len(inactive_cases) > 0 else ''))
    else:
      self.reply(c, sender_nick, from_channel, "Board is clear")

  def cmd_inject(self, c, params, sender_nick, from_channel):
    if len(params) < 2:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick and some text.")
    else:
      grabnick = params[0]
      grabtext = self.ratsignalre.sub("R@signal"," ".join(params[1:]))
      with self.cases.get(grabnick.lower()) as case:
        if case is not None:
          case.buffer.append("{} [INJECTED BY {}]".format(grabtext, sender_nick))
        else:
          case = Case(grabnick)
          case.buffer.append("{} [INJECTED BY {}]".format(grabtext, sender_nick))
          self.cases.append(case)
      if not self.silenced:
        self.reply(c, sender_nick, from_channel, "Added line for {}".format(grabnick))

  def cmd_sub(self, c, params, sender_nick, from_channel):
    if len(params) < 2:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick and a line index.")
      return
    grabnick = params[0]
    with self.cases.get(grabnick.lower()) as case:
      if case is None:
        self.reply(c, sender_nick, from_channel, "Can't find {} on the board.".format(grabnick))
        return
      try:
        lineno = int(params[1])
      except ValueError:
        self.reply(c, sender_nick, from_channel, "Cannot parse {} into a number.".format(params[1]))
        return
      if len(case.buffer) <= lineno:
        self.reply(c, sender_nick, from_channel, "There are only {} lines, can't use line no {}.".format(len(case.buffer), lineno))
        return
      if len(params) == 2:
        case.buffer.pop(lineno)
        if not self.silenced:
          self.reply(c, sender_nick, from_channel, "Line removed")
      else:
        grabtext = self.ratsignalre.sub("R@signal"," ".join(params[2:]))
        case.buffer[lineno] = "{} [INJECTED BY {}]".format(grabtext, sender_nick)
        if not self.silenced:
          self.reply(c, sender_nick, from_channel, "Subbed line no {} for {}".format(lineno, grabnick))

  def cmd_active(self, c, params, sender_nick, from_channel):
    if len(params) < 1:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick to search on the board")
      return
    grabnick = params[0]
    with self.cases.get(grabnick.lower()) as case:
      if case is None:
        self.reply(c, sender_nick, from_channel, "Can't find {} on the board.".format(grabnick))
        return
      case.active = not case.active
      if not self.silenced:
        self.reply(c, sender_nick, from_channel, "Case for {} is now {}".format(case.client, "Active" if case.active else "Inactive"))

  def cmd_assign(self, c, params, sender_nick, from_channel):
    if len(params) < 1:
      self.reply(c, sender_nick, from_channel, "Sorry, I need a nick to search on the board")
      return
    with self.cases.get(params[0].lower()) as case:
      if case is None:
        self.reply(c, sender_nick, from_channel, "Can't find {} on the board.".format(params[0]))
      else:
        ratnicks = params[1:] if len(params) > 1 else [sender_nick]
        case.rats.extend(ratnicks)
        if not self.silenced:
          self.reply(c, sender_nick, from_channel, "Assigned {} to {}.".format(", ".join(ratnicks), case.client))

  """
  Misc
  """
  def cmd_silence(self, c, params, sender_nick, from_channel):
    self.silenced = not self.silenced
    self.reply(c, sender_nick, from_channel, "I will make less noise now." if self.silenced else "Making more noise now!")

  def cmd_masters(self, c, params, sender_nick, from_channel):
# list(self.channels[e.target].opers()) + list(self.channels[e.target].voiced()) + list(self.channels[e.target].owners()) + list(self.channels[e.target].halfops()))
    if from_channel is None:
      self.reply(c, sender_nick, None, "Call this from a channel")
    else:
      self.reply(c,sender_nick, None, "Current masters in {}:".format(from_channel))
      chan = self.channels[from_channel]
      for t,l in [('Owners', chan.owners()), ('Opers', chan.opers()), ('Hops', chan.halfops()), ('Voicers', chan.voiced())]:
        self.reply(c, sender_nick, None, "{}: {}".format(t, ", ".join(l)))

  """
  Readout
  """
  def cmd_readout(self, c, params, sender_nick, from_channel):
    try:
      pid = int(params[0])
      proc = self.processes[pid]
      sender_nick = sender_nick or proc.sender_nick
      from_channel = from_channel or proc.from_channel
      while not proc.out_queue.empty():
        tp = proc.out_queue.get_nowait()
        if isinstance(tp, Exception):
          self.reply(c,sender_nick, from_channel,
              "\0034Unexpected Error\017: {}".format(tp))
        if isinstance(tp, Systemsearch):
          if tp.origin_systems is not None and len(tp.origin_systems) > 0:
            if '-d' in tp.args:
              plen = 1
            else:
              plen = len(tp.origin_systems)
            for rec in tp.origin_systems[:plen]:
              self.reply(c,sender_nick, from_channel,
                  "Found system \002%s\017 (\003%sMatching %d%%\017) at %s, %s" % (
                    rec['name'],
                    4 if rec['ratio'] < 80 else 7 if rec['ratio'] < 95 else 3,
                    rec['ratio'],
                    "(no coordinates)" if not 'coords' in rec else "[{0[x]:.0f} : {0[y]:.0f} : {0[z]:.0f}]".format(rec['coords']),
                    "(no close system searched)" if '-f' in tp.args else ("(no close system)" if not 'closest' in rec else "{:.1f}Ly from \002{}\017".format(rec['closest']['real_distance'], rec['closest']['name']))
                    ))
          elif tp.origin_systems is not None:
            self.reply(c, sender_nick, from_channel, "No systems found")
          if tp.reloaded is not None:
            self.reply(c, sender_nick, from_channel, tp.reloaded)
      return proc
    except (IndexError, ValueError, KeyError):
      self.reply(c,sender_nick, from_channel, "Failed - Please pass a valid pid instead of {}".format(params[0]))
    except:
      self.reply(c,sender_nick, from_channel, "Failed - Unhandled Error")
      raise


  def cmd_signal(self, c, params, sender_nick, from_channel):
    self.reply(c,sender_nick, from_channel, "Not implemented yet, sorry")

  def cmd_fact(self, c, params, sender_nick, from_channel):
    if len(params) > 0:
      if params[0] in FACTS.keys():
        if len(params) > 1:
          self.reply(c,sender_nick, from_channel, "{}: {}".format(", ".join(params[1:]), FACTS[params[0]]))
        else:
          self.reply(c,sender_nick, from_channel, FACTS[params[0]])
      else:
        self.reply(c,sender_nick, from_channel, 'No fact called ' + params[0])
    else:
      self.reply(c, sender_nick, None, 'Available FACTS:')
      for k in sorted(FACTS.keys()):
        self.reply(c, sender_nick, None, k + ' -> ' + FACTS[k])

  def handle_PING(self, msg):
    chunk = msg[5:]
    self.send("PONG %s" % chunk)

  def reply(self, c, nick,channel,msg):
    #self.botlogger.debug("reply nick: %s, channel: %s" % (nick, channel))
    to = channel if channel else nick
    if to is None:
      raise RatBotError('No recipient for privmsg')
    c.privmsg(to, msg)

  def send(self, msg):
    now = time.time()
    if self.lastmsgtime != None:
      elapsed = now - self.lastmsgtime
      if elapsed < self.delay:
        time.sleep(self.delay - elapsed)
    self.botlogger.debug(">> " + str(msg.replace("\r\n",'\\r\\n').encode()))
    self.socket.send(msg.encode())
    self.lastmsgtime = time.time()

def main():
  import sys
  if len(sys.argv) < 4:
    print("Usage: testbot <server[:port]> <channel> <nickname> [debug]")
    sys.exit(1)

  s = sys.argv[1].split(":", 1)
  server = s[0]
  if len(s) == 2:
    try:
      port = int(s[1])
    except ValueError:
      print("Error: Erroneous port.")
      sys.exit(1)
  else:
    port = 6667
  channels = sys.argv[2].split(",")
  nickname = sys.argv[3]
  debug = len(sys.argv) >= 5

  botlogger = logging.getLogger('RatBotLogger')
  sysloghandler = logging.handlers.SysLogHandler('/dev/log')
  sysloghandler.setFormatter(logging.Formatter('ratbot %(levelname)s: %(message)s'))
  botlogger.addHandler(sysloghandler)

  if debug:
    botlogger.setLevel(logging.DEBUG)
    stderrhandler = logging.StreamHandler()
    stderrhandler.setFormatter(logging.Formatter('ratbot %(levelname)s: %(message)s'))
    botlogger.addHandler(stderrhandler)
    botlib.systemsearch.DEBUG = True
  else:
    self.botlogger.setLevel(logging.INFO)

  bot = None
  while True:
    try:
      bot = TestBot(channels, nickname, server, port, debug)
      bot.start()
    except KeyboardInterrupt as e:
      raise
    except SystemExit:
      if bot.reset is None or bot.reset:
        botlogger.debug("Continuing")
        continue
      else:
        break
    except:
      botlogger.exception("Thrown")

if __name__ == "__main__":
  main()
