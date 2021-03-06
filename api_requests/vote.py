import time

from api import fieldtypes
from api.web import APIHandler
from api.exceptions import APIException
from api.server import test_get
from api.server import test_post
from api.server import handle_api_url
from api.exceptions import APIException

from libs import cache
from libs import log
from libs import db
from rainwave import playlist

def append_success_to_request(request, elec_id, entry_id):
	request.append("vote_result", { "success": True, "tl_key": "vote_submitted", "text": "Vote submitted.",
									"elec_id": elec_id,	"entry_id": entry_id, "text": "Vote submitted."})

@handle_api_url("vote")
class SubmitVote(APIHandler):
	return_name = "vote_result"
	tunein_required = True
	description = "Vote for a candidate in an election.  Cannot cancel/delete a vote.  If user has already voted, the vote will be changed to the submitted song."
	fields = { "entry_id": (fieldtypes.integer, True) }

	def post(self):
		events = cache.get_station(self.sid, "sched_next")
		lock_count = 0
		elec_id = None
		for event in events:
			lock_count += 1
			if event.is_election and event.has_entry_id(self.get_argument("entry_id")):
				elec_id = event.id
				self.vote(self.get_argument("entry_id"), event, lock_count)
				break
			if not self.user.data['radio_perks']:
				break
		append_success_to_request(self, elec_id, self.get_argument("entry_id"))

	def vote(self, entry_id, event, lock_count):
		# Subtract a previous vote from the song's total if there was one
		already_voted = False
		if self.user.is_anonymous():
			log.debug("vote", "Anon already voted: %s" % (self.user.id, self.user.data['listener_voted_entry']))
			if self.user.data['listener_voted_entry'] and self.user.data['listener_voted_entry'] == entry_id:
				raise APIException("already_voted_for_song")
			if self.user.data['listener_voted_entry']:
				already_voted = True
				if not event.add_vote_to_entry(entry_id, -1):
					log.warn("vote", "Could not subtract vote from entry: listener ID %s voting for entry ID %s." % (self.user.data['listener_id'], entry_id))
					raise APIException("internal_error")
		else:
			already_voted = db.c.fetch_row("SELECT entry_id, vote_id, song_id FROM r4_vote_history WHERE user_id = %s AND elec_id = %s", (self.user.id, event.id))
			log.debug("vote", "Already voted: %s" % repr(already_voted))
			if already_voted and already_voted['entry_id'] == entry_id:
				raise APIException("already_voted_for_song")
			elif already_voted:
				log.debug("vote", "Subtracting vote from %s" % already_voted['entry_id'])
				if not event.add_vote_to_entry(already_voted['entry_id'], -1):
					log.warn("vote", "Could not subtract vote from entry: listener ID %s voting for entry ID %s." % (self.user.data['listener_id'], entry_id))
					raise APIException("internal_error")

		# If this is a new vote, we need to check to make sure the listener is not locked.
		if not already_voted and self.user.data['listener_lock'] and self.user.data['listener_lock_sid'] != self.sid:
			raise APIException("user_locked", "User locked to %s for %s more songs." % (config.station_id_friendly[self.user.data['listener_lock_sid']], self.user.data['listener_lock_counter']))
		# Issue the listener lock (will extend a lock if necessary)
		if not self.user.lock_to_sid(self.sid, lock_count):
			log.warn("vote", "Could not lock user: listener ID %s voting for entry ID %s, tried to lock for %s events." % (self.user.data['listener_id'], entry_id, lock_count))
			raise APIException("internal_error", "Internal server error.  User is now locked to station ID %s." % self.sid)

		# Make sure the vote is tracked
		track_success = False
		if self.user.is_anonymous():
			if not db.c.update("UPDATE r4_listeners SET listener_voted_entry = %s WHERE listener_id = %s", (entry_id, self.user.data['listener_id'])):
				log.warn("vote", "Could not set voted_entry: listener ID %s voting for entry ID %s." % (self.user.data['listener_id'], entry_id))
				raise APIException("internal_error")
			self.user.update({ "listener_voted_entry": entry_id })
			track_success = True
		else:
			if already_voted:
				db.c.update("UPDATE r4_vote_history SET song_id = %s, entry_id = %s WHERE vote_id = %s", (event.get_entry(entry_id).id, entry_id, already_voted['vote_id']))
			else:
				time_window = int(time.time()) - 1209600
				vote_count = db.c.fetch_var("SELECT COUNT(vote_id) FROM r4_vote_history WHERE vote_time > %s AND user_id = %s", (time_window, self.user.id))
				db.c.execute("SELECT user_id, COUNT(song_id) AS c FROM r4_vote_history WHERE vote_time > %s GROUP BY user_id HAVING COUNT(song_id) > %s", (time_window, vote_count))
				rank = db.c.rowcount + 1
				db.c.update(
					"INSERT INTO r4_vote_history (elec_id, entry_id, user_id, song_id, vote_at_rank, vote_at_count) "
					"VALUES (%s, %s, %s, %s, %s, %s)",
					(event.id, entry_id, self.user.id, event.get_entry(entry_id).id, rank, vote_count))
			track_success = True

			user_vote_cache = cache.get_user(self.user, "vote_history")
			if not user_vote_cache:
				user_vote_cache = []
			while len(user_vote_cache) > 5:
				user_vote_cache.pop(0)
			user_vote_cache.append((event.id, entry_id))
			cache.set_user(self.user, "vote_history", user_vote_cache)

		# Register vote
		if not event.add_vote_to_entry(entry_id):
			log.warn("vote", "Could not add vote to entry: listener ID %s voting for entry ID %s." % (self.user.data['listener_id'], entry_id))
			raise APIException("internal_error")

