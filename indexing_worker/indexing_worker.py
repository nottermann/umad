import sys
import os

import redis

from elasticsearch_backend import *


# XXX: maybe these should be to stdout instead of stderr, I dunno
def debug(msg, force_debug=False):
	if DEBUG or force_debug:
		sys.stderr.write(PID_PREFIX + str(msg) + '\n')
		sys.stderr.flush()

def mention(msg):
	sys.stderr.write(PID_PREFIX + str(msg) + '\n')
	sys.stderr.flush()


DEBUG = bool(os.environ.get('UMAD_INDEXING_WORKER_DEBUG') == "True")
PID_PREFIX = '[pid {0}] '.format(os.getpid())
debug("Debug logging is enabled")



def index(url):
	debug("URL to index: {0}".format(url))

	try:
		d = get_distiller(url)
	except Exception as e:
		raise RuntimeError("Don't know how to handle URL: {0}".format(url))

	for doc in d.docs:
		if doc is None:
			# XXX: should this be `continue` instead? For distillers+urls that can return multiple documents, some of which might fail.
			return
		debug("Adding to index: {0} (type of the blob is {1})".format(doc['url'], type(doc['blob'])))

		# Depending on the backend, the blob will either be str
		# or unicode:
		#   Gollum:  unicode
		#   RT:      str
		#   Map:     str
		#   Provsys: str
		#
		# We need to test for this because blindly encode()ing
		# the blob will result in errors if it's already a
		# UTF8 str.
		trimmed_blob = doc['blob'][:400]
		if type(trimmed_blob) is str:
			debug("400 chars of blob: {0}".format(trimmed_blob))
		else: # unicode
			debug(u"400 chars of blob: {0}".format(trimmed_blob).encode('utf8'))
		add_to_index(doc)
		mention("Successfully added to index: %(url)s" % doc)
		debug("")


def delete(url):
	debug("URL to delete: {0}".format(url))
	try:
		delete_from_index(url)
	except Exception as e:
		mention("Failed to delete {0} from index: {1}".format(url, e) )
	else:
		mention("Deleted {0} from index".format(url) )



def main(argv=None):
	debug("Debug logging is enabled")

	redis_server_host = os.environ.get('UMAD_REDIS_HOST', 'localhost')
	redis_server_port = os.environ.get('UMAD_REDIS_PORT', 6379)
	teh_redis = redis.StrictRedis(host=redis_server_host, port=int(redis_server_port), db=0)

	while True:
		try:
			# Get URLs out of Redis. We're using this idiom to provide what is
			# effectively a "BSPOP" (blocking pop from a set), on a sorted set.
			# cf. Event Notification: http://redis.io/commands/blpop

			# Process deletions
			while True:
				pipeline = teh_redis.pipeline()
				pipeline.zrange('umad_deletion_queue', 0, 0)
				pipeline.zremrangebyrank('umad_deletion_queue', 0, 0)
				(urls, urlcount) = pipeline.execute() # Should return:  [ [maybe_single_url], {0|1} ]

				if not urls:
					break
				url = urls[0]

				try:                   delete(url)
				except Exception as e: debug("Something went boom while deleting {0}: {1}".format(url, e))

			# Process additions/updates
			while True:
				pipeline = teh_redis.pipeline()
				pipeline.zrange('umad_indexing_queue', 0, 0)
				pipeline.zremrangebyrank('umad_indexing_queue', 0, 0)
				(urls, urlcount) = pipeline.execute() # Should return:  [ [maybe_single_url], {0|1} ]

				if not urls:
					break
				url = urls[0]

				try:                   index(url)
				except Exception as e: debug("Something went boom while indexing {0}: {1}".format(url, e))

			debug("The barber is napping")
			teh_redis.brpop('barber')
			debug("------------------------")
			debug("The barber was woken up!")
		except Exception as e:
			debug("Something went boom: {0}".format(e))


	return 0


if __name__ == "__main__":
	sys.exit(main())
