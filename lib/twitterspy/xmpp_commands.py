# coding=utf-8
import re
import sys
import time
import types
import base64
import datetime
import urlparse
import sre_constants

from twisted.python import log
from twisted.words.xish import domish
from twisted.words.protocols.jabber.jid import JID
from twisted.web import client
from twisted.internet import reactor, threads
from wokkel import ping
from sqlalchemy.orm import exc

import models

import config
import twitter
import scheduling
import protocol
import moodiness

all_commands={}

def arg_required(validator=lambda n: n):
    def f(orig):
        def every(self, user, prot, args, session):
            if validator(args):
                orig(self, user, prot, args, session)
            else:
                prot.send_plain(user.jid, "Arguments required for %s:\n%s"
                    % (self.name, self.extended_help))
        return every
    return f

def login_required(orig):
    def every(self, user, prot, args, session):
        if user.has_credentials:
            orig(self, user, prot, args, session)
        else:
            prot.send_plain(user.jid, "You must twlogin before calling %s"
                % self.name)
    return every

def admin_required(orig):
    def every(self, user, prot, args, session):
        if user.is_admin:
            orig(self, user, prot, args, session)
        else:
            prot.send_plain(user.jid, "You're not an admin.")
    return every

class BaseCommand(object):
    """Base class for command processors."""

    def __get_extended_help(self):
        if self.__extended_help:
            return self.__extended_help
        else:
            return self.help

    def __set_extended_help(self, v):
        self.__extended_help=v

    extended_help=property(__get_extended_help, __set_extended_help)

    def __init__(self, name, help=None, extended_help=None, aliases=[]):
        self.name=name
        self.help=help
        self.aliases=aliases
        self.extended_help=extended_help

    def __call__(self, user, prot, args, session):
        raise NotImplementedError()

    def is_a_url(self, u):
        try:
            parsed = urlparse.urlparse(str(u))
            return parsed.scheme in ['http', 'https'] and parsed.netloc
        except:
            return False

class BaseStatusCommand(BaseCommand):

    def get_user_status(self, user):
        rv=[]
        rv.append("Jid:  %s" % user.jid)
        rv.append("Twitterspy status:  %s"
            % {True: 'Active', False: 'Inactive'}[user.active])
        resources = scheduling.resources(user.jid)
        if resources:
            rv.append("I see you logged in with the following resources:")
            for r in resources:
                rv.append(u"  • %s" % r)
        else:
            rv.append("I don't see you logged in with any resource I'd send "
                "a message to.  Perhaps you're dnd, xa, or negative priority.")
        rv.append("You are currently tracking %d topics." % len(user.tracks))
        if user.has_credentials:
            rv.append("You're logged in to twitter as %s" % (user.username))
        if user.friend_timeline_id is not None:
            rv.append("Friend tracking is enabled.")
        return "\n".join(rv)

class StatusCommand(BaseStatusCommand):

    def __init__(self):
        super(StatusCommand, self).__init__('status', 'Check your status.')

    def __call__(self, user, prot, args, session):
        prot.send_plain(user.jid, self.get_user_status(user))

class HelpCommand(BaseCommand):

    def __init__(self):
        super(HelpCommand, self).__init__('help', 'You need help.')

    def __call__(self, user, prot, args, session):
        rv=[]
        if args and args.strip():
            c=all_commands.get(args.strip().lower(), None)
            if c:
                rv.append("Help for %s:\n" % c.name)
                rv.append(c.extended_help)
                if c.aliases:
                    rv.append("\nAliases:\n * ")
                    "\n * ".join(c.aliases)
            else:
                rv.append("Unknown command %s." % args)
        else:
            for k in sorted(all_commands.keys()):
                if (not k.startswith('adm_')) or user.is_admin:
                    rv.append('%s\t%s' % (k, all_commands[k].help))
        rv.append("\nFor more help, see http://dustin.github.com/twitterspy/")
        prot.send_plain(user.jid, "\n".join(rv))

class OnCommand(BaseCommand):
    def __init__(self):
        super(OnCommand, self).__init__('on', 'Enable tracks.')

    def __call__(self, user, prot, args, session):
        user.active=True
        scheduling.enable_user(user.jid)
        prot.send_plain(user.jid, "Enabled tracks.")

