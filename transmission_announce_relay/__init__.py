"""transmission-announce-relay: a local announce relay for Transmission.

Routes tracker announces through a loopback HTTP endpoint that opens a fresh,
verified HTTPS connection per request, hedges stalled connections, and retries
once. Peer traffic is untouched.
"""
__version__ = '0.1.1'
