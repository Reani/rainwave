import psycopg2
from psycopg2 import extras
import sqlite3
import re

from libs import config
from libs import log

c = None
connection = None
c_old = None

# NOTE TO ALL:
#
# SQLite is UNUSABLE for production.  It is ONLY meant for testing
# and offers zero data consistency - important values (sequences, etc)
# are reset between startups.

class PostgresCursor(psycopg2.extras.RealDictCursor):
	allows_join_on_update = True
	is_postgres = True

	def fetch_var(self, query, params = None):
		self.execute(query, params)
		if self.rowcount == 0:
			return None
		r = self.fetchone()
		# I realize this is not the most efficient way, but one of the primary
		# uses of the DB is to pipe output directly to JSON as an object/dict.
		# Thus why this class inherits RealDictCursor.
		# Either I can use this small inefficiency here or for fetching rows
		# and entire queries I can convert each row into a dict manually.
		# This has a smaller penalty.
		return r[r.keys()[0]]

	def fetch_row(self, query, params = None):
		self.execute(query, params)
		#if self.rowcount == 0:
		#	return None
		return self.fetchone()

	def fetch_all(self, query, params = None):
		self.execute(query, params)
		if self.rowcount == 0:
			return []
		return self.fetchall()

	def fetch_list(self, query, params = None):
		self.execute(query, params)
		if self.rowcount == 0:
			return []
		arr = []
		row = self.fetchone()
		col = row.keys()[0]
		arr.append(row[col])
		for row in self.fetchall():
			arr.append(row[col])
		return arr

	def update(self, query, params = None):
		self.execute(query, params)
		return self.rowcount

	def get_next_id(self, table, column):
		return self.fetch_var("SELECT nextval('" + table + "_" + column + "_seq'::regclass)")

	def create_delete_fk(self, linking_table, foreign_table, key, create_idx = True):
		if create_idx:
			self.create_idx(linking_table, key)
		self.execute("ALTER TABLE %s ADD CONSTRAINT %s_%s_fk FOREIGN KEY (%s) REFERENCES %s (%s) ON DELETE CASCADE" % (linking_table, linking_table, key, key, foreign_table, key))

	def create_null_fk(self, linking_table, foreign_table, key, create_idx = True):
		if create_idx:
			self.create_idx(linking_table, key)
		self.execute("ALTER TABLE %s ADD CONSTRAINT %s_%s_fk FOREIGN KEY (%s) REFERENCES %s (%s) ON DELETE SET NULL" % (linking_table, linking_table, key, key, foreign_table, key))

	def create_idx(self, table, *args):
		name = "%s_%s_idx" % (table, '_'.join(map(str, args)))
		columns = ','.join(map(str, args))
		self.execute("CREATE INDEX %s ON %s (%s)" % (name, table, columns))

class SQLiteCursor(object):
	allows_join_on_update = False
	is_postgres = False

	def __init__(self, filename):
		self.con = sqlite3.connect(filename, 0, sqlite3.PARSE_DECLTYPES)
		self.con.row_factory = self._dict_factory
		self.cur = self.con.cursor()
		self.rowcount = 0
		self.print_next = False

	def close(self):
		self.cur.close()
		self.con.commit()
		self.con.close()

	# This isn't the most efficient.  See Pg's cursor class for explanation
	# why we want pure dicts.  Besides, speed isn't the primary concern
	# for SQLite, which is used for testing, not production.
	def _dict_factory(self, cursor, row):
		d = {}
		for idx, col in enumerate(cursor.description):
			d[col[0]] = row[idx]
		return d

	# Speaking of performance, everything gets mangled through this method anyway.
	def _convert_pg_query(self, query, for_print = False):
		if query.find("CREATE TABLE") >= 0:
			query = query.replace("SERIAL", "INTEGER")
		if query.find("ADD CONSTRAINT") >= 0:
			return None
		if not for_print:
			query = query.replace("%s", "?")
		query = query.replace("TRUE", "1")
		query = query.replace("FALSE", "0")
		query = query.replace("EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)", "(strftime('%s','now'))")
		return query

	def fetch_var(self, query, params = None):
		self.execute(query, params)
		# if self.cur.rowcount <= 0:
			# return None
		row = self.cur.fetchone()
		if not row:
			return None
		return row[row.keys()[0]]

	def fetch_row(self, query, params = None):
		self.execute(query, params)
		# if self.cur.rowcount <= 0:
			# return None
		return self.cur.fetchone()

	def fetch_all(self, query, params = None):
		self.execute(query, params)
		if self.cur.rowcount == 0:
			return []
		return self.cur.fetchall()

	def fetch_list(self, query, params = None):
		self.execute(query, params)
		arr = []
		for row in self.cur.fetchall():
			arr.append(row[row.keys()[0]])
		return arr

	def update(self, query, params = None):
		self.execute(query, params)
		return self.cur.rowcount

	def execute(self, query, params = None):
		if self.print_next:
			self.print_next = False
			if params:
				print self._convert_pg_query(query, True) % params
			else:
				print self._convert_pg_query(query, True)
		query = self._convert_pg_query(query)
		# If the query can't be done or properly to SQLite,
		# silently drop it.  This is mostly for table creation, things like foreign keys.
		if not query:
			return
		if params:
			self.cur.execute(query, params)
		else:
			self.cur.execute(query)
		self.rowcount = self.cur.rowcount

	def get_next_id(self, table, column):
		val = self.fetch_var("SELECT MAX(" + column + ") + 1 FROM " + table)
		if not val:
			return 1
		return val

	def fetchone(self):
		return self.cur.fetchone()

	def fetchall(self):
		return self.cur.fetchall()

	def create_delete_fk(self, linking_table, foreign_table, key, create_idx = True):
		pass

	def create_null_fk(self, linking_table, foreign_table, key, create_idx = True):
		pass

	def create_idx(self, table, *args):
		pass