class OffCommand(BaseCommand):
    def __init__(self):
        super(OffCommand, self).__init__('off', 'Disable tracks.')

    def __call__(self, user, prot, args, session):
        user.active=False
        scheduling.disable_user(user.jid)
        prot.send_plain(user.jid, "Disabled tracks.")

class SearchCommand(BaseCommand):

    def __init__(self):
        super(SearchCommand, self).__init__('search',
            'Perform a search query (but do not track).')

    def _success(self, e, jid, prot, query, rv):
        log.msg("%d results found for %s" % (len(rv.results), query))
        plain = []
        html = []
        for eid, p, h in rv.results:
            plain.append(p)
            html.append(h)
        prot.send_html(jid, str(len(rv.results))
                           + " results for " + query
                           + "\n\n" + "\n\n".join(plain),
                       str(len(rv.results)) + " results for "
                           + query + "<br/>\n<br/>\n"
                           + "<br/>\n<br/>\n".join(html))

    def _error(self, e, jid, prot):
        mood, good, lrr, percentage = moodiness.moodiness.current_mood()
        rv = [":( Problem performing search."]
        if percentage > 0.5:
            rv.append("%.1f%% of recent searches have worked (%d out of %d)"
                      % ((percentage * 100.0), good, lrr))
        elif good == 0:
            rv.append("This is not surprising, "
                      "no recent searches worked for me, either (%d out of %d)"
                      % (good, lrr))
        else:
            rv.append("This is not surprising -- only %.1f%% work now anyway (%d out of %d)"
                      % ((percentage * 100.0), good, lrr))
        prot.send_plain(jid, "\n".join(rv))
        return e

    def _do_search(self, query, jid, prot):
        rv = scheduling.SearchCollector()
        scheduling.getTwitterAPI().search(query, rv.gotResult, {'rpp': '3'}
            ).addCallback(moodiness.moodiness.markSuccess
            ).addErrback(moodiness.moodiness.markFailure
            ).addCallback(self._success, jid, prot, query, rv
            ).addErrback(self._error, jid, prot
            ).addErrback(log.err)

    @arg_required()
    def __call__(self, user, prot, args, session):
        scheduling.search_semaphore.run(self._do_search, args, user.jid, prot)

class TWLoginCommand(BaseCommand):

    def __init__(self):
        super(TWLoginCommand, self).__init__('twlogin',
            'Set your twitter username and password (use at your own risk)')

    @arg_required()
    def __call__(self, user, prot, args, session):
        args = args.replace(">", "").replace("<", "")
        username, password=args.split(' ', 1)
        jid = user.jid
        scheduling.getTwitterAPI(username, password).verify_credentials().addCallback(
            self.__credsVerified, prot, jid, username, password).addErrback(
            self.__credsRefused, prot, jid)

    def __credsRefused(self, e, prot, jid):
        log.msg("Failed to verify creds for %s: %s" % (jid, e))
        prot.send_plain(jid,
            ":( Your credentials were refused. "
                "Please try again: twlogin username password")

    @models.wants_session
    def __credsVerified(self, x, prot, jid, username, password, session):
        user = models.User.by_jid(jid, session)
        user.username = username
        user.password = base64.encodestring(password)
        try:
            session.commit()
            prot.send_plain(user.jid, "Added credentials for %s"
                % user.username)
            scheduling.users.set_creds(jid, username, password)
        except:
            log.err()
            prot.send_plain(user.jid, "Error setting credentials for %s. "
                "Please try again." % user.username)

class TWLogoutCommand(BaseCommand):

    def __init__(self):
        super(TWLogoutCommand, self).__init__('twlogout',
            "Discard your twitter credentials.")

    def __call__(self, user, prot, args, session):
        user.username = None
        user.password = None
        prot.send_plain(user.jid, "You have been logged out.")
        scheduling.users.set_creds(user.jid, None, None)

class TrackCommand(BaseCommand):

    def __init__(self):
        super(TrackCommand, self).__init__('track', "Start tracking a topic.")

    @arg_required()
    def __call__(self, user, prot, args, session):
        user.track(args, session)
        if user.active:
            scheduling.queries.add(user.jid, args, 0)
            rv = "Tracking %s" % args
        else:
            rv = "Will track %s as soon as you activate again." % args
        prot.send_plain(user.jid, rv)

