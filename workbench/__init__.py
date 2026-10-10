"""Operator review workbench: sign-in, page review and correction, and Act approval.

An operator-only web application, separate from the public read-only API. It keeps its own
workflow database (who reviewed what, every page revision, every approval) and never writes to
the public corpus; an approved Act is exported for the audited policy commands to record.
"""
