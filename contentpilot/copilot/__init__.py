"""ContentPilot v3 copilot layer: triage, statuses, due rules, search.

Sits on top of the existing sources/cards tables. SQLite stays the source of
truth; nothing here ever deletes a post.
"""