class UnTrackCommand(BaseCommand):

    def __init__(self):
        super(UnTrackCommand, self).__init__('untrack',
            "Stop tracking a topic.")

    @arg_required()
    def __call__(self, user, prot, args, session):
        if user.untrack(args, session):
            scheduling.queries.untracked(user.jid, args)
            prot.send_plain(user.jid, "Stopped tracking %s" % args)
        else:
            prot.send_plain(user.jid,
                "Didn't tracking %s (sure you were tracking it?)" % args)

class TracksCommand(BaseCommand):

    def __init__(self):
        super(TracksCommand, self).__init__('tracks',
            "List the topics you're tracking.", aliases=['tracking'])

    def __call__(self, user, prot, args, session):
        rv = ["Currently tracking:\n"]
        rv.extend(sorted([t.query for t in user.tracks]))
        prot.send_plain(user.jid, "\n".join(rv))

class PostCommand(BaseCommand):

    def __init__(self):
        super(PostCommand, self).__init__('post',
            "Post a message to twitter.")

    def _posted(self, id, jid, username, prot):
        url = "http://twitter.com/%s/statuses/%s" % (username, id)
        prot.send_plain(jid, ":) Your message has been posted: %s" % url)

    def _failed(self, e, jid, prot):
        log.msg("Error updating for %s:  %s" % (jid, str(e)))
        prot.send_plain(jid, ":( Failed to post your message. "
            "Your password may be wrong, or twitter may be broken.")

    @arg_required()
    def __call__(self, user, prot, args, session):
        if user.has_credentials:
            jid = user.jid
            scheduling.getTwitterAPI(user.username, user.decoded_password).update(
                args, 'twitterspy'
                ).addCallback(self._posted, jid, user.username, prot
                ).addErrback(self._failed, jid, prot)
        else:
            prot.send_plain(user.jid, "You must twlogin before you can post.")

class FollowCommand(BaseCommand):

    def __init__(self):
        super(FollowCommand, self).__init__('follow',
            "Begin following a user.")

    def _following(self, e, jid, prot, user):
        prot.send_plain(jid, ":) Now following %s" % user)

    def _failed(self, e, jid, prot, user):
        log.msg("Failed a follow request %s" % repr(e))
        prot.send_plain(jid, ":( Failed to follow %s" % user)

    @arg_required()
    @login_required
    def __call__(self, user, prot, args, session):
        scheduling.getTwitterAPI(user.username, user.decoded_password).follow(
            str(args)).addCallback(self._following, user.jid, prot, args
            ).addErrback(self._failed, user.jid, prot, args)

class LeaveUser(BaseCommand):

    def __init__(self):
        super(LeaveUser, self).__init__('leave',
            "Stop following a user.", aliases=['unfollow'])

    def _left(self, e, jid, prot, user):
        prot.send_plain(jid, ":) No longer following %s" % user)

    def _failed(self, e, jid, prot, user):
        log.msg("Failed an unfollow request: %s", repr(e))
        prot.send_plain(jid, ":( Failed to stop following %s" % user)

    @arg_required()
    @login_required
    def __call__(self, user, prot, args, session):
        scheduling.getTwitterAPI(user.username, user.decoded_password).leave(
            str(args)).addCallback(self._left, user.jid, prot, args
            ).addErrback(self._failed, user.jid, prot, args)

class BlockCommand(BaseCommand):

    def __init__(self):
        super(BlockCommand, self).__init__('block',
                                            "Block a user.")

    def _blocked(self, e, jid, prot, user):
        prot.send_plain(jid, ":) Now blocking %s" % user)

    def _failed(self, e, jid, prot, user):
        log.msg("Failed a block request %s" % repr(e))
        prot.send_plain(jid, ":( Failed to block %s" % user)

    @arg_required()
    @login_required
    def __call__(self, user, prot, args, session):
        scheduling.getTwitterAPI(user.username, user.decoded_password).block(
            str(args)).addCallback(self._blocked, user.jid, prot, args
            ).addErrback(self._failed, user.jid, prot, args)

