import time
import pprint
import tornado.ioloop
import datetime

from backend import sync_to_front
from rainwave import event
from rainwave import playlist
from rainwave import listeners
from rainwave import request
from rainwave import user
from libs import db
from libs import config
from libs import cache
from libs import log

# import pdb

# Events for each station
current = {}
next = {}
history = {}

class ScheduleIsEmpty(Exception):
	pass

def load():
	for sid in config.station_ids:
		current[sid] = cache.get_station(sid, "sched_current")
		# If our cache is empty, pull from the DB
		if not current[sid]:
			current[sid] = get_event_in_progress(sid)
		if not current[sid] or not current[sid].get_song():
			current[sid] = _create_election(sid)

		next[sid] = cache.get_station(sid, "sched_next")
		if not next[sid]:
			# pdb.set_trace()
			future_time = int(time.time()) + current[sid].length()
			next_elecs = event.Election.load_unused(sid)
			next_event = True
			next[sid] = []
			while len(next[sid]) < 2 and next_event:
				next_event = get_event_at_time(sid, future_time)
				if not next_event:
					if len(next_elecs) > 0:
						next_event = next_elecs.pop(0)
					else:
						next_event = _create_election(sid, future_time)
				if next_event:
					future_time += next_event.length()
					next[sid].append(next_event)

		history[sid] = cache.get_station(sid, "sched_history")
		if not history[sid]:
			history[sid] = []
			# Only loads elections but this should be good enough for history 99% of the time.
			for elec_id in db.c.fetch_list("SELECT elec_id FROM r4_elections WHERE elec_start_actual < %s ORDER BY elec_start_actual DESC LIMIT 5", (current[sid].start_actual,)):
				history[sid].insert(0, event.Election.load_by_id(elec_id))

def get_event_in_progress(sid):
	in_progress = db.c.fetch_row("SELECT sched_id, sched_type FROM r4_schedule WHERE sid = %s AND sched_in_progress = TRUE ORDER BY sched_start DESC LIMIT 1", (sid,))
	if in_progress:
		return event.load_by_id_and_type(in_progress['sched_id'], in_progress['sched_type'])
	else:
		return get_event_at_time(sid, int(time.time()), queued_events=True)

def get_event_at_time(sid, epoch_time, queued_events = False):
	if queued_events:
		in_queue = db.c.fetch_row("SELECT sched_id, sched_type FROM r4_schedule WHERE sid = %s AND sched_start = 0 ORDER BY sched_id LIMIT 1", (sid,))
		if in_queue:
			return event.load_by_id_and_type(in_queue['sched_id'], in_queue['sched_type'])
	at_time = db.c.fetch_row("SELECT sched_id, sched_type FROM r4_schedule WHERE sid = %s AND sched_start <= %s AND sched_end > %s ORDER BY (%s - sched_start) LIMIT 1", (sid, epoch_time + 5, epoch_time, epoch_time))
	if at_time:
		return event.load_by_id_and_type(at_time['sched_id'], at_time['sched_type'])
	elif epoch_time >= time.time():
		return None
	else:
		# We add 5 seconds here in order to make up for any crossfading and buffering times that can screw up the radio timing
		elec_id = db.c.fetch_var("SELECT elec_id FROM r4_elections WHERE r4_elections.sid = %s AND elec_start_actual <= %s ORDER BY elec_start_actual DESC LIMIT 1", (sid, epoch_time - 5))
		if elec_id:
			return event.Election.load_by_id(elec_id)
		else:
			return None

def get_current_file(sid):
	return current[sid].get_filename()

def get_current_event(sid):
	return current[sid]

