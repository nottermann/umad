import re
from operator import itemgetter
from itertools import chain
from dateutil.parser import *
from dateutil.tz import *
import requests
import redis

TICKET_URL_TEMPLATE = 'https://ticket.api.anchor.com.au/ticket/{0}'.format
TICKET_MESSAGE_URL_BASE = 'https://ticket.api.anchor.com.au/ticket_message'
WEB_TICKET_URL_TEMPLATE = 'https://rt.engineroom.anchor.net.au/Ticket/Display.html?id={_id}'.format

TICKET_UNSTALL_RE = re.compile(r'The ticket \d+ has not received a reply from the requestor for.*Get on the phone with the client right now', re.I)

CUSTOMER_NAME_CACHE_TTL = 7 * 24 * 60 * 60 # 1 week, in seconds

from distiller import Distiller


class RtTicketDistiller(Distiller):
	doc_type = 'rt'

	@classmethod
	def will_handle(klass, url):
		return url.startswith( ('rt://', 'https://rt.engineroom.anchor.net.au/') )

	@staticmethod
	def clean_message(msg):
		fields_we_care_about = (
				'_id',
				'from_email',
				'from_realname',
				'content',
				'subject',
				'created',
				'private',
				)
		clean_msg = dict([ (k,v) for (k,v) in msg.iteritems() if k in fields_we_care_about ])

		# When we reply on a ticket, we get the ticket-wide subject if we don't specify a different Subject
		if clean_msg['subject'] == 'No Subject':
			clean_msg['subject'] = ''

		# Message body cleanup, attempt to nuke junk
		body_lines = clean_msg['content'].split('\n')
		body_lines = [ line.strip() for line in body_lines ]                     # Remove leading and trailing whitespace for later compaction
		body_lines = [ line for line in body_lines if not line.startswith('>') ] # Quoted lines
		body_lines = [ line for line in body_lines if line not in ('', '.') ]    # Empty lines or lines with a single dot (usually from creation of internal tickets)
		body_lines = [ line for line in body_lines if line not in ('Hi,', 'Hello,') or len(line) > 20 ] # Greetings

		# Remove Anchor boilerplate when customers reply to the message when they first file a ticket
		boilerplate = ( u'Hello and thanks for your email.',
						u'This is an automated response from our email robots.',
						u'If you\u2019d like to speak to someone about your issue immediately, please contact us via phone, being sure to keep the issue number handy so we can refer to it quickly:',
						u'* Australia Local call 1300 883 979', u'* US toll free (888) 250 8847',
						u'If replying by email, please ensure that the subject line remains the same as it is now.',
						u'In the meantime, feel free to check out the following resources, to learn a little more about the Anchor Team and how we work.',
						u'* Our Blog: http://www.anchor.com.au/blog/'
					)
		body_lines = [ line for line in body_lines if not line.startswith(boilerplate) ]

		# Remove reply text, RT ticket clone text - eg "On Tuesday, 20 May 2014 4:43 PM, New support request via Anchor Helpdesk <support@anchor.com.au> wrote:"
		# XXX: Do we want to clear the text below this that appears in other messages as well?
		body_lines = [ line for line in body_lines if not (line.startswith('On') and line.endswith('wrote:')) ]

		# Remove thanks and signatures
		lines_beginning_with_thanks = [ line for line in body_lines if line.startswith('Thanks') and len(line) < 10 ] # Kill trailing platitudes
		if lines_beginning_with_thanks:
			body_lines = body_lines[:body_lines.index(lines_beginning_with_thanks[0])]
		if 'Regards,' in body_lines:                                             # Kill trailing platitudes
			body_lines = body_lines[:body_lines.index('Regards,')]
		if '--' in body_lines:                                                   # Kill signatures
			body_lines = body_lines[:body_lines.index('--')]

		# And put it all back together again
		clean_msg['content'] = '\n'.join(body_lines)

		# Sloppiness in the ticket API?
		if clean_msg['from_email'] is None:
			clean_msg['from_email'] = ''

		# Sloppiness in the ticket API?
		if clean_msg['from_realname'] is None:
			clean_msg['from_realname'] = ''

		# Kill quotemarks around names
		clean_msg['from_realname'] = clean_msg['from_realname'].strip("'\"")

		# Don't index the automated comment that gets added when a ticket unstalls itself
		if TICKET_UNSTALL_RE.search(clean_msg['content']):
			clean_msg['content'] = ''

		return clean_msg


	# Expect an URL of form:
	# https://rt.engineroom.anchor.net.au/Ticket/Display.html?id=152
	def tidy_url(self):
		"Turn the RT URL into an API URL"

		if self.url.startswith('rt://'):
			self.url = self.url.replace('rt://', 'https://rt.engineroom.anchor.net.au/Ticket/Display.html?id=')

		rt_url_match = re.match(r'https://rt\.engineroom\.anchor\.net\.au/Ticket/\w+\.html\?id=(\d+)', self.url)
		if rt_url_match is None:
			raise ValueError("This URL doesn't match our idea of an RT URL: %s" % self.url)
		ticket_number = rt_url_match.group(1)

		self.supplied_ticket_id = ticket_number
		self.ticket_url         = TICKET_URL_TEMPLATE(ticket_number)


	def blobify(self):
		# Customer Name cache
		try:
			cn_cache = redis.StrictRedis(host='localhost', port=6379, db=0)
			cn_key   = "customer_id:{0}".format
			cn_get   = lambda x: None if x is None else cn_cache.get(cn_key(x))  # Being a jackass, I <3 curry
		except:
			raise RuntimeError("Can't connect to local Redis server to cache customer details")


		# Prepare auth
		try:
			api_credentials = self.auth['anchor_api']
		except:
			raise RuntimeError("You must provide Anchor API credentials, please set API_AUTH_USER and API_AUTH_PASS")

		# Prep URL and headers for requests
		self.tidy_url()
		ticket_url   = self.ticket_url

		# Get ticket from API
		ticket_response = requests.get(ticket_url, auth=api_credentials, verify=True, headers=self.accept_json)
		try:
			ticket_response.raise_for_status()
		except:
			#debug("Couldn't get ticket from API, HTTP error %s, probably not allowed to view ticket" % ticket_response.status_code)
			if ticket_response.status_code == 404:
				self.enqueue_deletion()
			return

		# Mangle ticket until no good
		ticket = ticket_response.json() # FIXME: add error-checking
		if 'code' in ticket: # we got a 404 or 403 or something, probably redundant after the raise_for_status check
			#debug("Ticket API hates us? %s" % str(ticket) )
			return

		ticket_url         = WEB_TICKET_URL_TEMPLATE(**ticket) # Canonicalise the ticket URL, as merged tickets could have been accessed by multiple URLs
		ticket_number      = "{_id}".format(**ticket)
		ticket_subject     = ticket['subject']
		ticket_status      = ticket['status']
		ticket_queue       = ticket['queue']
		ticket_category    = ticket['category'] # Unlike some other fields, this can be None
		ticket_priority    = ticket['priority']
		ticket_lastupdated = ticket['lastupdated']
		customer_visible   = True if not ticket['private'] else False

		self.resolved_ticket_id = ticket_number

		# Handle deleted and merged tickets by calling our enqueue_deletion method.
		# self.url   is the "original" URL, before resolution.
		# ticket_url is the resolved URL, following any merges.
		#
		#  - Merged tickets: Delete the original URL if there's a mismatch
		#  - Deleted tickets: Delete the resolved URL
		#  - What about tickets that have been merged and then deleted? Handle the
		#    merge first, then deletion

		# Merged tickets
		if self.supplied_ticket_id != self.resolved_ticket_id:
			self.enqueue_deletion()
			print "Merge detected, {0} into {1}, enqueued for deletion from index: {2}".format(self.supplied_ticket_id, self.resolved_ticket_id, self.url)

		# Deleted tickets (hidden from display later, but delete them now)
		if ticket_status == 'deleted':
			# XXX: Is this the correct URL to nuke? For A merged into B,
			# this will nuke B if it was also deleted. A should have been
			# deleted in the previous step.
			self.enqueue_deletion(url=ticket_url)
			print "Status=deleted, enqueued for deletion from index: {0}".format(ticket_url)
			return

		# This may be None if there's no Related Customer set
		customer_url = ticket['customer_url']
		customer_id  = customer_url.rpartition('/')[-1] if customer_url else None

		customer_name = None
		if customer_id:
			if cn_get(customer_id) is None:
				# We need to retrieve it from the customer API
				customer_response = requests.get(customer_url, auth=api_credentials, verify=True, headers=self.accept_json)
				if customer_response.status_code != 200:
					retrieved_name = '__NOT_FOUND__'
				else:
					customer = customer_response.json() # FIXME: add error-checking
					retrieved_name = customer.get('description', '__NOT_FOUND__') # more paranoia against the unexpected

				# Now stash it
				cn_cache.setex( cn_key(customer_id), CUSTOMER_NAME_CACHE_TTL, retrieved_name ) # Assume success, should only fail if TTL is invalid

			# Now make use of it
			maybe_customer_name = cn_get(customer_id)
			if maybe_customer_name != "__NOT_FOUND__":
				# Even if everything goes wrong and cn_key() gives us
				# None after hitting that customer API, it's okay.
				customer_name = maybe_customer_name

		# Get a real datetime object, let ElasticSearch figure out the rest
		ticket_lastupdated = parse(ticket_lastupdated)
		ticket_lastupdated = ticket_lastupdated.astimezone(tzutc())

		# Get associated messages from API
		messages_response  = requests.get(TICKET_MESSAGE_URL_BASE, params={'ticket_url': self.ticket_url}, auth=api_credentials, verify=True, headers=self.accept_json)
		try:
			messages_response.raise_for_status()
		except:
			#debug("Error getting Messages from API, got HTTP response {0}".format(ticket_response.status_code))
			return



		# Mangle messages until no good
		messages = messages_response.json() # FIXME: add error-checking
		messages = [ self.clean_message(x) for x in messages ]

		# We see git@bitts rollin', we hatin'
		messages = [ m for m in messages if not m['from_email'].startswith('git@bitts') ]

		# Pull out the first post, we'll use it for the excerpt
		# XXX: Blindly assumes the first post has the lowest numerical ID, check with dev team whether this is correct
		messages.sort(key=itemgetter('_id'))
		# Some messages are empty or otherwise useless, so ignore them
		messages = [ m for m in messages if m['subject'] or m['content'] or m['from_email'] ]

		# XXX: A ticket with no useful message?  Something's wrong - quick, let's die!
		if len(messages) == 0:
			raise RuntimeError("{} had no useful messages in ticket API".format(self.ticket_url) )

		# Get the first clean message
		first_post = {}
		for message in messages:
			if message['content']!= '':
				first_post = message
				break
		first_post['content'] = '\n'.join( first_post['content'].split('\n')[:6] )
		ticket_excerpt = first_post['content'].encode('utf8')

		# Grab customer-presentable messages
		if customer_visible:
			public_messages = [ m for m in messages if not m['private'] ]

			# XXX: We have found non-private tickets without any customer-visible messages, like rt://8785
			if public_messages:
				public_first_post = public_messages[0]
				public_ticket_excerpt = public_first_post['content'].encode('utf8')
			else:
				public_ticket_excerpt = "No excerpt could be found for this ticket, please contact Anchor Support for assistance"

		# This is an empty list if the ticket has seen no actual communication (eg. internal-only tickets)
		contact_timestamps = [ parse(m['created']) for m in messages if not m['private'] ]


		# Put together our response. We have:
		# - ticket_url (string)
		# - ticket_subject (string)
		# - ticket_status (string)
		# - ticket_queue (string)
		# - ticket_category (string or None)
		# - ticket_priority (int)
		# - messages (iterable of dicts)
		# - public_messages (iterable of dicts)
		# - public_ticket_excerpt (string)

		all_message_lines = [ x for x in chain(*[ message['content'].split('\n') for message in messages ]) ]
		if customer_visible:
			public_all_message_lines = [ x for x in chain(*[ message['content'].split('\n') for message in public_messages ]) ]
		realnames         = list(set( [ x['from_realname'] for x in messages if x['from_realname'] != '' ] ))
		emails            = list(set( [ x['from_email']    for x in messages if x['from_email']    != '' ] ))

		blob = " ".join([
				ticket_number.encode('utf8'),
				ticket_subject.encode('utf8'),
				' '.join(realnames).encode('utf8'),
				' '.join(emails).encode('utf8'),
				' '.join(all_message_lines).encode('utf8'),
				])

		public_blob = None
		if customer_visible and public_all_message_lines: # Double-check for existence of real content
			public_blob = " ".join([
					ticket_number.encode('utf8'),
					ticket_subject.encode('utf8'),
					' '.join(realnames).encode('utf8'),
					' '.join(emails).encode('utf8'),
					' '.join(public_all_message_lines).encode('utf8'),
					])

		ticketblob = {
			'url':              ticket_url,
			'blob':             blob,
			'local_id':         ticket_number,
			'title':            ticket_subject, # printable as a document title
			'excerpt':          ticket_excerpt,
			'subject':          ticket_subject,
			'status':           ticket_status,
			'queue':            ticket_queue,
			'priority':         ticket_priority,
			'realname':         realnames,
			'email':            emails,
			'last_updated':     ticket_lastupdated,
			'customer_visible': customer_visible,
			}

		# Only set category if it's meaningful
		if ticket_category:
			ticketblob['category'] = ticket_category

		# Only set public_blob if we've got it
		if public_blob:
			ticketblob['public_blob'] = public_blob

		# Only set last_contact if it has meaning
		if contact_timestamps:
			ticketblob['last_contact'] = max(contact_timestamps).astimezone(tzutc())

		# Only set customer details if we have that metadata
		if customer_id:
			ticketblob['customer_id'] = customer_id

		if customer_name:
			ticketblob['customer_name'] = customer_name

		if customer_url:
			ticketblob['customer_url'] = customer_url

		maybe_customer_details = ' '.join(  [ x for x in (customer_name,customer_id) if x is not None ]  )
		if maybe_customer_details:
			ticketblob['customer'] = maybe_customer_details

		yield ticketblob