def open():
	global connection
	global c
	global c_old

	if c or c_old:
		close()

	type = config.get("db_type")
	name = config.get("db_name")
	host = config.get("db_host")
	port = config.get("db_port")
	user = config.get("db_user")
	password = config.get("db_password")

	if type == "postgres":
		psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
		psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)
		base_connstr = "sslmode=disable "
		if host:
			base_connstr += "host=%s " % host
		if port:
			base_connstr += "port=%s " % port
		if user:
			base_connstr += "user=%s " % user
		if password:
			base_connstr += "password=%s " % password
		connection = psycopg2.connect(base_connstr + ("dbname=%s" % name))
		connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
		connection.autocommit = True
		c = connection.cursor(cursor_factory=PostgresCursor)

		if config.has("db_USE_LIVE_R3") and config.get("db_USE_LIVE_R3"):
			c_old = None
			connection = psycopg2.connect(base_connstr + "dbname=rainwave")
			connection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
			connection.autocommit = True
			c_old = connection.cursor(cursor_factory=PostgresCursor)
		else:
			c_old = c
	elif type == "sqlite":
		log.debug("dbopen", "Opening SQLite DB %s" % name)
		c = SQLiteCursor(name)
		c_old = c
	else:
		log.critical("dbopen", "Invalid DB type %s!" % type)
		return False

	return True

def close():
	global connection
	global c
	global c_old

	if c_old and (c_old != c):
		c_old.close()
		c_old = None
	if c:
		c.close()
		c = None
	if connection:
		connection.close()
		connection = None

	return True