def advance_station(sid):
	# This has been necessary during development and debugging.
	# Do we want to add an "if config.get("developer_mode")" here so it crashes in production and we hunt down the bug?
	# next[sid] = filter(None, next[sid])

	start_time = time.time()
	playlist.prepare_cooldown_algorithm(sid)
	playlist.clear_updated_albums(sid)
	log.debug("advance", "Playlist prepare time: %.6f" % (time.time() - start_time,))

	start_time = time.time()
	current[sid].finish()
	log.debug("advance", "Current finish time: %.6f" % (time.time() - start_time,))

	start_time = time.time()
	last_song = current[sid].get_song()
	if last_song:
		db.c.update("INSERT INTO r4_song_history (sid, song_id) VALUES (%s, %s)", (sid, last_song.id))
	log.debug("advance", "Last song insertion time: %s" % (time.time() - start_time,))

	start_time = time.time()
	history[sid].insert(0, current[sid])
	while len(history[sid]) > 5:
		history[sid].pop()
	log.debug("advance", "History management: %.6f" % (time.time() - start_time,))

	start_time = time.time()
	integrate_new_events(sid)
	# If we need some emergency elections here
	if len(next[sid]) == 0:
		next[sid].append(_create_election(sid))
	else:
		sort_next(sid)
	log.debug("advance", "Next event management: %.6f" % (time.time() - start_time,))

	start_time = time.time()
	current[sid] = next[sid].pop(0)
	current[sid].start_event()
	log.debug("advance", "Current management: %.6f" % (time.time() - start_time,))

	tornado.ioloop.IOLoop.instance().add_timeout(datetime.timedelta(milliseconds=100), lambda: post_process(sid))

def post_process(sid):
	start_time = time.time()
	set_next_start_times(sid)
	request.update_line(sid)
	playlist.reduce_song_blocks(sid)
	_create_elections(sid)

	_add_listener_count_record(sid)
	cache.update_user_rating_acl(sid, history[sid][0].get_song().id)
	_trim(sid)
	user.trim_listeners(sid)
	user.unlock_listeners(sid)
	playlist.warm_cooled_songs(sid)
	playlist.warm_cooled_albums(sid)

	_update_memcache(sid)
	sync_to_front.sync_frontend_all(sid)
	log.debug("post", "Post-processing prepare time: %.6f" % (time.time() - start_time,))

def refresh_schedule(sid):
	integrate_new_events(sid)
	sort_next(sid)
	_update_memcache(sid)
	sync_to_front.sync_frontend_all_timed(sid)

def _add_listener_count_record(sid):
	lc_guests = db.c.fetch_var("SELECT COUNT(*) FROM r4_listeners WHERE sid = %s AND listener_purge = FALSE AND user_id = 1", (sid,))
	lc_users = db.c.fetch_var("SELECT COUNT(*) FROM r4_listeners WHERE sid = %s AND listener_purge = FALSE AND user_id > 1", (sid,))
	lc_guests_active = db.c.fetch_var("SELECT COUNT(*) FROM r4_listeners WHERE sid = %s AND listener_purge = FALSE AND user_id = 1 AND listener_voted_entry IS NOT NULL", (sid,))
	lc_users_active = db.c.fetch_var("SELECT COUNT(*) FROM r4_listeners WHERE sid = %s AND listener_purge = FALSE AND user_id > 1 AND listener_voted_entry IS NOT NULL", (sid,))
	return db.c.update("INSERT INTO r4_listener_counts (sid, lc_guests, lc_users, lc_guests_active, lc_users_active) VALUES (%s, %s, %s, %s, %s)", (sid, lc_guests, lc_users, lc_guests_active, lc_users_active))

def _get_schedule_stats(sid):
	max_sched_id = 0
	max_elec_id = 0
	num_elections = 0
	for e in next[sid]:
		if e.is_election:
			num_elections += 1
			if e.id > max_elec_id:
				max_elec_id = e.id
		elif not e.is_election and e.id > max_sched_id:
			max_sched_id = e.id
	return (max_sched_id, max_elec_id, num_elections)

def integrate_new_events(sid):
	max_sched_id, max_elec_id, num_elections = _get_schedule_stats(sid)

	# Will remove events that no longer have a schedule ID with them
	new_next = []
	for e in next[sid]:
		if e.is_election and db.c.fetch_var("SELECT elec_id FROM r4_elections WHERE elec_id = %s" % e.id):
			new_next.append(e)
		elif db.c.fetch_var("SELECT sched_id FROM r4_schedule WHERE sched_id = %s" % e.id):
			new_next.append(e)
	next[sid] = new_next

	# This is the line of code that loads in any upcoming events
	unused_sched_id = db.c.fetch_list("SELECT sched_id FROM r4_schedule WHERE sid = %s AND sched_id > %s AND sched_used = FALSE AND sched_start <= %s ORDER BY sched_start", (sid, max_sched_id, int(time.time()) + 86400))
	for sched_id in unused_sched_id:
		e = event.load_by_id(sched_id)
		if not e:
			log.warn("schedule", "Unused event ID %s was None." % sched_id)
		else:
			next[sid].append(e)

	# Step 4: Insert "priority elections" ahead of anything else
	priority_elec_ids = db.c.fetch_list("SELECT elec_id FROM r4_elections WHERE sid = %s AND elec_id > %s AND elec_priority = TRUE ORDER BY elec_id DESC", (sid, max_elec_id))
	for elec_id in priority_elec_ids:
		e = playlist.Election.load_by_id(elec_id)
		# The client, through the API, sets start times, so we don't have to worry about where in the array it goes.  The sorter will take care of it.
		next[sid].append(e)
		if e.id > max_elec_id:
			max_elec_id = e.id
		num_elections += 1
		e.set_priority(False)

	return (max_sched_id, max_elec_id, num_elections)

