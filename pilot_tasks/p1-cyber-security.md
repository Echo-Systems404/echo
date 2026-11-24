---
repeat: forever
every: 15m
areas: ~/Documents/Echo tasks/cyber security
powers: read, append, security
---
# cyber security

The harness's sentinel watches this Mac every few minutes by itself: remote
access, malware signs (XProtect reports, unsigned startup items, programs in
temp folders, changes to sensitive files), and what talks to the internet. It
contains high-risk finds and leaves reminders on its own. Your job is the log.

1. Call security_events once. It shows what the sentinel logged since your
   last summary, and any open incident.
2. Use append_file to add exactly one line of JSON to security-log.jsonl:
   {"ts": "<now, YYYY-MM-DD HH:MM>", "status": "quiet" or "changes" or "incident", "events": <how many>, "high": <how many high>, "summary": "<one or two plain sentences: what happened and what it means for the user>", "actions": ["<what the harness did, from the events' Done notes>"]}
   If there were no events, use "quiet", a short summary, and an empty actions list.
3. Don't use remind for anything the sentinel already reminded the user about. Use it only if something in the events worries you and the sentinel didn't flag it.
4. Those are the only tool calls to make. Finish with a one-line report.