class UnblockCommand(BaseCommand):

    def __init__(self):
        super(UnblockCommand, self).__init__('unblock',
                                        "Unblock a user.")

    def _left(self, e, jid, prot, user):
        prot.send_plain(jid, ":) No longer blocking %s" % user)

    def _failed(self, e, jid, prot, user):
        log.msg("Failed an unblock request: %s", repr(e))
        prot.send_plain(jid, ":( Failed to unblock %s" % user)

    @arg_required()
    @login_required
    def __call__(self, user, prot, args, session):
        scheduling.getTwitterAPI(user.username, user.decoded_password).unblock(
            str(args)).addCallback(self._left, user.jid, prot, args
            ).addErrback(self._failed, user.jid, prot, args)

def must_be_on_or_off(args):
    return args and args.lower() in ["on", "off"]

class AutopostCommand(BaseCommand):

    def __init__(self):
        super(AutopostCommand, self).__init__('autopost',
            "Enable or disable autopost.")

    @arg_required(must_be_on_or_off)
    def __call__(self, user, prot, args, session):
        user.auto_post = (args.lower() == "on")
        prot.send_plain(user.jid, "Autoposting is now %s." % (args.lower()))

class WatchFriendsCommand(BaseCommand):

    def __init__(self):
        super(WatchFriendsCommand, self).__init__('watch_friends',
            "Enable or disable watching friends.", aliases=['watchfriends'])

    def _gotFriendStatus(self, jid, prot):
        @models.wants_session
        def f(entry, session):
            user = models.User.by_jid(jid, session)
            user.friend_timeline_id = entry.id
            try:
                session.commit()
                prot.send_plain(jid, ":) Starting to watch friends.")
            except:
                log.err()
                prot.send_plain(jid,
                    ":( Error watching friends, please try again.")
        return f

    @arg_required(must_be_on_or_off)
    @login_required
    def __call__(self, user, prot, args, session):
        args = args.lower()
        if args == 'on':
            scheduling.getTwitterAPI(user.username, user.decoded_password).friends(
                self._gotFriendStatus(user.jid, prot), params={'count': '1'})
        elif args == 'off':
            user.friend_timeline_id = None
            prot.send_plain(user.jid, ":) No longer watching your friends.")
        else:
            prot.send_plain(user.jid, "Watch must be 'on' or 'off'.")

class WhoisCommand(BaseCommand):

    def __init__(self):
        super(WhoisCommand, self).__init__('whois',
            'Find out who a user is.')

    def _fail(self, e, prot, jid, u):
        prot.send_plain(user.jid, "Couldn't get info for %s" % u)

    def _gotUser(self, u, prot, jid):
        html="""Whois <a
  href="http://twitter.com/%(screen_name)s">%(screen_name)s</a><br/><br/>
Name:  %(name)s<br/>
Home:  %(url)s<br/>
Where: %(location)s<br/>
Friends: <a
  href="http://twitter.com/%(screen_name)s/friends">%(friends_count)s</a><br/>
Followers: <a
  href="http://twitter.com/%(screen_name)s/followers">%(followers_count)s</a><br/>
Recently:<br/>
        %(status_text)s
"""
        params = dict(u.__dict__)
        params['status_text'] = u.status.text
        prot.send_html(jid, "(no plain text yet)", html % params)

    @arg_required()
    @login_required
    def __call__(self, user, prot, args, session):
        scheduling.getTwitterAPI(user.username, user.decoded_password).show_user(
            str(args)).addErrback(self._fail, prot, user.jid, args
            ).addCallback(self._gotUser, prot, user.jid)

class Top10Command(BaseCommand):

    def __init__(self):
        super(Top10Command, self).__init__('top10',
            'Get the top10 most common tracks.')

    def __call__(self, user, prot, args, session):
        query="""
select t.query, count(*) as watchers
  from tracks t join user_tracks ut on (t.id = ut.track_id)
  group by t.query
  order by watchers desc, query
  limit 10
"""
        rv=["Top 10 most tracked topics:"]
        rv.append("")
        for row in models._engine.execute(query).fetchall():
            rv.append("%s (%d watchers)" % (row[0], row[1]))
        prot.send_plain(user.jid, "\n".join(rv))