def sort_next(sid):
	global next
	next[sid] = filter(None, next[sid])
	next[sid] = sorted(next[sid], key=lambda event: event.start)

def set_next_start_times(sid):
	"""
	Calibrate the start points of each scheduled event until we reach the number of future elections planned.
	This has not been fully tested and will likely require some tweaking to be done in the future.
	"""
	if len(next[sid]) == 0 or config.get_station(sid, "num_planned_elections") == 0:
		return
	i = 1
	next[sid][0].start_predicted = current[sid].start_actual + current[sid].length()
	while (i < len(next[sid])):
		if not next[sid][i].start_predicted or ((next[sid][i].start_predicted - next[sid][i - 1].start_predicted) <= 30) or next[sid][i].is_election:
			next[sid][i].start_predicted = next[sid][i - 1].start_predicted + next[sid][i - 1].length()
			i += 1
		else:
			break

def _create_elections(sid):
	# Step, er, 0: Update the request cache first, so elections have the most recent data to work with
	# (the entire requests module depends on its caches)
	request.update_cache(sid)

	max_sched_id, max_elec_id, num_elections = _get_schedule_stats(sid)
	log.debug("create_elec", "Max sched ID: %s // Max elec ID: %s // Num elections already existing: %s // Size of next: %s" % (max_sched_id, max_elec_id, num_elections, len(next[sid])))

	# Step 2: Load up any elections that have been added while we've been idle and append them to the list
	unused_elec_id = db.c.fetch_list("SELECT elec_id FROM r4_elections WHERE sid = %s AND elec_id > %s AND elec_used = FALSE AND elec_priority = FALSE ORDER BY elec_id", (sid, max_elec_id))
	unused_elecs = []
	for elec_id in unused_elec_id:
		unused_elecs.append(event.Election.load_by_id(elec_id))

	# Step 3: Insert elections where there's time and adjust predicted start times as necessary, if num_elections < 2 then create them where necessary
	i = 1
	running_time = current[sid].start_actual + current[sid].length()
	if len(next[sid]) > 0:
		next[sid][0].start = running_time
	while i < len(next[sid]):
		next_start = next[sid][i].start
		gap = next_start - running_time
		next_elec_i = None
		next_elec_length = playlist.get_average_song_length(sid)
		j = i
		while j < len(next[sid]):
			if next[sid][j].is_election:
				next_elec = j
				next_elec_length = next[sid][j].length()
				break
		if not next_elec_i and len(unused_elecs) > 0:
			next_elec_length = unused_elecs[0].length()

		# TODO: This algorithm DEFINITELY needs code/concept review
		# There are potential holes - it is not as comprehensive a scheduler as the previous
		# Rainwave scheduler, however it is vastly simplified.
		# One drawback is that you cannot schedule elections themselves to run at certain times.

		create_elecs = False
		# If the event we're looking at collides with the previous event, adjust this event to start later
		if gap <= 0:
			next[sid][i].start = running_time
			running_time += next[sid][i].length()
		# If we have no elections current in the next list and there's enough time to fit a song, stuff an election in
		# (this automatically takes into account unused elections, based on next_elec_length definition above)
		elif not next_elec_i and gap <= (next_elec_length * 1.4):
			next_elec = None
			# If we have an existing unused election, we can use that (as next_elec_length is already based on the first unused elec, this can happen)
			if len(unused_elecs) > 0:
				next_elec = unused_elecs.pop(0)
			# If not, create a new election timed to the gap (next_elec_length will be the average song length*1.4, so this will happen frequently)
			else:
				next_elec = _create_election(sid, running_time, gap)
			num_elections += 1
			next_elec.start = running_time
			running_time += next_elec.length()
			next[sid].insert(i, next_elec)
		# If it's more accurate to squeeze a created election in here than adjust the next event, move the event
		# *OR* the next event is too far out and we have elections in hand
		elif next_elec_i and ((gap <= (next_elec_length / 2)) or (gap > (next_elec_length * 1.5))):
			next_elec = next[sid].pop(next_elec_i)
			next_elec.start = running_time
			running_time += next_elec.length()
			next[sid].insert(i, next_elec)
		# The next event is better off aligned
		else:
			next[sid][i].start = running_time
			running_time += next[sid][i].length()
		i += 1

	needed_elecs = config.get_station(sid, "num_planned_elections") - num_elections
	log.debug("create_elec", "Before: Needed elecs: %s // Unused elecs: %s // Current num elections: %s // Next size: %s" % (needed_elecs, len(unused_elecs), num_elections, len(next[sid])))
	# Step 5: If we're at less than X elections available, create them (or use unused ones) and append them
	# No timing is required here, since we're simply outright appending to the end
	# (any elections appearing before a scheduled item would be handled by the block above)
	failures = 0
	while needed_elecs > 0 and failures <= 2:
		next_elec = None
		if len(unused_elecs) > 0:
			next_elec = unused_elecs.pop(0)
		else:
			next_elec = _create_election(sid, running_time)
		next_elec_length = next_elec.length()
		if next_elec_length > 0:
			next_elec.start = running_time
			running_time += next_elec.length()
			next[sid].append(next_elec)
			num_elections += 1
			needed_elecs -= 1
		else:
			log.error("create_elec", "Election ID %s was faulty - zero length.  Deleting.")
			next_elec.delete()
			failures += 1
	if failures >= 2:
		log.error("create_elec", "Total failure when creating elections.")
	log.debug("create_elec", "After: Unused elecs: %s // Current num elections: %s // Next size: %s" % (len(unused_elecs), num_elections, len(next[sid])))