def create_tables():
	if config.test_mode:
		_create_test_tables()

	c.update(" \
		CREATE TABLE r4_albums ( \
			album_id				SERIAL		PRIMARY KEY, \
			album_name				TEXT		, \
			album_rating				REAL		DEFAULT 0, \
			album_rating_count			INTEGER		DEFAULT 0, \
			album_added_on				INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			album_song_count			SMALLINT		DEFAULT 0 \
		)")

	c.update(" \
		CREATE TABLE r4_songs ( \
			song_id					SERIAL		PRIMARY KEY, \
			song_verified				BOOLEAN		DEFAULT TRUE, \
			song_scanned				BOOLEAN		DEFAULT TRUE, \
			song_filename				TEXT		, \
			song_title				TEXT		, \
			song_link				TEXT		, \
			song_link_text				TEXT		, \
			song_length				SMALLINT	, \
			song_added_on				INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			song_rating				REAL		DEFAULT 0, \
			song_rating_count			INTEGER		DEFAULT 0, \
			song_cool_multiply			REAL		DEFAULT 1, \
			song_cool_override			INTEGER		, \
			song_origin_sid				SMALLINT	NOT NULL, \
			song_artist_tag				TEXT		, \
			song_request_count			INT			DEFAULT 0, \
			song_file_mtime				INTEGER		\
		)")
	c.create_idx("r4_songs", "song_verified")

	c.update(" \
		CREATE TABLE r4_song_sid ( \
			song_id					INTEGER		NOT NULL, \
			sid					SMALLINT	NOT NULL, \
			song_cool				BOOLEAN		DEFAULT FALSE, \
			song_cool_end				INTEGER		DEFAULT 0, \
			song_elec_appearances			INTEGER		DEFAULT 0, \
			song_elec_last				INTEGER		DEFAULT 0, \
			song_elec_blocked			BOOLEAN 	DEFAULT FALSE, \
			song_elec_blocked_num			SMALLINT	DEFAULT 0, \
			song_elec_blocked_by			TEXT		, \
			song_vote_share				REAL		, \
			song_vote_total				INTEGER		, \
			song_played_last			INTEGER		, \
			song_exists				BOOLEAN		DEFAULT TRUE, \
			song_request_only			BOOLEAN		DEFAULT FALSE, \
			song_request_only_end		INTEGER	DEFAULT 0, \
			album_id					INTEGER \
		)")
	# c.create_idx("r4_song_sid", "song_id")	# handled by create_delete_fk
	c.create_idx("r4_song_sid", "sid")
	c.create_idx("r4_song_sid", "song_cool")
	c.create_idx("r4_song_sid", "song_elec_blocked")
	c.create_idx("r4_song_sid", "song_exists")
	c.create_idx("r4_song_sid", "song_request_only")
	c.create_delete_fk("r4_song_sid", "r4_songs", "song_id")
	c.create_null_fk("r4_song_sid", "r4_albums", "album_id")

	c.update(" \
		CREATE TABLE r4_song_ratings ( \
			song_id					INTEGER		NOT NULL, \
			user_id					INTEGER		NOT NULL, \
			song_rating_user			REAL		, \
			song_rated_at				INTEGER		, \
			song_rated_at_rank			INTEGER		, \
			song_rated_at_count			INTEGER		, \
			song_fave				BOOLEAN, \
		)")
	# c.create_idx("r4_song_ratings", "user_id", "song_id")		# handled by create_delete_fk
	# c.create_idx("r4_song_ratings", "song_id")
	c.create_delete_fk("r4_song_ratings", "r4_songs", "song_id")
	c.create_delete_fk("r4_song_ratings", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_album_sid ( \
			album_exists				BOOLEAN		DEFAULT TRUE, \
			album_id				INTEGER		NOT NULL, \
			sid					SMALLINT	NOT NULL, \
			album_played_last			INTEGER		DEFAULT 0, \
			album_played_times			INTEGER		DEFAULT 0, \
			album_request_count			INTEGER		DEFAULT 0, \
			album_cool					BOOLEAN		DEFAULT FALSE, \
			album_cool_multiply			REAL		DEFAULT 1, \
			album_cool_override			INTEGER		, \
			album_cool_lowest			INTEGER		DEFAULT 0, \
			album_updated				INTEGER		DEFAULT 0, \
			album_elec_last				INTEGER		DEFAULT 0, \
			album_elec_appearances			INTEGER		DEFAULT 0, \
			album_vote_share			REAL		DEFAULT 0, \
			album_vote_total			INTEGER		DEFAULT 0\
		)")
	c.create_idx("r4_album_sid", "album_exists")
	c.create_idx("r4_album_sid", "sid")
	# c.create_idx("r4_album_sid", "album_id")		# handled by create_delete_fk
	c.create_delete_fk("r4_album_sid", "r4_albums", "album_id")

	c.update(" \
		CREATE TABLE r4_album_ratings ( \
			album_id				INTEGER		NOT NULL, \
			user_id					INTEGER		NOT NULL, \
			album_rating_user		REAL		, \
			album_fave				BOOLEAN, \
			album_rating_complete	BOOLEAN		DEFAULT FALSE \
		)")
	c.create_idx("r4_album_ratings", "user_id", "album_id")
	c.create_idx("r4_album_ratings", "album_id")
	c.create_delete_fk("r4_album_ratings", "r4_albums", "album_id", create_idx=False)
	c.create_delete_fk("r4_album_ratings", "phpbb_users", "user_id", create_idx=False)

	c.update(" \
		CREATE TABLE r4_artists		( \
			artist_id				SERIAL		NOT NULL PRIMARY KEY, \
			artist_name				TEXT		\
		)")

	c.update(" \
		CREATE TABLE r4_song_artist	( \
			song_id					INTEGER		NOT NULL, \
			artist_id				INTEGER		NOT NULL, \
			artist_is_tag				BOOLEAN		DEFAULT TRUE \
		)")
	# c.create_idx("r4_song_artist", "song_id")		# handled by create_delete_fk
	# c.create_idx("r4_song_artist", "artist_id")
	c.create_delete_fk("r4_song_artist", "r4_songs", "song_id")
	c.create_delete_fk("r4_song_artist", "r4_artists", "artist_id")

	c.update(" \
		CREATE TABLE r4_groups ( \
			group_id				SERIAL		PRIMARY KEY, \
			group_name				TEXT		, \
			group_elec_block		SMALLINT	DEFAULT 5, \
			group_cool_time			SMALLINT	DEFAULT 900 \
		)")

	c.update(" \
		CREATE TABLE r4_song_group ( \
			song_id					INTEGER		NOT NULL, \
			group_id				INTEGER		NOT NULL, \
			group_is_tag				BOOLEAN		DEFAULT TRUE \
		)")
	# c.create_idx("r4_song_group", "song_id")		# handled by create_delete_fk
	# c.create_idx("r4_song_group", "group_id")
	c.create_delete_fk("r4_song_group", "r4_songs", "song_id")
	c.create_delete_fk("r4_song_group", "r4_groups", "group_id")

	c.update(" \
		CREATE TABLE r4_schedule ( \
			sched_id				SERIAL		PRIMARY KEY, \
			sched_start				INTEGER		, \
			sched_start_actual			INTEGER		, \
			sched_end				INTEGER		, \
			sched_end_actual			INTEGER		, \
			sched_type				TEXT	, \
			sched_name				TEXT		, \
			sid					SMALLINT	NOT NULL, \
			sched_public				BOOLEAN		DEFAULT TRUE, \
			sched_timed				BOOLEAN		DEFAULT TRUE, \
			sched_url				TEXT		, \
			sched_in_progress			BOOLEAN		DEFAULT FALSE, \
			sched_used				BOOLEAN		DEFAULT FALSE, \
			sched_dj_user_id			INT, \
			sched_creator_user_id		INT \
		)")
	c.create_idx("r4_schedule", "sched_in_progress")
	c.create_idx("r4_schedule", "sched_public")
	c.create_idx("r4_schedule", "sched_start_actual")

	c.update(" \
		CREATE TABLE r4_elections ( \
			elec_id					INTEGER		PRIMARY KEY, \
			elec_used				BOOLEAN		DEFAULT FALSE, \
			elec_in_progress			BOOLEAN		DEFAULT FALSE, \
			elec_start_actual			INTEGER		, \
			elec_type				TEXT		, \
			elec_priority				BOOLEAN		DEFAULT FALSE, \
			sid					SMALLINT	NOT NULL \
		)")
	# If we're using Postgres, make the Election ID use the same sequence
	# as schedule_id for unique numbers so there's one unified "event ID".
	if c.is_postgres:
		c.update("ALTER TABLE r4_elections ALTER COLUMN elec_id SET DEFAULT nextval('r4_schedule_sched_id_seq')")
		c.update("ALTER TABLE r4_elections ALTER COLUMN elec_id SET NOT NULL")
	c.create_idx("r4_elections", "elec_used")
	c.create_idx("r4_elections", "sid")

	c.update(" \
		CREATE TABLE r4_election_entries ( \
			entry_id				SERIAL		PRIMARY KEY, \
			song_id					INTEGER		NOT NULL, \
			elec_id					INTEGER		NOT NULL, \
			entry_type				SMALLINT	DEFAULT 2, \
			entry_position				SMALLINT	, \
			entry_votes				SMALLINT	DEFAULT 0 \
		)")
	# c.create_idx("r4_election_entries", "song_id")	# handled by create_delete_fk
	# c.create_idx("r4_election_entries", "elec_id")
	c.create_delete_fk("r4_election_entries", "r4_songs", "song_id")
	c.create_delete_fk("r4_election_entries", "r4_elections", "elec_id")

	c.update(" \
		CREATE TABLE r4_election_queue ( \
			elecq_id				SERIAL		PRIMARY KEY, \
			song_id					INTEGER		, \
			sid					SMALLINT	NOT NULL \
		)")
	# c.create_idx("r4_election_queue", "song_id")		# handled by create_delete_fk
	c.create_delete_fk("r4_election_queue", "r4_songs", "song_id")

	c.update(" \
		CREATE TABLE r4_one_ups ( \
			sched_id				INTEGER		NOT NULL, \
			song_id					INTEGER		NOT NULL \
		)")
	# c.create_idx("r4_one_ups", "sched_id")		# handled by create_delete_fk
	# c.create_idx("r4_one_ups", "song_id")
	c.create_delete_fk("r4_one_ups", "r4_schedule", "sched_id")
	c.create_delete_fk("r4_one_ups", "r4_songs", "song_id")

	c.update(" \
		CREATE TABLE r4_listeners ( \
			listener_id				SERIAL		PRIMARY KEY, \
			sid					SMALLINT	NOT NULL, \
			listener_ip				TEXT		, \
			listener_relay				TEXT		, \
			listener_agent				TEXT		, \
			listener_icecast_id			INTEGER		NOT NULL, \
			listener_lock				BOOLEAN		DEFAULT FALSE, \
			listener_lock_sid			SMALLINT	, \
			listener_lock_counter			SMALLINT	DEFAULT 0, \
			listener_purge				BOOLEAN		DEFAULT FALSE, \
			listener_voted_entry			INTEGER		, \
			user_id					INTEGER		DEFAULT 1 \
		)")
	c.create_idx("r4_listeners", "sid")
	# c.create_idx("r4_listeners", "user_id")		# handled by create_delete_fk
	c.create_delete_fk("r4_listeners", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_listener_counts ( \
			lc_time					INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			sid					SMALLINT	NOT NULL, \
			lc_guests				SMALLINT	, \
			lc_users				SMALLINT	, \
			lc_users_active				SMALLINT	, \
			lc_guests_active			SMALLINT	\
		)")
	c.create_idx("r4_listener_counts", "lc_time")
	c.create_idx("r4_listener_counts", "sid")

	c.update(" \
		CREATE TABLE r4_donations ( \
			donation_id				SERIAL		PRIMARY KEY, \
			user_id					INTEGER		, \
			donation_amount			REAL		, \
			donation_message		TEXT		, \
			donation_private		BOOLEAN		DEFAULT TRUE \
		)")

	c.update(" \
		CREATE TABLE r4_request_store ( \
			reqstor_id				SERIAL		PRIMARY KEY, \
			reqstor_order			SMALLINT	DEFAULT 32766, \
			user_id					INTEGER		NOT NULL, \
			song_id					INTEGER		NOT NULL \
		)")
	# c.create_idx("r4_request_store", "user_id")		# handled by create_delete_fk
	# c.create_idx("r4_request_store", "song_id")
	c.create_delete_fk("r4_request_store", "phpbb_users", "user_id")
	c.create_delete_fk("r4_request_store", "r4_songs", "song_id")

	c.update(" \
		CREATE TABLE r4_request_line ( \
			user_id					INTEGER		NOT NULL, \
			sid					SMALLINT	NOT NULL, \
			line_wait_start				INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			line_expiry_tune_in			INTEGER		, \
			line_expiry_election			INTEGER \
		)")
	# c.create_idx("r4_request_line", "user_id")		# handled by create_delete_fk
	c.create_idx("r4_request_line", "sid")
	c.create_idx("r4_request_line", "line_wait_start")
	c.create_delete_fk("r4_request_line", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_request_history ( \
			request_id				SERIAL		PRIMARY KEY, \
			user_id					INTEGER		NOT NULL, \
			song_id					INTEGER		NOT NULL, \
			request_fulfilled_at			INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			request_wait_time			INTEGER		, \
			request_line_size			INTEGER		, \
			request_at_count			INTEGER		\
		)")
	# c.create_idx("r4_request_history", "user_id")		# handled by create_delete_fk
	# c.create_idx("r4_request_history", "song_id")
	c.create_null_fk("r4_request_history", "r4_songs", "song_id")
	c.create_delete_fk("r4_request_history", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_vote_history ( \
			vote_id					SERIAL		PRIMARY KEY, \
			vote_time				INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			elec_id					INTEGER		, \
			user_id					INTEGER		NOT NULL, \
			song_id					INTEGER		NOT NULL, \
			vote_at_rank				INTEGER		, \
			vote_at_count				INTEGER		, \
			entry_id				INTEGER		\
		)")
	# c.create_idx("r4_vote_history", "user_id")		# handled by create_delete_fk
	# c.create_idx("r4_vote_history", "song_id")
	# c.create_idx("r4_vote_history", "entry_id")
	c.create_null_fk("r4_vote_history", "r4_election_entries", "entry_id")
	c.create_null_fk("r4_vote_history", "r4_elections", "elec_id")
	c.create_null_fk("r4_vote_history", "r4_songs", "song_id")
	c.create_delete_fk("r4_vote_history", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_api_keys ( \
			api_id					SERIAL		PRIMARY KEY, \
			user_id					INTEGER		NOT NULL, \
			api_ip					TEXT		, \
			api_key					VARCHAR(10) , \
			api_expiry				INTEGER		\
		)")
	# c.create_idx("r4_api_keys", "user_id")		# handled by create_delete_fk
	c.create_idx("r4_api_keys", "api_ip")
	c.create_delete_fk("r4_api_keys", "phpbb_users", "user_id")

	c.update(" \
		CREATE TABLE r4_oneup_lists ( \
			oneuplist_id				SERIAL		PRIMARY KEY, \
			sched_id				INTEGER		\
		)")
	# c.create_idx("r4_oneup_lists", "sched_id")		# handled by create_delete_fk
	c.create_delete_fk("r4_oneup_lists", "r4_schedule", "sched_id")

	c.update(" \
		CREATE TABLE r4_oneup_list_content ( \
			oneuplist_id				INTEGER		NOT NULL, \
			song_id					INTEGER		NOT NULL, \
			oneup_position				SMALLINT	\
		)")
	c.create_delete_fk("r4_oneup_list_content", "r4_songs", "song_id")
	c.create_delete_fk("r4_oneup_list_content", "r4_oneup_lists", "oneuplist_id")

	c.update(" \
		CREATE TABLE r4_song_history ( \
			songhist_id				SERIAL		PRIMARY KEY, \
			songhist_time			INTEGER		DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP), \
			sid						SMALLINT	NOT NULL, \
			song_id					INTEGER		NOT NULL \
		)")
	c.create_idx("r4_song_history", "sid")
	c.create_delete_fk("r4_song_history", "r4_songs", "song_id")

	if config.test_mode:
		_fill_test_tables()

def _create_test_tables():
	c.update(" \
		CREATE TABLE phpbb_users( \
			user_id					SERIAL		PRIMARY KEY, \
			radio_totalvotes		INTEGER		DEFAULT 0, \
			radio_totalratings		INTEGER		DEFAULT 0, \
			radio_totalmindchange	INTEGER		DEFAULT 0, \
			radio_totalrequests		INTEGER		DEFAULT 0, \
			radio_winningvotes			INT		DEFAULT 0, \
			radio_losingvotes			INT		DEFAULT 0, \
			radio_winningrequests			INT		DEFAULT 0, \
			radio_losingrequests			INT		DEFAULT 0, \
			radio_listenkey			TEXT		DEFAULT 'TESTKEY', \
			group_id				INT		DEFAULT 1, \
			username				TEXT 		DEFAULT 'Test', \
			user_new_privmsg			INT		DEFAULT 0, \
			user_avatar				TEXT		DEFAULT '', \
			user_avatar_type			INT		DEFAULT 0, \
			radio_inactive			BOOLEAN		DEFAULT FALSE \
		)")

	c.update("CREATE TABLE phpbb_sessions("
				"session_user_id		INT,"
				"session_id				TEXT,"
				"session_last_visit		INT,"
				"session_page			TEXT)")

	c.update("CREATE TABLE phpbb_session_keys(key_id TEXT, user_id INT)")

def _fill_test_tables():
	# Anonymous user
	c.update("INSERT INTO phpbb_users (user_id, username) VALUES (1, 'Anonymous')")
	c.update("INSERT INTO r4_api_keys (user_id, api_key, api_ip) VALUES (1, 'TESTKEY', '127.0.0.1')")

	# User ID 2: site admin
	c.update("INSERT INTO phpbb_users (user_id, username, group_id) VALUES (2, 'Test', 5)")
	c.update("INSERT INTO r4_api_keys (user_id, api_key) VALUES (2, 'TESTKEY')")