class MoodCommand(BaseCommand):

    def __init__(self):
        super(MoodCommand, self).__init__('mood',
            "Ask about twitterspy's mood.")

    def __call__(self, user, prot, args, session):
        mood, good, total, percentage = moodiness.moodiness.current_mood()
        if mood:
            rv=["My current mood is %s" % mood]
            rv.append("I've processed %d out of the last %d searches."
                      % (good, total))
        else:
            rv=["I just woke up.  Ask me in a minute or two."]
        rv.append("I currently have %d API requests available, "
                  "and have run out %d times."
                  % (scheduling.available_requests, scheduling.empty_resets))
        prot.send_plain(user.jid, "\n".join(rv))

class AdminSubscribeCommand(BaseCommand):

    def __init__(self):
        super(AdminSubscribeCommand, self).__init__('adm_subscribe',
            'Subscribe a user.')

    @admin_required
    @arg_required()
    def __call__(self, user, prot, args, session):
        prot.send_plain(user.jid, "Subscribing " + args)
        protocol.presence_conn.subscribe(JID(args))

class AdminUserStatusCommand(BaseStatusCommand):

    def __init__(self):
        super(AdminUserStatusCommand, self).__init__('adm_status',
            "Check a user's status.")

    @admin_required
    @arg_required()
    def __call__(self, user, prot, args, session):
        try:
            u=models.User.by_jid(args, session)
            prot.send_plain(user.jid, self.get_user_status(u))
        except Exception, e:
            prot.send_plain(user.jid, "Failed to load user: " + str(e))

class AdminPingCommand(BaseCommand):

    def __init__(self):
        super(AdminPingCommand, self).__init__('adm_ping',
            'Ping a JID')

    def ping(self, prot, fromjid, tojid):
        p = ping.Ping(prot.xmlstream, config.SCREEN_NAME, tojid)
        d = p.send()
        log.msg("Sending ping %s" % p.toXml())
        def _gotPing(x):
            duration = time.time() - p.start_time
            log.msg("pong %s" % tojid)
            prot.send_plain(fromjid, ":) Pong (%s) - %fs" % (tojid, duration))
        def _gotError(x):
            duration = time.time() - p.start_time
            log.msg("Got an error pinging %s: %s" % (tojid, x))
            prot.send_plain(fromjid, ":( Error pinging %s (%fs): %s"
                            % (tojid, duration, x.value.condition))
        d.addCallback(_gotPing)
        d.addErrback(_gotError)
        return d

    @admin_required
    @arg_required()
    def __call__(self, user, prot, args, session):
        # For bare jids, we'll send what was requested,
        # but also look up the user and send it to any active resources
        self.ping(prot, user.jid, args)
        j = JID(args)
        if j.user and not j.resource:
            for rsrc in scheduling.resources(args):
                j.resource=rsrc
                self.ping(prot, user.jid, j.full())

class AdminBroadcastCommand(BaseCommand):

    def __init__(self):
        super(AdminBroadcastCommand, self).__init__('adm_broadcast',
                                                    'Broadcast a message.')

    @models.wants_session
    def _load_users(self, session):
        return [user.jid for user in session.query(models.User).filter(
                models.User.status.in_(['online', 'away', 'dnd', 'xa']))]

    def _do_broadcast(self, users, prot, jid, msg):
        log.msg("Administrative broadcast from %s" % jid)
        for j in users:
            log.msg("Sending message to %s" % j)
            prot.send_plain(j, msg)
        prot.send_plain(jid, "Sent message to %d users" % len(users))

    @admin_required
    @arg_required()
    def __call__(self, user, prot, args, session):
        threads.deferToThread(self._load_users).addCallback(
            self._do_broadcast, prot, user.jid, args)

for __t in (t for t in globals().values() if isinstance(type, type(t))):
    if BaseCommand in __t.__mro__:
        try:
            i = __t()
            all_commands[i.name] = i
        except TypeError, e:
            # Ignore abstract bases
            log.msg("Error loading %s: %s" % (__t.__name__, str(e)))
            pass