def _create_election(sid, start_time = None, target_length = None):
	log.debug("create_elec", "Creating election, start time %s target length %s." % (start_time, target_length))
	db.c.update("START TRANSACTION")
	try:
		# Check to see if there are any events during this time
		elec_scheduler = None
		if start_time:
			elec_scheduler = get_event_at_time(sid, start_time)
		# If there are, and it makes elections (e.g. PVP Hours), get it from there
		if elec_scheduler and elec_scheduler.produces_elections:
			elec_scheduler.create_election(sid)
		else:
			elec = event.Election.create(sid)
		elec.fill(target_length)
		db.c.update("COMMIT")
		return elec
	except:
		db.c.update("ROLLBACK")
		raise

def _trim(sid):
	# Deletes any events in the schedule and elections tables that are old, according to the config
	current_time = int(time.time())
	db.c.update("DELETE FROM r4_schedule WHERE sched_start_actual <= %s", (current_time - config.get("trim_event_age"),))
	db.c.update("DELETE FROM r4_elections WHERE elec_start_actual <= %s", (current_time - config.get("trim_election_age"),))
	max_history_id = db.c.fetch_var("SELECT MAX(songhist_id) FROM r4_song_history")
	db.c.update("DELETE FROM r4_song_history WHERE songhist_id <= %s", (max_history_id - config.get("trim_history_length"),))

def _update_memcache(sid):
	cache.set_station(sid, "sched_current", current[sid], True)
	cache.set_station(sid, "sched_next", next[sid], True)
	cache.set_station(sid, "sched_history", history[sid], True)
	cache.set_station(sid, "sched_current_dict", current[sid].to_dict(), True)
	next_dict_list = []
	for event in next[sid]:
		next_dict_list.append(event.to_dict())
	cache.set_station(sid, "sched_next_dict", next_dict_list, True)
	history_dict_list = []
	for event in history[sid]:
		history_dict_list.append(event.to_dict())
	cache.set_station(sid, "sched_history_dict", history_dict_list, True)
	cache.prime_rating_cache_for_events([ current[sid] ] + next[sid] + history[sid])
	cache.set_station(sid, "listeners_current", listeners.get_listeners_dict(sid), True)
	cache.set_station(sid, "album_diff", playlist.get_updated_albums_dict(sid), True)
	playlist.clear_updated_albums(sid)
	cache.set_station(sid, "all_albums", playlist.get_all_albums_list(sid), True)
	cache.set_station(sid, "all_artists", playlist.get_all_artists_list(sid), True)
